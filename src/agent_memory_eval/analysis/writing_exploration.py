"""Recompute writing allocations and generated-snapshot eligibility from saved evidence."""

from dataclasses import asdict

from agent_memory_eval.analysis.memory_writing import audit_memory_writes
from agent_memory_eval.analysis.preparation import audit_preparations
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy
from agent_memory_eval.contracts.writing import writer_policy, writing_allocations, writing_profile
from agent_memory_eval.memory.preparation import SnapshotBudgetExceeded, snapshot_replay_input
from agent_memory_eval.memory.writing import WRITER_COMPLETION_CONTRACT
from agent_memory_eval.recording.journal import BlobStore, read_experiment


def audit_writing(root):
    evidence = read_experiment(root)
    scan, state, experiment = evidence
    blobs = BlobStore(root)
    runtime = state["writing_exploration"]
    control = blobs.get(runtime["control_ref"])
    profile = control["profile"]
    writing_profile(profile, require_ready=True)
    identity = control["implementation"]
    if (identity["worktree_has_uncommitted_implementation"] or not identity["implementation_commit"]
            or not control["model"].get("selected_endpoint")):
        raise ContractError("Writing lacks its committed implementation or verified model endpoint")
    writes, preparations = audit_memory_writes(root), audit_preparations(root)
    by_run = {w["run_id"]:w for w in writes["writes"]}
    expected = {a["run_id"]:a for a in control["schedule"]}
    results = runtime["results"]
    if (len(expected) != 2 * profile["repetitions"] or len(results) != len(expected)
            or [r["run_id"] for r in results] != [a["run_id"] for a in control["schedule"]]
            or {r["run_id"] for r in results if r["status"] != "skipped"} != set(by_run)):
        raise ContractError("Writing allocations, results and actual writers differ")
    if [{k:a[k] for k in ("feedback_level", "repetition")} for a in control["schedule"]] != writing_allocations(profile):
        raise ContractError("Writing feedback order differs from the fixed schedule seed")
    policy = writer_policy(profile)
    read_policy = MemoryPolicy(True, "none", False, policy.page_size, read_mode="preload", **profile["bank_limits"])
    eligible, proposed, empty_without_calls, empty_after_calls = 0, 0, 0, 0
    skipped_suffix = False
    for result in results:
        run = result["run_id"]
        allocation = expected[run]
        if any(result[k] != allocation[k] for k in ("feedback_level", "repetition")):
            raise ContractError("Writing result changed its allocation")
        if result["status"] == "skipped":
            skipped_suffix = True
            if run in scan["starts"] or not result.get("reason"):
                raise ContractError("Skipped allocation has an attempted writer or no stop reason")
            continue
        if skipped_suffix:
            raise ContractError("A stopped allocation was followed by another attempted writer")
        report, config = by_run[run], state["memory_writes"][run]
        status = report["termination_reason"].removeprefix("memory_write_")
        events = [e for e in scan["events"].values() if e["run_id"] == run]
        calls = [call for e in events if e["type"] == "model.request_finished" and e["payload"]["status"] == "response"
                 for call in blobs.get(e["payload"]["response_ref"])["choices"][0]["message"].get("tool_calls") or []
                 if call["function"]["name"] == "memory_store"]
        proposed += len(calls)
        empty_without_calls += status == "empty" and not calls
        empty_after_calls += status == "empty" and bool(calls)
        errors = [e["payload"]["code"] for e in events if e["type"] == "runtime.error"]
        unknown = any(e["type"] == "model.request_finished" and e["payload"]["cost_status"] == "unknown" for e in events)
        end = next(e for e in scan["events"].values() if e["run_id"] == run and e["type"] == "run.ended")
        if (result["status"] != status or result["memory_stores"] != report["memory_stores"]
                or result["bank_id"] != config["bank_id"] or result["experience_ref"] != control["experience_ref"]
                or result["error_code"] != (errors[-1] if errors else None) or result["unknown_cost"] is not unknown
                or result["snapshot_id"] != end["payload"]["end_snapshot_id"]
                or config["experience_ref"] != control["experience_ref"] or config["policy"] != asdict(policy)
                or config["feedback"] != dict(level=allocation["feedback_level"], critique=None, critique_origin=None)
                or config["model_config"] != control["model_config"] or config["instruction"] != control["instructions"]["writer"]
                or config.get("completion_contract") != WRITER_COMPLETION_CONTRACT
                or config.get("execution_limits") != profile["execution_limits"]):
            raise ContractError("Writer differs from the frozen exploration profile")
        model = control["model"]
        if config["verified_model"] != dict(id=model["id"], canonical_slug=model.get("canonical_slug"),
                provider_name=model["selected_endpoint"]["provider_name"]):
            raise ContractError("Writer response identity differs from the verified control")
        for event in events:
            if event["type"] != "model.request_finished" or event["payload"]["status"] != "response":
                continue
            raw = blobs.get(event["payload"]["response_ref"])
            pinned = (isinstance(raw.get("model"), str) and raw["model"] in {model["id"], model.get("canonical_slug")}
                      and raw.get("provider") == model["selected_endpoint"]["provider_name"])
            if not pinned and status != "error":
                raise ContractError("A writer accepted a response from another model or provider")
        try:
            planned = snapshot_replay_input(blobs, state, control["decision_ref"], result["snapshot_id"],
                policy=read_policy, instruction=control["instructions"]["replay"],
                max_input_bytes=profile["execution_limits"]["max_input_bytes"])
        except SnapshotBudgetExceeded:
            wanted = False
            if result.get("snapshot_preflight_error") != "SNAPSHOT_BUDGET_EXCEEDED" or "snapshot_preparation_run_id" in result:
                raise ContractError("Snapshot budget failure was omitted or changed") from None
        else:
            wanted = status in {"empty", "completed"} and planned["fits_input_limit"]
            prep = state["request_preparations"][result["snapshot_preparation_run_id"]]
            prep_config = blobs.get(prep["configuration_ref"])
            if prep_config["snapshot_id"] != result["snapshot_id"] or prep_config["decision_ref"] != control["decision_ref"]:
                raise ContractError("Generated snapshot preflight used a different source or memory")
        if result["replay_eligible"] is not wanted:
            raise ContractError("Replay eligibility hides an error or an oversized generated snapshot")
        eligible += wanted
    skipped = sum(r["status"] == "skipped" for r in results)
    status = "stopped" if skipped else "completed"
    if runtime["status"] != status or writes["request_audit"]["requests_by_role"].keys() - {"writer"}:
        raise ContractError("Exploration is incomplete or called a worker/user simulator")
    return dict(status=status, experiment_id=experiment, allocations=len(results), skipped=skipped,
        completed_writes=sum(r["status"] == "completed" for r in results),
        empty_writes=sum(r["status"] == "empty" for r in results), errors=sum(r["status"] == "error" for r in results),
        empty_without_store_calls=empty_without_calls, empty_after_store_calls=empty_after_calls,
        proposed_memory_store_calls=proposed,
        replay_eligible_snapshots=eligible, request_audit=writes["request_audit"],
        preparations=len(preparations["preparations"]), scope="writing_only_no_memory_effect_measurement")
