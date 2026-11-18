"""Reconstruct a full four-arm batch, including shared inputs and the unattempted suffix."""

from collections import Counter
from dataclasses import asdict
import math
from pathlib import Path

from agent_memory_eval.analysis.snapshot_replay import audit_snapshot_replays
from agent_memory_eval.analysis.written_comparison import screen_reply
from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.experiments.replay_inputs import verify_inputs
from agent_memory_eval.experiments.replay_schedule import local_snapshot, schedule, validate_profile
from agent_memory_eval.recording.journal import BlobStore, read_experiment


def _stop_reason(rows, skipped, elapsed, profile):
    if any(r["stop_required"] for r in rows[:-1]):
        raise ContractError("Batch continued after an error or unknown cost")
    submitted, cost = 0, 0.0
    for row in rows:
        if submitted >= profile["max_model_requests"] or cost >= profile["max_reported_cost_usd"]:
            raise ContractError("Batch continued after exhausting its global guard")
        submitted += row["delivery_status"] != "not_submitted"
        if row["cost_status"] == "observed":
            cost += row["reported_cost_usd"]
    if rows and rows[-1]["stop_required"]:
        return "unknown_cost" if rows[-1]["unknown_cost"] else rows[-1]["error_code"]
    if skipped:
        if submitted >= profile["max_model_requests"]:
            return "call_limit"
        if cost >= profile["max_reported_cost_usd"]:
            return "spend_limit"
        if elapsed >= profile["max_seconds"]:
            return "time_limit"
        raise ContractError("No recorded stop condition justifies skipping these allocations")
    return None


def _event_order(scan):
    phases = []
    for event in scan["events"].values():
        if event["run_id"] is not None:
            if event["run_id"] not in scan["starts"]:
                raise ContractError("Replay event has no run")
            continue
        if event["type"] == "bank.snapshot_created":
            continue
        origin = event["payload"].get("origin", "")
        if event["type"] != "business.transition" or not origin.startswith("written_batch.") or event["record_kind"] != "observed":
            raise ContractError("Unexpected controller event in a written replay batch")
        phases.append(event)
    names = [e["payload"]["origin"].removeprefix("written_batch.") for e in phases]
    if names not in (["staged"], ["staged", "started", "finished"]):
        raise ContractError("Incomplete or out-of-order written batch lifecycle")
    if scan["records"][-1]["events"] != [phases[-1]]:
        raise ContractError("Batch has changes after its terminal record")
    return phases


def audit_batch(root, *, expected_profile=None):
    root = Path(root)
    evidence = read_experiment(root)
    scan, state, experiment = evidence
    if scan["tail_bytes"] or scan["incomplete_runs"] or scan["pending_requests"]:
        raise ContractError("Incomplete written replay journal; preserve it for diagnosis")
    blobs = BlobStore(root)
    phases = _event_order(scan)
    control = blobs.get(phases[0]["payload"]["control_ref"])
    profile = control["profile"]
    if expected_profile is not None and profile != expected_profile:
        raise ContractError("Recorded batch differs from the supplied protocol")
    inputs = verify_inputs(root, blobs, state, scan["events"], control)
    catalog = control["catalog"]
    input_snaps = {*catalog["writers"].values(), *catalog["controls"].values()}
    early_snaps = [e["payload"]["snapshot_id"] for e in scan["events"].values()
                   if e["type"] == "bank.snapshot_created" and e["event_seq"] < phases[0]["event_seq"]]
    if len(early_snaps) != len(input_snaps) or set(early_snaps) != input_snaps:
        raise ContractError("Source snapshots must be produced before the batch is staged")
    if {s["bank_id"] for s in state["snapshots"].values()} != set(state["banks"]):
        raise ContractError("Batch contains a bank without a recorded snapshot")
    report = dict(format="agent-memory-written-replay-batch-audit/v1", experiment_id=experiment,
        status="staged", profile_sha256=digest(canonical(profile)), preparation_sha256=profile["preparation_sha256"],
        **inputs["summary"], replay_model_requests=0, submitted_requests=0, observed_replies=0,
        unknown_cost_requests=0, reported_replay_cost_usd_decimal="0", skipped_inputs=0, errors=0,
        stop_reason=None, official_scores_computed=False, business_tools_executed=False, policy_effect_estimated=False)
    if len(phases) == 1:
        if scan["starts"] or state.get("snapshot_replays") or set(state["snapshots"]) != input_snaps:
            raise ContractError("A staged-only batch contains execution evidence")
        return dict(report, input_groups=inputs["groups"])
    validate_profile(profile, require_ready=True)
    freeze = blobs.get(phases[1]["payload"]["freeze_ref"])
    plan = schedule(inputs["groups"], profile["repetitions"], profile["schedule_seed"])
    if plan != freeze["schedule"] or freeze["sampling_unit"] != profile["sampling_unit"]:
        raise ContractError("Frozen request order differs from the protocol")
    model = freeze["model"]
    identity = freeze["implementation"]
    config = inputs["model_config"]
    if (not identity.get("implementation_commit") or identity.get("worktree_has_uncommitted_implementation") is not False
            or model["id"] != config["model"] or config["provider"]["only"] != [model["selected_endpoint"]["tag"]]):
        raise ContractError("Frozen worker identity or route differs from the verified inputs")
    verified = dict(id=model["id"], canonical_slug=model.get("canonical_slug"), provider_name=model["selected_endpoint"]["provider_name"])
    if set(scan["starts"]) != set(state.get("snapshot_replays", {})):
        raise ContractError("Batch contains an unassigned run")
    rows = []
    if scan["starts"]:
        replay = audit_snapshot_replays(root, evidence=evidence)
        rows = replay["replays"]
        report.update(replay_model_requests=replay["model_requests"], unknown_cost_requests=replay["unknown_cost_requests"],
                      reported_replay_cost_usd_decimal=replay["observed_cost_usd_decimal"])
    if ([r["run_id"] for r in rows] != [p["run_id"] for p in plan[:len(rows)]]
            or any(scan["starts"][r["run_id"]]["event_seq"] <= phases[1]["event_seq"] for r in rows)
            or set(state["snapshots"]) != input_snaps | {r["snapshot_id"] for r in rows}):
        raise ContractError("Recorded runs are not the frozen prefix or contain an extra bank")
    ends = {e["run_id"]:e["event_seq"] for e in scan["events"].values() if e["type"] == "run.ended"}
    if any(ends[a["run_id"]] >= scan["starts"][b["run_id"]]["event_seq"] for a, b in zip(rows, rows[1:])):
        raise ContractError("Replay runs overlapped in a sequential batch")
    terminal = phases[-1]["payload"]
    skipped = [p["run_id"] for p in plan[len(rows):]]
    elapsed = terminal["elapsed_seconds"]
    if (skipped != terminal["skipped_run_ids"] or type(elapsed) not in (int, float) or not math.isfinite(elapsed)
            or elapsed < 0 or _stop_reason(rows, skipped, elapsed, profile) != terminal["stop_reason"]):
        raise ContractError("Recorded stop and skipped allocations disagree with observed evidence")
    by_input = {(g["case_id"], g["request_sha256"]):g for g in inputs["groups"]}
    by_case = {c["case_id"]:c for c in inputs["packet"]["cases"]}
    rules = {r["case_id"]:r for r in inputs["rubrics"]["cases"]}
    finishes = {e["run_id"]:e["payload"] for e in scan["events"].values() if e["type"] == "model.request_finished"}
    assignments, outcomes = [], []
    limits = asdict(ExecutionLimits(1, 0, 0, profile["max_input_bytes"]))
    for index, allocation in enumerate(plan):
        run, case = allocation["run_id"], allocation["case_id"]
        group = by_input[case, allocation["request_sha256"]]
        first = group["members"][0]
        result, screen = None, None
        if index < len(rows):
            result = rows[index]
            cfg = state["snapshot_replays"][run]
            if (cfg["decision_ref"] != control["decisions"][case] or cfg["snapshot_id"] != local_snapshot(catalog, first)
                    or cfg["verified_model"] != verified or cfg["expected_request_sha256"] != allocation["request_sha256"]
                    or cfg["policy"] != asdict(inputs["policy"]) or cfg["instruction"] != control["instruction"]
                    or cfg["execution_limits"] != limits or result["condition_id"] != first["arm"]
                    or result["record_kind"] != "observed"):
                raise ContractError("Replay used a different decision, input bank, model or execution limit")
            if result["status"] == "response":
                raw = blobs.get(finishes[run]["response_ref"])
                screen = screen_reply(by_case[case], rules[case], blobs.get(result["message_ref"]),
                                      finish_reason=raw["choices"][0]["finish_reason"])
        status = result["status"] if result else "skipped"
        skip_reason = None if result else terminal["stop_reason"]
        outcomes.append(dict(allocation, status=status, shared_by=len(group["members"]), screen=screen, skip_reason=skip_reason,
            error_code=result["error_code"] if result else None, request_id=result["request_id"] if result else None,
            message_ref=result["message_ref"] if result else None, delivery_status=result["delivery_status"] if result else None,
            cost_status=result["cost_status"] if result else None, reported_cost_usd=result["reported_cost_usd"] if result else None))
        for member in group["members"]:
            assignments.append(dict(member, **allocation, status=status, skip_reason=skip_reason,
                                    screen_status=screen["status"] if screen else None))
    return dict(report, status="stopped" if terminal["stop_reason"] is not None else "completed",
        stop_reason=terminal["stop_reason"], skipped_inputs=len(skipped), skipped_logical_rows=sum(a["status"] == "skipped" for a in assignments),
        submitted_requests=sum(r["delivery_status"] != "not_submitted" for r in rows),
        observed_replies=sum(r["status"] == "response" for r in rows), errors=sum(r["status"] == "error" for r in rows),
        screens_by_distinct_response=dict(Counter(r["screen"]["status"] for r in outcomes if r["screen"] is not None)),
        allocations=assignments, distinct_inputs=outcomes)
