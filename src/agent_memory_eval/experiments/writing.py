"""Bounded writing-only exploration composed from existing memory/recording APIs."""

from copy import deepcopy
from pathlib import Path
import os
from tempfile import TemporaryDirectory
import time

from agent_memory_eval.contracts.connection import provider_route, validate_profile
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, new_id, read_json
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.contracts.writing import writer_policy, writing_allocations, writing_profile
from agent_memory_eval.memory.preparation import SnapshotBudgetExceeded, prepare_snapshot_replay, prepare_writer_request
from agent_memory_eval.memory.writing import WriterFeedback, read_public_experience, write_from_experience
from agent_memory_eval.models.openrouter import (
    OpenRouterClient, SpendGuard, model_catalog, model_endpoints, resolve_endpoint, resolve_model,
)
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.experience import capture_recorded_context, import_recorded_experience
from agent_memory_eval.recording.journal import Journal, safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity


def _inputs(journal, profile, root):
    connection = read_json(asset_text(root, profile["connection_profile_ref"]).encode())
    validate_profile(connection)
    instructions = {key:asset_text(root, ref) for key,ref in profile["instructions"].items()}
    source = profile["source"]
    source_root = safe_path(root, source["path"])
    experience = import_recorded_experience(journal, source_root,
        source_run_id=source["run_id"], log_sha256=source["log_sha256"])
    decision = capture_recorded_context(journal, source_root, source_run_id=source["run_id"],
        log_sha256=source["log_sha256"], request_id=source["decision_request_id"])
    context = read_public_experience(journal.blobs, journal.scan, experience)
    config = dict(model=connection["worker"]["model_id"], **deepcopy(connection["worker"]["parameters"]),
        provider=deepcopy(provider_route(connection, "worker")), stream=False)
    preparations = [prepare_writer_request(journal, experience, WriterFeedback(level),
        instruction=instructions["writer"], model_config=config, condition_id="preflight-" + level,
        max_input_bytes=profile["execution_limits"]["max_input_bytes"]) for level in profile["feedback_levels"]]
    return dict(experience_ref=experience, decision_ref=decision, instructions=instructions,
        model_config=config, record_kind=context["record_kind"], preparations=preparations)


def preflight_writing(profile, root, *, require_ready=False):
    plan = writing_profile(profile, require_ready=require_ready)
    with TemporaryDirectory() as folder, Journal(folder, "writing-preflight", contract_version="1.2") as journal:
        inputs = _inputs(journal, profile, Path(root).resolve())
    sizes = [dict(feedback_level=level, input_bytes=value["input_bytes"], fits_input_limit=value["fits_input_limit"],
                  request_sha256=value["request_ref"]["sha256"])
             for level,value in zip(profile["feedback_levels"], inputs["preparations"])]
    fits = all(value["fits_input_limit"] is True for value in sizes)
    if require_ready and (not fits or inputs["record_kind"] != "observed"):
        raise ContractError("Observed writing requires fitting initial requests and an observed source")
    return dict(local_ready=plan["ready"] and fits and inputs["record_kind"] == "observed", live_ready=False,
        unresolved_fields=plan["unresolved_fields"], initial_requests=sizes, model_config=inputs["model_config"],
        source_record_kind=inputs["record_kind"], current_endpoint_verified=False, new_model_or_catalog_calls=0,
        scope="writing_only_no_memory_effect_measurement")


def _stopped(guard):
    if guard.calls >= guard.max_calls:
        return "CALL_LIMIT"
    if time.monotonic() >= guard.deadline:
        return "TIME_LIMIT"
    if guard.unknown_cost:
        return "UNKNOWN_COST_STOP"
    if guard.reported_usd >= guard.max_usd:
        return "SPEND_LIMIT"
    return None


def run_writing(profile, output, root, *, opener=None):
    root, output = Path(root).resolve(), Path(output).absolute()
    writing_profile(profile, require_ready=True)
    if root != ROOT.resolve():
        raise ContractError("Implementation root differs from the executing checkout")
    if output.exists() or output.resolve().is_relative_to(safe_path(root, profile["source"]["path"]).resolve()):
        raise ContractError("Writing requires a new output directory outside its source")
    if output.is_relative_to(root) and output.relative_to(root).parts[0] not in {"runs", "outputs"}:
        raise ContractError("Repository output must be in ignored runs/ or outputs/")
    identity = implementation_identity(root)
    preflight = preflight_writing(profile, root, require_ready=True)
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ContractError("OPENROUTER_API_KEY_MISSING")
    config = preflight["model_config"]
    parameters = {k:v for k,v in config.items() if k not in {"model", "provider", "stream"}}
    parameters.update(tools=[], tool_choice="auto")
    model = resolve_model(model_catalog(), config["model"], parameters)
    model = resolve_endpoint(model, model_endpoints(model["id"]), config["provider"], parameters)
    if implementation_identity(root) != identity:
        raise ContractError("Implementation changed during preparation")
    with Journal(output, new_id("writing"), contract_version="1.2") as journal:
        inputs = _inputs(journal, profile, root)
        if (inputs["model_config"] != config or [v["request_ref"]["sha256"] for v in inputs["preparations"]]
                != [v["request_sha256"] for v in preflight["initial_requests"]]):
            raise ContractError("Inputs changed after local preflight")
        if implementation_identity(root) != identity:
            raise ContractError("Implementation changed before the run was frozen")
        schedule = [dict(row, run_id=new_id("writer")) for row in writing_allocations(profile)]
        control = dict(profile=deepcopy(profile), implementation=identity, model=model, schedule=schedule,
            experience_ref=inputs["experience_ref"], decision_ref=inputs["decision_ref"],
            model_config=config, instructions=inputs["instructions"], preflight=preflight)
        state = deepcopy(journal.state)
        state["writing_exploration"] = dict(control_ref=journal.blobs.put(control), results=[], status="running")
        journal.append([], state=state)
        limits, policy = ExecutionLimits(**profile["execution_limits"]), writer_policy(profile)
        guard = SpendGuard(max_calls=profile["guards"]["max_model_requests"],
            max_usd=profile["guards"]["max_reported_cost_usd"], max_seconds=profile["guards"]["max_seconds"])
        client = OpenRouterClient(model, guard, route=config["provider"], max_input_bytes=limits.max_input_bytes,
            timeout_seconds=profile["timeout_seconds"], **({"opener":opener} if opener else {}))
        stopped = None
        for allocation in schedule:
            stopped = stopped or _stopped(guard)
            if stopped:
                result = dict(allocation, status="skipped", reason=stopped)
            else:
                written = write_from_experience(journal, inputs["experience_ref"], WriterFeedback(allocation["feedback_level"]),
                    instruction=inputs["instructions"]["writer"], model_config=config, policy=policy,
                    client=client, condition_id=allocation["feedback_level"], run_id=allocation["run_id"],
                    max_requests=None, execution_limits=limits)
                result = dict(written, repetition=allocation["repetition"], replay_eligible=False)
                read_policy = MemoryPolicy(True, "none", False, policy.page_size, read_mode="preload", **profile["bank_limits"])
                try:
                    prepared = prepare_snapshot_replay(journal, inputs["decision_ref"], written["snapshot_id"],
                        policy=read_policy, instruction=inputs["instructions"]["replay"],
                        condition_id="size-check-" + allocation["run_id"], max_input_bytes=limits.max_input_bytes)
                    result["snapshot_preparation_run_id"] = prepared["run_id"]
                    result["replay_eligible"] = written["status"] in {"empty", "completed"} and prepared["fits_input_limit"]
                except SnapshotBudgetExceeded:
                    result["snapshot_preflight_error"] = "SNAPSHOT_BUDGET_EXCEEDED"
                if written["error_code"] in {"CALL_LIMIT", "TIME_LIMIT", "SPEND_LIMIT", "UNKNOWN_COST_STOP",
                        "ContractError", "WRITER_MODEL_MISMATCH", "WRITER_INVALID_FINISH_REASON"}:
                    stopped = written["error_code"]
            state = deepcopy(journal.state)
            state["writing_exploration"]["results"].append(result)
            journal.append([], state=state)
        state = deepcopy(journal.state)
        state["writing_exploration"]["status"] = "stopped" if any(r["status"] == "skipped" for r in state["writing_exploration"]["results"]) else "completed"
        journal.append([], state=state)
        return deepcopy(state["writing_exploration"])
