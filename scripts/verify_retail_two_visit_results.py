"""Reconcile the original and supplemented retail experiment against the two closed journals."""

import argparse
from collections import defaultdict
from fractions import Fraction
from math import isclose
from pathlib import Path

from agent_memory_eval.contracts.core import digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from scripts.analyze_full_retail import require
from scripts.audit_retail_order_choice import publish
from scripts.audit_retail_two_visit_billing import audit as audit_billing
from scripts.audit_retail_two_visit_inputs import audit as audit_inputs
from scripts.audit_retail_two_visit_sessions import audit as audit_sessions
from scripts.run_retail_two_visit import read
from scripts.run_retail_two_visit_recovery import verified_replacements


def primary(result):
    """Recount the primary contrast from the individual decisions using rational customer weights."""
    arms = ("preload", "empty_preload")
    cases = defaultdict(dict)
    for row in result["decisions"]:
        if row["mode"] == "previous_only" and row["arm"] in arms and row["behavior"] != "unobserved":
            cases[row["customer"], row["task"]][row["arm"]] = row["behavior"] == "focused_target"
    customers = defaultdict(list)
    positives = dict.fromkeys(arms, 0)
    for (customer, _), pair in cases.items():
        if len(pair) == 2:
            for arm in arms:
                positives[arm] += pair[arm]
            customers[customer].append(pair["preload"] - pair["empty_preload"])
    means = [Fraction(sum(vals), len(vals)) for vals in customers.values()]
    average = sum(means) / len(means)
    counts = {
        "complete_pairs": sum(map(len, customers.values())),
        "customers_with_pairs": len(means),
        "gain": sum(v > 0 for v in means),
        "tie": means.count(0),
        "loss": sum(v < 0 for v in means),
    }
    reported = result["first"]["paired"]["previous_only/preload/empty_preload"]
    require(all(reported[k] == v for k, v in counts.items()), "Customer counts disagree")
    require(isclose(reported["difference"], float(average), abs_tol=1e-12), "Weighted primary disagrees")
    return {**counts, **positives, "customer_weighted_difference_fraction": str(average)}


def submitted_ids(folder, jobs):
    """Read actual provider generation IDs and first-choice session IDs directly from the journals."""
    generations = set()
    with (folder / "expense.jsonl").open("rb") as stream:
        for line in stream:
            generation = read_json(line).get("generation")
            if generation:
                require(generation not in generations, "Repeated provider generation")
                generations.add(generation)
    sessions = set()
    for job in jobs:
        log = folder / job["run"] / "log.jsonl"
        if not log.is_file():
            continue
        with log.open("rb") as stream:
            start = next(e for line in stream for e in read_json(line)["events"] if e["type"] == "run.started")
        require(start["run_id"] == job["run"] and start["payload"]["fresh_session"], "Unexpected target session")
        require(start["session_id"] not in sessions, "Repeated first-choice session")
        sessions.add(start["session_id"])
    return generations, sessions


def compare_rows(jobs, replacements, original, augmented):
    counts = {}
    for table in ("decisions", "continuations"):
        preserved = added = completed = 0
        for job, before, merged in zip(jobs, original[table], augmented[table], strict=True):
            after = dict(merged)
            eligible = job["run"] in replacements
            require(after.pop("recovery_slot") == eligible, "Unexpected recovery label")
            for field in ("mode", "arm", "customer", "task", "source", "position"):
                require(before[field] == after[field] == job[field], "Changed registered assignment")
            if eligible:
                require(before["decision_status"] == "bank_missing", "Replacement of an earlier decision")
                if table == "continuations":
                    require(before["completion"] == "decision_missing", "Replacement of an earlier consultation")
                    require(before["review_id"] == after["review_id"], "Changed blinded slot")
                    completed += after["completion"] == "completed"
                added += after["behavior"] != "unobserved"
            else:
                require(before == after, "Supplement modified an ineligible original row")
                preserved += 1
        counts[table] = {"unchanged_original_rows": preserved, "new_observed_first_decisions": added}
        if table == "continuations":
            counts[table]["new_completed_consultations"] = completed
    return counts


def verify(main, recovery, public, original_path, augmented_path):
    original, augmented = read(original_path), read(augmented_path)
    jobs = read(main / "registration.json")["jobs"]["decision"]
    replacements = verified_replacements(main, recovery)
    rows = compare_rows(jobs, replacements, original, augmented)
    ids = []
    evidence = {}
    for name, folder, result in (("original", main, original), ("supplement", recovery, augmented)):
        local_jobs = read(folder / "registration.json")["jobs"]["decision"]
        billing = audit_billing(folder)
        inputs = audit_inputs(folder, public, pure_folder=main)
        generations, sessions = submitted_ids(folder, local_jobs)
        require(len(generations) == billing["verified_distinct_provider_ids"], "Different provider identities")
        require(len(sessions) == inputs["different_sessions"], "Different number of audited fresh sessions")
        ids.append((generations, sessions))
        evidence[name] = {
            "billing": billing,
            "inputs": inputs,
            "episodes": {s: audit_sessions(ROOT, folder, s, public) for s in ("source", "writer", "continuation")},
            "primary_in_published_analysis": primary(result),
        }
    for old, new in zip(ids[0], ids[1], strict=True):
        require(old.isdisjoint(new), "A provider generation or target session was reused across cohorts")
    summary = read(recovery / "summary.json")
    return {
        "analysis_sha256": {
            "original": digest(original_path.read_bytes()),
            "augmented": digest(augmented_path.read_bytes()),
        },
        "eligible_recovery_slots": len(replacements),
        "overlay": rows,
        "distinct_target_sessions": sum(len(s) for _, s in ids),
        "distinct_provider_generations": sum(len(g) for g, _ in ids),
        "combined_confirmed_usd": summary["combined_confirmed_usd"],
        "combined_held_usd": summary["combined_held_usd"],
        "model_requests_for_verification": 0,
        "evidence": evidence,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("run", "recovery", "public", "original-results", "augmented-results", "output"):
        parser.add_argument("--" + argument, type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.run, args.recovery, args.public, args.original_results, args.augmented_results)
    publish(args.output, result)
