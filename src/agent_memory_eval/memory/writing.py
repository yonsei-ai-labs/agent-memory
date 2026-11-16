"""Write isolated memories from one closed run's public observations."""

from copy import deepcopy
from dataclasses import asdict, dataclass

from agent_memory_eval.contracts.core import ContractError, RunContext, canonical, new_id
from agent_memory_eval.memory.router import ToolRouter
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry


def public_message(value):
    """Project only text and public calls; never forward provider reasoning fields."""
    role = value.get("role")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ContractError("Unsupported public message role")
    message = dict(role=role, content=value.get("content"))
    if message["content"] is not None and not isinstance(message["content"], str):
        raise ContractError("Only text public experiences are supported")
    if role == "tool":
        message["tool_call_id"] = value["tool_call_id"]
    if value.get("tool_calls"):
        message["tool_calls"] = [dict(id=call["id"], type="function", function=dict(
            name=call["function"]["name"], arguments=call["function"]["arguments"]))
            for call in value["tool_calls"]]
    return message


def _experience(blobs, scan, run_id):
    start = scan["starts"][run_id]
    events = [e for e in scan["events"].values() if e["run_id"] == run_id]
    ends = [e for e in events if e["type"] == "run.ended"]
    outcomes = [e for e in events if e["type"] == "outcome.scored"]
    if len(ends) != 1 or len(outcomes) != 1:
        raise ContractError("Writing requires one closed, scored source run")
    end = ends[0]
    if any(e["type"] == "memory.stored" for e in events):
        raise ContractError("Use an unwritten source so feedback variants share the same observations")
    public = [e for e in events if e["type"] == "business.transition"
              and "public_message_ref" in e["payload"] and e["phase"] == "solve"]
    if not public:
        raise ContractError("The source has no recorded worker-public observations")
    anchor = next(row for row in scan["records"] if any(e["event_id"] == end["event_id"] for e in row["events"]))
    return dict(format="agent-memory-public-experience/v1", experiment_id=start["experiment_id"],
        source_run_id=run_id, record_kind=start["record_kind"], source_end_event_id=end["event_id"],
        source_end_seq=end["event_seq"], source_commit_sha256=anchor["record_sha256"],
        task_ref=start["payload"]["task_ref"],
        observations=[dict(event_id=e["event_id"], event_seq=e["event_seq"],
            source_ref=e["payload"]["public_message_ref"],
            message=public_message(blobs.get(e["payload"]["public_message_ref"]))) for e in public],
        outcome_event_id=outcomes[0]["event_id"])


def freeze_public_experience(journal, run_id):
    return journal.blobs.put(_experience(journal.blobs, journal.scan, run_id))


def read_public_experience(blobs, scan, ref):
    value = blobs.get(ref)
    if canonical(value) != canonical(_experience(blobs, scan, value["source_run_id"])):
        raise ContractError("Frozen public experience differs from its source events")
    return value


@dataclass(frozen=True)
class WriterFeedback:
    level: str
    critique: str | None = None
    critique_origin: str | None = None

    def __post_init__(self):
        if self.level not in {"none", "result_only", "critique"}:
            raise ContractError("Unknown writer feedback level")
        if self.level == "critique":
            if any(not isinstance(v, str) or not v.strip() for v in (self.critique, self.critique_origin)):
                raise ContractError("Critique text and its explicit origin are required")
        elif self.critique is not None or self.critique_origin is not None:
            raise ContractError("This feedback level cannot contain a critique")


def writer_inputs(blobs, scan, experience, feedback):
    context = dict(kind="past_public_experience", task_context=public_message(blobs.get(experience["task_ref"])),
                   messages=[o["message"] for o in experience["observations"]])
    result = dict(kind="evaluation_feedback", level=feedback.level)
    if feedback.level != "none":
        outcome = scan["events"][experience["outcome_event_id"]]["payload"]
        if outcome["grading_status"] != "graded" or type(outcome["official_success"]) is not bool:
            raise ContractError("Result feedback requires an available source outcome")
        result["success"] = outcome["official_success"]
    if feedback.level == "critique":
        result["critique"] = feedback.critique
    return [dict(role="user", content=canonical(value).decode()) for value in (context, result)]


class FrozenBusiness:
    ended, steps, stop_tools = True, 0, frozenset()


def write_from_experience(journal, experience_ref, feedback, *, instruction, policy, client,
                          model_config, max_requests, condition_id, run_id=None):
    """No collection rerun, source grading artifact, business tools or shared writer session."""
    if (not policy.enabled or policy.write_timing != "post_task" or policy.read_mode != "none"
            or policy.same_episode_read or not isinstance(instruction, str) or not instruction.strip()
            or type(max_requests) is not int or max_requests < 1):
        raise ContractError("Writing requires explicit post-task-only policy, instruction and request limit")
    allowed = {"model", "temperature", "seed", "reasoning", "provider", "stream", "max_completion_tokens", "max_tokens"}
    if set(model_config) - allowed or not isinstance(model_config.get("model"), str):
        raise ContractError("Unexpected writer model configuration")
    experience = read_public_experience(journal.blobs, journal.scan, experience_ref)
    inputs = writer_inputs(journal.blobs, journal.scan, experience, feedback)
    kind = "illustrative" if getattr(client, "offline", False) else "observed"
    if experience["record_kind"] != kind:
        raise ContractError("Source and writer record kinds differ")
    model = getattr(client, "model", None)
    if kind == "observed" and (not model or model["id"] != model_config["model"] or not model.get("selected_endpoint")):
        raise ContractError("Observed writing requires the verified worker model and endpoint")
    ctx = RunContext(journal.experiment_id, run_id or new_id("r"), new_id("s"), condition_id,
                     "collection", "post_task_write", kind)
    memory = MemoryService(journal, record_kind=kind)
    bank = memory.create_bank(ctx.run_id, policy)
    config = dict(experience_ref=experience_ref, feedback=asdict(feedback), instruction=instruction,
                  model_config=deepcopy(model_config), policy=asdict(policy), max_requests=max_requests, bank_id=bank)
    state = deepcopy(journal.state)
    state.setdefault("memory_writes", {})[ctx.run_id] = config
    journal.append([ctx.at("prepare").event("run.started", dict(bank_id=bank, start_snapshot_id=None,
        fresh_session=True, source_run_ids=[experience["source_run_id"]], task_ref=experience["task_ref"]))], state=state)
    registry = SourceRegistry(journal, ctx)
    template = registry.template(dict(model_config, system=dict(role="system", content=instruction),
        tools=memory_tools(["memory_store"]), tool_choice="auto"), roles=("writer",))
    history = [(template, "/system")]
    for message in inputs:
        event = ctx.event("business.transition", dict(origin="memory_writer.input",
            experience_ref=experience_ref, public_message_ref=journal.blobs.put(message)))
        journal.append([event])
        history.append((registry.add("business_result", event["event_id"], roles=("writer",)), ""))
    router = ToolRouter(journal, memory, FrozenBusiness(), bank, policy)
    gateway, status, error_code = RecordingGateway(journal), "completed", None
    try:
        while True:
            message, source, request_id = gateway.call(ctx, registry, "writer", template, history, client,
                                                       max_requests=max_requests)
            if kind == "observed":
                raw = journal.blobs.get(registry.sources[source]["artifact_ref"])
                if (raw.get("model") not in {model["id"], model.get("canonical_slug")}
                        or raw.get("provider") != model["selected_endpoint"]["provider_name"]):
                    raise ContractError("Writer response model or provider differs from the verified worker")
            history.append((source, ""))
            calls = message.get("tool_calls") or []
            if not calls:
                break
            for _, source_kind, event_id in router.dispatch(ctx, request_id, calls):
                history.append((registry.add(source_kind, event_id, roles=("writer",)), ""))
    except Exception as error:
        status, error_code = "error", getattr(error, "code", type(error).__name__)
        journal.append([ctx.event("runtime.error", dict(code=error_code))])
    snapshot = memory.freeze(bank)
    memory.seal(bank)
    stores = sum(e["run_id"] == ctx.run_id and e["type"] == "memory.stored" for e in journal.events.values())
    if status == "completed" and not stores:
        status = "empty"
    journal.append([ctx.at("seal").event("run.ended", dict(termination_reason="memory_write_" + status,
        grading_status="not_available", end_snapshot_id=snapshot))])
    return dict(run_id=ctx.run_id, bank_id=bank, snapshot_id=snapshot, status=status,
                memory_stores=stores, error_code=error_code, experience_ref=experience_ref,
                feedback_level=feedback.level,
                unknown_cost=any(e["run_id"] == ctx.run_id and e["type"] == "model.request_finished"
                    and e["payload"]["cost_status"] == "unknown" for e in journal.events.values()))
