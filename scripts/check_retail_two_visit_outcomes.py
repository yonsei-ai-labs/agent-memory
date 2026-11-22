"""Recount the registered order choice and completed consultation outcomes from original responses and turn labels."""

import argparse
from collections import Counter, defaultdict
from pathlib import Path

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.recording.journal import BlobStore
from scripts.analyze_full_retail import require
from scripts.analyze_retail_two_visit import public_turns
from scripts.audit_retail_order_choice import publish
from scripts.run_retail_two_visit import read
from scripts.run_retail_two_visit_recovery import verified_replacements


def first_choice(folder, receipt, target):
    blobs = BlobStore(folder)
    with (folder / "log.jsonl").open("rb") as stream:
        for line in stream:
            for event in read_json(line)["events"]:
                data = event["payload"]
                if event["type"] == "model.request_finished" and data["request_id"] == receipt["request"]:
                    message = blobs.get(data["response_ref"])["choices"][0]["message"]
                    calls = message.get("tool_calls") or []
                    if len(calls) != 1 or calls[0]["function"]["name"] != "get_order_details":
                        return False
                    arguments = read_json(calls[0]["function"]["arguments"].encode())
                    return arguments.get("order_id") == target
    raise ValueError("First decision has no original provider response")


def consultation(turns, labels, target, score):
    """Follow the proposal and consent in dialogue order, evaluating each successful cancellation."""
    require(labels["turns_sha256"] == digest(canonical(turns)), "Review and actual public dialogue differ")
    require(labels["status"] == "reviewed", "The recount needs a resolved review")
    proposals = {p["turn"]: p for p in labels["proposals"]}
    responses = {r["turn"]: r for r in labels["responses"]}
    executed = {
        turn["action"]
        for turn in turns
        if turn["role"] == "result" and turn["ok"] and turns[turn["action"]]["name"] == "cancel_pending_order"
    }
    active, consent = None, False
    qualified = bool(executed) and score == 1
    other_order = False
    for i, turn in enumerate(turns):
        if i in proposals:
            active, consent = proposals[i], False
        if i in responses:
            response = responses[i]
            consent = active and response["proposal"] == active["turn"] and response["disposition"] == "confirmed"
        if i in executed:
            args = turn["arguments"]
            other_order |= args["order_id"] != target
            qualified &= bool(
                active
                and consent
                and args["order_id"] == target == active["order"]
                and args["reason"] == "no longer needed" == active["reason"]
            )
    wrong = [i for i, p in proposals.items() if p["order"] != target]
    questions = len(labels["clarifications"])
    return {
        "completed": 1,
        "db_success": int(score == 1),
        "consent_qualified": int(qualified),
        "consultations_with_order_questions": int(questions > 0),
        "clarifications": questions,
        "consultations_with_wrong_proposals": int(bool(wrong)),
        "wrong_proposals": len(wrong),
        "corrected_proposals": sum(any(j > i and p["order"] == target for j, p in proposals.items()) for i in wrong),
        "other_order_cancelled": int(other_order),
    }


def check(main, supplement, original_reviews, recovery_reviews, published):
    archive, extra, result = read(original_reviews), read(recovery_reviews), read(published)
    require(archive["labels"].keys().isdisjoint(extra["labels"]), "Duplicate blind reviews")
    labels = {**archive["labels"], **extra["labels"]}
    jobs = read(main / "registration.json")["jobs"]["decision"]
    replacements = verified_replacements(main, supplement)
    groups = defaultdict(Counter)
    completed = set()
    observed = 0
    for job, decision, episode in zip(jobs, result["decisions"], result["continuations"], strict=True):
        replacement = replacements.get(job["run"])
        folder = supplement / replacement["run"] if replacement else main / job["run"]
        first = read(folder / "decision.json")
        require(all(decision[k] == episode[k] == job[k] for k in ("task", "arm", "mode")), "Wrong assignment")
        if first["status"] == "checkpoint":
            require(
                first_choice(folder, first, job["target"]) == (decision["behavior"] == "focused_target"),
                "First choice differs from the original response",
            )
            observed += 1
        finished = read(folder / "continuation.json")
        require(finished["status"] == episode["completion"], "Changed closure status")
        if finished["status"] != "completed":
            continue
        blind = archive["index"][job["run"]]
        require(blind == episode["review_id"] and blind not in completed, "Wrong blinded consultation")
        turns = public_turns(BlobStore(folder).get(finished["trajectory"]))
        counts = consultation(turns, labels[blind], job["target"], finished["db_reward"])
        require(finished["db_reward"] == episode["db_reward"], "DB score differs")
        for name in (
            "clarifications",
            "wrong_proposals",
            "corrected_proposals",
            "consent_qualified",
            "other_order_cancelled",
        ):
            require(counts[name] == episode[name], "Consultation recount differs: " + name)
        groups[job["mode"], job["arm"]].update(counts)
        completed.add(blind)
    require(completed == labels.keys(), "Some completed consultations escaped the recount")
    checked = 0
    for group in result["full_by_mode_arm"]:
        counts = groups.pop((group["mode"], group["arm"]))
        require(all(group[k] == v for k, v in counts.items()), "Group count differs from the independent recount")
        checked += len(counts)
    require(not groups, "Unreported group")
    return {
        "published_results_sha256": digest(published.read_bytes()),
        "first_provider_responses_checked": observed,
        "completed_consultations_checked": len(completed),
        "mode_arm_group_totals_checked": checked,
        "unmatched": 0,
        "model_requests_for_recount": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("run", "recovery", "original-reviews", "recovery-reviews", "results", "output"):
        parser.add_argument("--" + key, required=True, type=Path)
    a = parser.parse_args()
    publish(a.output, check(a.run, a.recovery, a.original_reviews, a.recovery_reviews, a.results))
