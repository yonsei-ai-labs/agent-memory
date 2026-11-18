"""Verify the archived two-pass batch and reproduce its published result offline."""

import json
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.forms_results import summarize
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import endpoint_terms
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, safe_path


ROOT = Path(__file__).resolve().parents[1]
LOCK = "artifacts/pilots/20261118-two-pass-forms-reproduction-lock.json"


def archived_reward(report):
    """Recompute this batch's DB × COMMUNICATE reward from its recorded checks."""
    info = report["reward_info"]
    match = info["db_check"]["db_match"]
    if (info["reward_basis"] != ["DB", "COMMUNICATE"] or type(match) is not bool
            or info["communicate_checks"] is not None
            or info["info"]["communicate"] != {"note": "No communicate_info to evaluate"}):
        raise ContractError("Unexpected native reward basis or communication checks for this batch")
    db_reward = float(match)
    if (info["db_check"]["db_reward"] != db_reward
            or info["reward_breakdown"] != {"DB": db_reward, "COMMUNICATE": 1.0}
            or type(info["reward"]) not in (float, int) or info["reward"] != db_reward):
        raise ContractError("Native reward disagrees with its recorded DB or communication component")
    return db_reward


def native_scores(raw, rows):
    """Read the native reward for each graded episode in allocation order."""
    scores = []
    for row in rows:
        result = row.get("result", {})
        if row["phase"] != "solve" or result.get("grading_status") != "graded":
            continue
        report = BlobStore(safe_path(raw, row["directory"])).get(result["report_ref"])
        reward = archived_reward(report)
        if (report["task_id"] != row["task_id"]
                or reward != result["official_score"] or (reward == 1) is not result["official_success"]):
            raise ContractError("Allocation differs from its archived native reward")
        scores.append(dict(index=row["index"], run_id=row["run_id"], task_id=row["task_id"],
                           repetition=row["repetition"], arm=row["arm"], reward=reward,
                           report_sha256=result["report_ref"]["sha256"]))
    return scores


def review(root):
    lock = read_json((root / LOCK).read_bytes())
    if lock["format"] != "agent-memory-two-pass-forms-reproduction-lock/v1":
        raise ContractError("Unexpected two-pass reproduction lock")
    raw = safe_path(root, lock["planned_output"])
    completion_bytes = safe_path(raw, "completion.json").read_bytes()
    if digest(completion_bytes) != lock["completion_sha256"]:
        raise ContractError("Completion bytes differ from the historical run")
    profile = read_json(asset_text(root, lock["profile_ref"]).encode())
    control = read_json(safe_path(raw, "control.json").read_bytes())
    identity = control["implementation"]
    if (control["profile"] != profile or profile["output_root"] != lock["planned_output"]
            or identity["implementation_commit"] != lock["implementation_commit"]
            or profile["implementation_commit"] != lock["implementation_commit"]
            or identity["implementation_tree"] != lock["implementation_tree"]
            or identity["worktree_has_uncommitted_implementation"] is not False):
        raise ContractError("Archived deployment differs from the historical implementation or profile")
    assets = {}
    for name in ("design_ref", "preflight_ref", "research_ref", "endpoint_ref"):
        if profile[name] != lock[name]:
            raise ContractError("Deployment asset differs from the reproduction lock: " + name)
        assets[name] = asset_text(root, profile[name])
    design = read_json(assets["design_ref"].encode())
    preflight = read_json(assets["preflight_ref"].encode())
    if control["design"] != design or control["preflight"] != preflight["native_and_source"]:
        raise ContractError("The archived design or preflight differs from the deployment assets")
    endpoints = read_json(assets["endpoint_ref"].encode())
    if endpoint_terms(control["model_catalog_entries"]) != endpoint_terms(endpoints["models"]):
        raise ContractError("Archived role endpoints differ from the frozen selection")

    result = summarize(raw)
    expected = read_json(asset_text(root, lock["results_ref"]).encode())
    if canonical(result) != canonical(expected):
        raise ContractError("Recomputed batch differs from its complete published result")
    rows = read_json(safe_path(raw, "allocations.json").read_bytes())
    scores = native_scores(raw, rows)
    scores_sha256 = digest(canonical(scores))
    if scores_sha256 != lock["native_scores_sha256"]:
        raise ContractError("Individual native rewards differ from the historical score ledger")
    solves = [row for row in rows if row["phase"] == "solve"]
    usage = result["usage_by_role"]
    return dict(status="passed", completion_sha256=digest(completion_bytes),
                result_file_sha256=lock["results_ref"]["sha256"],
                result_canonical_sha256=digest(canonical(result)), native_scores_sha256=scores_sha256,
                writer_allocations=len(rows) - len(solves), solver_allocations=len(solves),
                graded_episodes=len(scores), successful_episodes=sum(s["reward"] == 1 for s in scores),
                ungraded_started_episodes=sum(r["status"] != "not_started" for r in solves) - len(scores),
                unstarted_episodes=sum(r["status"] == "not_started" for r in solves),
                recorded_http_calls=result["completion"]["http_calls"],
                known_cost_usd=str(sum(Decimal(u["reported_usd"]) for u in usage.values())),
                unpriced_requests=sum(u["unknown_count"] for u in usage.values()),
                recorded_stop_reason=result["completion"]["stop_reason"])


if __name__ == "__main__":
    print(json.dumps(review(ROOT), ensure_ascii=False, indent=2))
