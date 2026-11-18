"""Stage all four arms and run a frozen, bounded sequence of distinct worker inputs."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from agent_memory_eval.contracts.core import ContractError, RunContext, new_id, read_json
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.replay_inputs import import_inputs, load_bundle, verify_inputs
from agent_memory_eval.experiments.replay_schedule import local_snapshot, schedule, validate_profile
from agent_memory_eval.models.openrouter import OpenRouterClient, model_catalog, model_endpoints, resolve_endpoint, resolve_model
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.experience import ReplayContextReader
from agent_memory_eval.recording.journal import Journal, safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.runner.snapshot_replay import run_snapshot_replay
from agent_memory_eval.runner.tau2_baseline import batch_stop, spend_guard


def _record(journal, phase, **payload):
    ctx = RunContext(journal.experiment_id, record_kind="observed")
    journal.append([ctx.event("business.transition", dict(origin="written_batch." + phase, **payload))])


def _stage(journal, profile, root, preparation):
    if journal.contract_version != "1.2" or journal.scan["records"]:
        raise ContractError("A written replay batch requires a fresh v1.2 journal")
    bundle = load_bundle(profile, root, preparation)
    control = import_inputs(journal, profile, bundle, root)
    inputs = verify_inputs(journal.root, journal.blobs, journal.state, journal.events, control)
    _record(journal, "staged", control_ref=journal.blobs.put(control))
    return control, inputs


def _new_output(root, output, profile, preparation):
    comparison = read_json(asset_text(root, profile["comparison_ref"]).encode())
    receipt = read_json(asset_text(root, comparison["writing_result_ref"]).encode())
    sources = (safe_path(root, comparison["decision_source"]["path"]), safe_path(root, receipt["source_root"]), preparation)
    output = Path(output).absolute()
    if output != output.resolve() or any(output.is_relative_to(Path(source).resolve()) for source in sources):
        raise ContractError("Output must be outside the input bundles and must not traverse symlinks")
    if output.is_relative_to(root) and (output == root or output.relative_to(root).parts[0] not in {"runs", "outputs"}):
        raise ContractError("Repository batch outputs belong under runs/ or outputs/")
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ContractError("Batch output must be a new directory") from None
    return output


def stage_batch(profile, root, preparation, output=None):
    """With no destination this is a read-only preflight using a temporary staging journal."""
    root = Path(root).resolve()
    validate_profile(profile)
    if output is None:
        with TemporaryDirectory() as directory:
            return stage_batch(profile, root, preparation, Path(directory) / "preflight")
    destination = _new_output(root, output, profile, preparation)
    with Journal(destination, new_id("batch"), contract_version="1.2") as journal:
        _, inputs = _stage(journal, profile, root, preparation)
    return dict(status="staged", **inputs["summary"], input_groups=inputs["groups"], replay_model_requests=0, catalog_calls=0,
                business_tools_executed=False, policy_effect_estimated=False)


def run_batch(profile, root, preparation, output, *, opener=None):
    validate_profile(profile, require_ready=True)
    root = Path(root).resolve()
    if root != ROOT.resolve():
        raise ContractError("Live runtime and frozen checkout differ")
    identity = implementation_identity(root)
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ContractError("OPENROUTER_API_KEY_MISSING")
    destination = _new_output(root, output, profile, preparation)
    with Journal(destination, new_id("batch"), contract_version="1.2") as journal:
        control, inputs = _stage(journal, profile, root, preparation)
        config = inputs["model_config"]
        model = resolve_model(model_catalog(), config["model"], config["parameters"])
        model = resolve_endpoint(model, model_endpoints(model["id"]), config["provider"], config["parameters"])
        if implementation_identity(root) != identity:
            raise ContractError("Implementation changed before freezing the worker schedule")
        groups = {(g["case_id"], g["request_sha256"]):g for g in inputs["groups"]}
        plan = schedule(inputs["groups"], profile["repetitions"], profile["schedule_seed"])
        _record(journal, "started", freeze_ref=journal.blobs.put(dict(implementation=identity, model=model,
            schedule=plan, sampling_unit=profile["sampling_unit"])))
        limits = ExecutionLimits(1, 0, 0, profile["max_input_bytes"])
        started = time.monotonic()
        guard = spend_guard(profile)
        client = OpenRouterClient(model, guard, route=config["provider"], max_input_bytes=limits.max_input_bytes,
            timeout_seconds=profile["timeout_seconds"], **({"opener":opener} if opener is not None else {}))
        reader = ReplayContextReader()
        stop, executed = None, 0
        for row in plan:
            stop = batch_stop(guard)
            if stop:
                break
            group = groups[row["case_id"], row["request_sha256"]]
            first = group["members"][0]
            result = run_snapshot_replay(journal, control["decisions"][row["case_id"]], local_snapshot(control["catalog"], first),
                policy=inputs["policy"], instruction=control["instruction"], condition_id=first["arm"],
                client=client, execution_limits=limits, expected_request_sha256=row["request_sha256"],
                run_id=row["run_id"], context_reader=reader)
            executed += 1
            if result["stop_required"]:
                stop = "unknown_cost" if result["unknown_cost"] else result["error_code"]
                break
        _record(journal, "finished", stop_reason=stop, skipped_run_ids=[row["run_id"] for row in plan[executed:]],
                elapsed_seconds=time.monotonic() - started)
