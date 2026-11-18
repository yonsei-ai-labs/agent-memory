"""Review strict search misses and link the same banks to previously graded full episodes."""

import argparse
import json
from pathlib import Path
import re

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path


def changed_date_review(outcome, profile, workspace):
    prior = read_json(asset_text(ROOT, profile["prior_protocol_ref"]).encode())
    parent = safe_path(workspace, prior["second_pass"]["path"])
    source_hash = prior["second_pass"]["opening"]["wire_sha256"]
    original = read_json(asset_text(parent, dict(path="blobs/sha256/" + source_hash, sha256=source_hash)).encode())
    as_of = re.search(r"The current time is (\d{4}-\d{2}-\d{2})", original["messages"][0]["content"])[1]
    date = profile["cases"]["changed_date"]["expected_search_date"]
    if as_of >= date:
        raise ContractError("Expected the requested date to follow the archived policy clock")
    expected = [dict(name="search_direct_flight", arguments=dict(origin="SEA", destination="JFK", date=date))]
    rows = [r for r in outcome["rows"] if r["case"] == "changed_date" and r["score"] is not None]
    misses = []
    for r in rows:
        score = r["score"]
        if not score["exact_action"]:
            misses.append(dict(slot=r["slot"], repeat=r["repeat"], arm=r["arm"], response_ref=r["response_ref"],
                called_correct_date=score["calls"] == expected, calls=score["calls"], text=score["public_text"]))
    return dict(clock_date=as_of, requested_date=date, scorable=len(rows), strict_exact=len(rows)-len(misses),
                correct_single_search_ignoring_text=sum(r["score"]["calls"] == expected for r in rows), strict_misses=misses)


def review(workspace, result_path):
    outcome = read_json(result_path.read_bytes())
    control = read_json((workspace / "runs" / outcome["study"] / "control.json").read_bytes())
    profile = control["profile"]
    if (profile["format"] != "agent-memory-format-content-decisions/v1" or
            digest((workspace / "runs" / outcome["study"] / "control.json").read_bytes()) != outcome["completion"]["control_sha256"]):
        raise ContractError("New decision data and its frozen source selection differ")
    base = safe_path(workspace, profile["writer_root"])
    inventory = read_json(asset_text(workspace, profile["writer_inventory_ref"]).encode())
    seal = read_json(asset_text(base, dict(path="completion.json", sha256=inventory["comparison_completion_sha256"])).encode())
    allocations = read_json(asset_text(base, dict(path="allocations.json", sha256=seal["allocation_sha256"])).encode())
    regrade_ref = dict(path="artifacts/analyses/20261118-new-customer-forms-final/native-regrade.json",
                       sha256="088368895ae3d862c1badbcab5aeb4dc589ea97e9c21b8d6a3403df4b37e4c79")
    regrade = read_json(asset_text(workspace, regrade_ref).encode())["comparison"]
    if regrade["allocation_sha256"] != seal["allocation_sha256"]:
        raise ContractError("Independent native regrading does not cover the sealed allocations")
    grades = {g["index"]: g for g in regrade["grades"]}
    old = [r for r in allocations if r["task_id"] == "24" and r["phase"] == "solve"]
    empties = {r["repetition"]: r for r in old if r["arm"] == "empty_preload"}
    banks = control["first_pass_evidence"]["banks"]
    rows = []
    for r in old:
        name = f"written_{r['arm']}_{r['repetition']}"
        if name not in banks:
            continue
        bank, result = banks[name], r["result"]
        root = safe_path(base, r["directory"])
        if digest((root / "log.jsonl").read_bytes()) != result["log_sha256"]:
            raise ContractError("Historical episode journal differs from its receipt")
        scan, state, _ = read_experiment(root)
        assignment = state["assignments"][r["run_id"]]
        pre, = [e for e in scan["events"].values() if e["type"] == "memory.preloaded" and e["run_id"] == r["run_id"]]
        blobs = BlobStore(root)
        items = read_json(blobs.get(pre["payload"]["public_message_ref"])["content"].encode())["items"]
        score, = [e["payload"] for e in scan["events"].values() if e["type"] == "outcome.scored" and e["run_id"] == r["run_id"]]
        grade = grades[r["index"]]
        baseline = empties[r["repetition"]]
        if (assignment["source_writer_run_id"] != bank["writer_run"] or assignment["source_writer_snapshot_id"] != bank["original_snapshot"]
                or items != bank["items"] or score["grading_status"] != "graded" or score["official_score"] != grade["official_score"]
                or grade["report_sha256"] != result["report_ref"]["sha256"] or score["official_success"] != result["official_success"]
                or grades[baseline["index"]]["official_score"] != baseline["result"]["official_score"]):
            raise ContractError("Historical bank, tool-visible preload or native result changed")
        rows.append(dict(arm=name, item_sha256=[digest(canonical(i)) for i in items], historical_job=r["index"],
            native_success=score["official_success"], native_db_match=grade["db_match"], historical_empty_job=baseline["index"],
            historical_empty_success=baseline["result"]["official_success"], log_sha256=result["log_sha256"],
            local_decisions={k: dict(exact=c[name]["exact"], scored=c[name]["scored"]) for k, c in outcome["counts"].items()}))
    if len(rows) != 8 or len({r["arm"] for r in rows}) != 8:
        raise ContractError("Expected one previously graded full episode for every original bank")
    return dict(scope="Post hoc inspection of changed-date replies and linkage to eight earlier full episodes. One historical episode per bank.",
                decision_results_sha256=digest(result_path.read_bytes()), historical_regrade_ref=regrade_ref,
                changed_date=changed_date_review(outcome, profile, workspace), historical_episodes=rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    if args.result and args.result.exists():
        parser.error("Use a new result file")
    output = review(args.workspace.resolve(), args.decisions)
    if args.result:
        write_json(args.result, output)
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2))
