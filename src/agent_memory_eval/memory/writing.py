"""Write isolated memories from one closed run's public observations."""

from copy import deepcopy
from dataclasses import asdict, dataclass

from agent_memory_eval.contracts.core import ContractError, RunContext, canonical, new_id
from agent_memory_eval.memory.router import ToolRouter
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry


WRITER_MODEL_FIELDS = frozenset({
    "model", "temperature", "seed", "reasoning", "provider", "stream", "max_completion_tokens", "max_tokens",
})
WRITER_COMPLETION_CONTRACT = "writer-finish/v1"
REQUIRED_STORE_CONTRACT = "writer-required-store/v1"
WRITER_CONTRACTS = (WRITER_COMPLETION_CONTRACT, REQUIRED_STORE_CONTRACT)


def writer_reply_error(response, *, require_finish_reason):
    """Classify an incomplete reply before dispatching any of its tool calls."""
    choice = response["choices"][0]
    reason = choice.get("finish_reason")
    if reason == "length":
        return "WRITER_OUTPUT_TRUNCATED"
    if reason == "content_filter":
        return "WRITER_CONTENT_FILTERED"
    if reason is None and not require_finish_reason:
        return None  # Historical synthetic transports omit provider metadata.
    if not isinstance(reason, str) or reason not in {"stop", "tool_calls"}:
        return "WRITER_INVALID_FINISH_REASON"
    if reason == "tool_calls" and not choice["message"].get("tool_calls"):
        return "WRITER_INCOMPLETE_TOOL_CALLS"
    return None


def writer_response_error(response, *, model_identity, observed):
    if observed:
        aliases = {v for v in (model_identity["id"], model_identity.get("canonical_slug")) if isinstance(v, str) and v}
        if (not isinstance(response.get("model"), str) or response["model"] not in aliases
                or response.get("provider") != model_identity["provider_name"]):
            return "WRITER_MODEL_MISMATCH"
    return writer_reply_error(response, require_finish_reason=observed)


class IncompleteWriterReply(ContractError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


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
    if value.get("format") == "agent-memory-public-experience/v2":
        from agent_memory_eval.recording.experience import read_imported_experience
        return read_imported_experience(blobs, scan, value)
    if value.get("format") == "agent-memory-forms-public-experience/v1":
        from agent_memory_eval.experiments.forms_write import read_form_experience
        return read_form_experience(blobs, scan, value)
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
        outcome = (experience["recorded_outcome"] if experience["format"] == "agent-memory-public-experience/v2"
                   else scan["events"][experience["outcome_event_id"]]["payload"])
        if outcome["grading_status"] != "graded" or type(outcome["official_success"]) is not bool:
            raise ContractError("Result feedback requires an available source outcome")
        result["success"] = outcome["official_success"]
    if feedback.level == "critique":
        result["critique"] = feedback.critique
    return [dict(role="user", content=canonical(value).decode()) for value in (context, result)]


def writer_template(model_config, instruction, *, completion_contract=WRITER_COMPLETION_CONTRACT):
    if (set(model_config) - WRITER_MODEL_FIELDS or not isinstance(model_config.get("model"), str)
            or not isinstance(instruction, str) or not instruction.strip()):
        raise ContractError("Unexpected writer model configuration or instruction")
    if completion_contract not in WRITER_CONTRACTS:
        raise ContractError("Unknown writer completion contract")
    return dict(deepcopy(model_config), system=dict(role="system", content=instruction),
                tools=memory_tools(["memory_store"]),
                tool_choice="required" if completion_contract == REQUIRED_STORE_CONTRACT else "auto")


class FrozenBusiness:
    ended, steps, stop_tools = True, 0, frozenset()


class UnknownWriterCost(ContractError):
    code = "UNKNOWN_COST_STOP"


def write_from_experience(journal, experience_ref, feedback, *, instruction, policy, client,
                          model_config, max_requests, condition_id, run_id=None, execution_limits=None,
                          completion_contract=WRITER_COMPLETION_CONTRACT):
    """No collection rerun, source grading artifact, business tools or shared writer session."""
    if (not policy.enabled or policy.write_timing != "post_task" or policy.read_mode != "none"
            or policy.same_episode_read or not isinstance(instruction, str) or not instruction.strip()
            or (execution_limits is None and (type(max_requests) is not int or max_requests < 1))
            or (execution_limits is not None and max_requests is not None)):
        raise ContractError("Writing requires explicit post-task-only policy, instruction and request limit")
    template_config = writer_template(model_config, instruction, completion_contract=completion_contract)
    experience = read_public_experience(journal.blobs, journal.scan, experience_ref)
    inputs = writer_inputs(journal.blobs, journal.scan, experience, feedback)
    kind = "illustrative" if getattr(client, "offline", False) else "observed"
    if experience["record_kind"] != kind:
        raise ContractError("Source and writer record kinds differ")
    model = getattr(client, "model", None)
    if kind == "observed" and (not model or model["id"] != model_config["model"] or not model.get("selected_endpoint")):
        raise ContractError("Observed writing requires the verified worker model and endpoint")
    if (kind == "observed" and execution_limits is not None
            and getattr(client, "max_input_bytes", None) != execution_limits.max_input_bytes):
        raise ContractError("Writer transport and declared input limits differ")
    if run_id is not None and (not isinstance(run_id, str) or not run_id or run_id in journal.scan["starts"]):
        raise ContractError("Writer run ID must be new and nonempty")
    ctx = RunContext(journal.experiment_id, run_id or new_id("r"), new_id("s"), condition_id,
                     "collection", "post_task_write", kind)
    memory = MemoryService(journal, record_kind=kind)
    bank = memory.create_bank(ctx.run_id, policy)
    verified_model = (dict(id=model["id"], canonical_slug=model.get("canonical_slug"),
        provider_name=model["selected_endpoint"]["provider_name"]) if kind == "observed" else None)
    config = dict(response_projection="public-assistant/v1", completion_contract=completion_contract,
                  verified_model=verified_model,
                  experience_ref=experience_ref,
                  feedback=asdict(feedback), instruction=instruction,
                  model_config=deepcopy(model_config), policy=asdict(policy), max_requests=max_requests, bank_id=bank)
    if execution_limits is not None:
        config["execution_limits"] = asdict(execution_limits)
        config["execution_contract"] = "per-role-limits/v1"
    state = deepcopy(journal.state)
    state.setdefault("memory_writes", {})[ctx.run_id] = config
    journal.append([ctx.at("prepare").event("run.started", dict(bank_id=bank, start_snapshot_id=None,
        fresh_session=True, source_run_ids=[experience["source_run_id"]], task_ref=experience["task_ref"]))], state=state)
    registry = SourceRegistry(journal, ctx)
    template = registry.template(template_config, roles=("writer",))
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
                                                       max_requests=max_requests, project_self_authored=True,
                                                       execution_limits=execution_limits)
            raw = journal.blobs.get(registry.sources[source]["artifact_ref"])
            completion_error = writer_response_error(raw, model_identity=verified_model, observed=kind == "observed")
            if completion_error:
                raise IncompleteWriterReply(completion_error)
            history.append((source, ""))
            calls = message.get("tool_calls") or []
            unknown = journal.events[registry.sources[source]["producer_event_id"]]["payload"]["cost_status"] == "unknown"
            if not calls:
                if unknown:
                    raise UnknownWriterCost("Writer completion has an unverified cost")
                if completion_contract == REQUIRED_STORE_CONTRACT:
                    raise IncompleteWriterReply("WRITER_REQUIRED_STORE_MISSING")
                break
            for _, source_kind, event_id in router.dispatch(ctx, request_id, calls):
                history.append((registry.add(source_kind, event_id, roles=("writer",)), ""))
            if unknown:
                raise UnknownWriterCost("Writer response has an unverified cost")
            if completion_contract == REQUIRED_STORE_CONTRACT and journal.state["banks"][bank]["heads"]:
                break
    except Exception as error:
        status, error_code = "error", getattr(error, "code", type(error).__name__)
        if getattr(error, "recorded_event_id", None) not in journal.events:
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
