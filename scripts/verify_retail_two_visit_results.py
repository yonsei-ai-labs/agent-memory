"""Reconcile the original and supplemented retail experiment against the two closed journals."""

import argparse
from collections import defaultdict
from fractions import Fraction
from math import isclose
from pathlib import Path

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from scripts.analyze_full_retail import require
from scripts.audit_retail_order_choice import publish
from scripts.audit_retail_two_visit_billing import audit as audit_billing
from scripts.audit_retail_two_visit_inputs import audit as audit_inputs
from scripts.audit_retail_two_visit_sessions import audit as audit_sessions
from scripts.replay_full_retail_fields import reference_state
from scripts.run_full_retail import modules_for
from scripts.run_retail_two_visit import read, read_receipt
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


def interrupted_states(folder):
    """Recover the retail DB at the recorded interruption without inventing a later customer response."""
    modules_for(ROOT)
    from tau2.domains.retail.environment import get_environment

    reg = read(folder / "registration.json")
    cohort = read(ROOT / reg["design"]["cohort"]["path"])
    expected = {r["customer"]: r["expected_db"] for r in cohort["reserved"]}
    recovered = []
    for job in reg["jobs"]["continuation"]:
        path = folder / job["run"]
        receipt = read(path / "continuation.json")
        if receipt["status"] != "error":
            continue
        require(read_receipt(path, "continuation") == receipt, "Interrupted journal differs from receipt")
        scan, _, experiment = read_experiment(path)
        events = list(scan["events"].values())
        require(
            experiment == job["run"] and not any(e["type"] == "outcome.scored" for e in events),
            "Interrupted episode already has a score or a different run ID",
        )
        blobs = BlobStore(path)
        actions, cancellations = [], []
        requests = {}
        latest_worker = None
        for event in events:
            payload = event["payload"]
            if event["type"] == "model.request_started":
                requests[payload["request_id"]] = payload
                if payload["actor_role"] == "worker":
                    latest_worker = payload
            if event["type"] != "business.transition" or "action" not in payload:
                continue
            name = payload["action"]
            actions.append({"name": name, "arguments": payload["arguments"], "requestor": "assistant"})
            if name == "cancel_pending_order":
                result = read_json(blobs.get(payload["public_message_ref"])["content"].encode())
                require(
                    result["order_id"] == payload["arguments"]["order_id"] and result["status"] == "cancelled",
                    "Missing cancellation receipt",
                )
                preceding = blobs.get(latest_worker["request_ref"])["messages"][-2:]
                cancellations.append({
                    "target": result["order_id"] == job["target"],
                    "reason": result["cancel_reason"],
                    "tool_result_sha256": payload["public_message_ref"]["sha256"],
                    "preceding_roles": [m["role"] for m in preceding],
                    "preceding_messages_sha256": digest(canonical(preceding)),
                })
        hashes, errors = reference_state(get_environment(), actions)
        require(not errors, "An interrupted episode's recorded business action did not replay")
        end = events[-1]
        require(
            end["type"] == "model.request_finished" and end["payload"]["status"] == "error",
            "Missing terminal provider error",
        )
        failure = end["payload"]
        response = blobs.get(failure["response_ref"]) if failure["response_ref"] else {}
        recovered.append({
            "run": job["run"], "journal_sha256": receipt["journal_sha256"], "error": receipt["provider_error"],
            "failed_role": requests[failure["request_id"]]["actor_role"],
            "error_generation_id_available": bool(response.get("id")),
            "error_usage_available": response.get("usage") is not None,
            "observed_actions_after_authentication": [a["name"] for a in actions],
            "observed_cancellations": cancellations,
            "db_already_matches_target": hashes[0] == expected[job["customer"]][job["target"]],
            "official_terminal_score": None,
        })
    return recovered


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
        inputs = audit_inputs(folder, public, pure_folder=main if folder == recovery else None)
        generations, sessions = submitted_ids(folder, local_jobs)
        require(len(generations) == billing["verified_distinct_provider_ids"], "Different provider identities")
        require(len(sessions) == inputs["different_sessions"], "Different number of audited fresh sessions")
        ids.append((generations, sessions))
        evidence[name] = {
            "billing": billing,
            "inputs": inputs,
            "episodes": {s: audit_sessions(ROOT, folder, s, public) for s in ("source", "writer", "continuation")},
            "interrupted_consultations": interrupted_states(folder),
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
