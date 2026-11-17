"""Execute one frozen-memory reply using the existing v1.2 recording gateway."""

from copy import deepcopy
from dataclasses import asdict

from agent_memory_eval.contracts.core import ContractError, RunContext, canonical, digest, new_id
from agent_memory_eval.contracts.snapshot_replay import SCOPE, replay_response_error, validate_replay_record
from agent_memory_eval.memory.preparation import snapshot_replay_input
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry, public_assistant_message


class ReplayInputMismatch(ContractError):
    code = "REPLAY_INPUT_MISMATCH"


def run_snapshot_replay(journal, decision_ref, snapshot_id, *, policy, instruction, condition_id,
                        client, execution_limits, expected_request_sha256, run_id=None, context_reader=None):
    """Low-level runtime. The caller owns frozen experiment settings and a shared spend guard."""
    if journal.contract_version != "1.2":
        raise ContractError("Snapshot replay needs a v1.2 journal")
    if run_id is not None and (not isinstance(run_id, str) or not run_id or run_id in journal.scan["starts"]):
        raise ContractError("Replay run ID must be new and nonempty")
    kind = "illustrative" if getattr(client, "offline", False) else "observed"
    model = getattr(client, "model", None)
    identity = None
    if kind == "observed":
        if not model or not model.get("selected_endpoint"):
            raise ContractError("Observed replay requires a verified model and endpoint")
        identity = dict(id=model["id"], canonical_slug=model.get("canonical_slug"),
                        provider_name=model["selected_endpoint"]["provider_name"])
    config = dict(format="agent-memory-snapshot-replay-config/v1", record_kind=kind,
        decision_ref=decision_ref, snapshot_id=snapshot_id, policy=asdict(policy), instruction=instruction,
        expected_request_sha256=expected_request_sha256, execution_contract="per-role-limits/v1",
        execution_limits=asdict(execution_limits), verified_model=identity,
        response_projection="public-assistant/v1", scope=SCOPE)
    validate_replay_record(config)
    if not isinstance(condition_id, str) or not condition_id.strip():
        raise ContractError("Replay condition must be explicit")
    planned = snapshot_replay_input(journal.blobs, journal.state, decision_ref, snapshot_id,
        policy=policy, instruction=instruction, max_input_bytes=execution_limits.max_input_bytes,
        context_reader=context_reader)
    source, expected = planned["source"], planned["request"]
    if digest(canonical(expected)) != expected_request_sha256:
        raise ReplayInputMismatch("The selected decision, snapshot or rendering differs from preparation")
    if source["archive"]["record_kind"] != kind:
        raise ContractError("Source and replay transport record kinds differ")
    if kind == "observed" and (model["id"] != expected["model"] or getattr(client, "route", None) != expected.get("provider")
            or getattr(client, "max_input_bytes", None) != execution_limits.max_input_bytes):
        raise ContractError("Replay transport model, route or input limit differs from the frozen request")
    if kind == "illustrative" and expected["model"] != "synthetic-offline":
        raise ContractError("Illustrative replay requires its synthetic model identity")
    producer = [e for e in journal.events.values() if e["type"] == "bank.snapshot_created"
                and e["payload"]["snapshot_id"] == snapshot_id]
    if len(producer) != 1 or producer[0]["record_kind"] != kind:
        raise ContractError("Snapshot and replay record kinds differ")
    ctx = RunContext(journal.experiment_id, run_id or new_id("replay"), new_id("s"), condition_id,
                     "diagnostic", "solve", kind)
    memory = MemoryService(journal, record_kind=kind)
    bank = memory.create_bank(ctx.run_id, policy, snapshot_id)
    state = deepcopy(journal.state)
    state.setdefault("snapshot_replays", {})[ctx.run_id] = config
    journal.append([ctx.at("prepare").event("run.started", dict(bank_id=bank, start_snapshot_id=snapshot_id,
        fresh_session=True, source_run_ids=[source["archive"]["source_run_id"]],
        task_ref=source["archive"]["task_ref"]))], state=state)
    message_ref, error_code = None, None
    try:
        preload = memory.preload(ctx, bank)
        registry = SourceRegistry(journal, ctx)
        template = registry.template({k:v for k,v in expected.items() if k != "messages"}, roles=("worker",))
        messages = []
        for index, message in enumerate(expected["messages"]):
            if index == 1:
                messages.append((registry.add("memory_snapshot", preload["event_id"]), ""))
            else:
                messages.append((registry.template(dict(message=message), roles=("worker",)), "/message"))

        def exact_wire(wire):
            if canonical(wire) != canonical(expected):
                raise ReplayInputMismatch("Final wire differs from the approved preparation")
            return wire

        message, response_source, _ = RecordingGateway(journal).call(ctx, registry, "worker", template,
            messages, client, max_requests=None, execution_limits=execution_limits, transform=exact_wire)
        raw = journal.blobs.get(registry.sources[response_source]["artifact_ref"])
        error_code = replay_response_error(raw, identity)
        if error_code is None:
            message_ref = journal.blobs.put(public_assistant_message(message))
        else:
            journal.append([ctx.event("runtime.error", dict(code=error_code))])
    except Exception as error:
        error_code = getattr(error, "code", type(error).__name__)
        if getattr(error, "recorded_event_id", None) not in journal.events:
            journal.append([ctx.event("runtime.error", dict(code=error_code))])
    finishes = [e["payload"] for e in journal.events.values()
                if e["run_id"] == ctx.run_id and e["type"] == "model.request_finished"]
    finish = finishes[0] if finishes else None
    final_snapshot = memory.freeze(bank)
    memory.seal(bank)
    unknown = finish is not None and finish["cost_status"] == "unknown"
    result = dict(format="agent-memory-snapshot-replay-result/v1", run_id=ctx.run_id, bank_id=bank,
        snapshot_id=final_snapshot, request_id=finish["request_id"] if finish else None,
        message_ref=message_ref, status="response" if error_code is None else "error", error_code=error_code,
        unknown_cost=unknown, stop_required=unknown or error_code is not None,
        official_success=None, scope=SCOPE)
    validate_replay_record(result)
    journal.append([ctx.at("grade").event("business.transition", dict(origin="snapshot_replay.result",
        result_ref=journal.blobs.put(result)))])
    journal.append([ctx.at("grade").event("outcome.scored", dict(grading_status="not_available",
        official_success=None, official_score=None, official_artifact_ref=None))])
    journal.append([ctx.at("seal").event("run.ended", dict(termination_reason="single_reply" if error_code is None else error_code,
        grading_status="not_available", end_snapshot_id=final_snapshot))])
    return result
