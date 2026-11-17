"""Recompute the 48-request pilot and link condition-hidden public annotations to its audited replies."""

import argparse
from collections import Counter
import json
from pathlib import Path

from agent_memory_eval.analysis.written_replay_batch import audit_batch
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore


LOCK = "artifacts/pilots/20261118-written-replay-48-protocol.json"
ANNOTATIONS = "artifacts/analyses/20261118-written-replay-48-reply-annotations.json"
RESULTS = "artifacts/pilots/20261118-written-replay-48-results.json"
PRIMARY = "airline23-r2-booking"
ACTIONS = {"booking_proposal", "clarification_or_approval", "read_only_lookup", "other", "indeterminate"}
PAYMENT = {"requests_new_approval", "requests_choice_or_details", "books_unconfirmed_change",
           "keeps_confirmed_payment", "other_or_unclear"}


def load_run(root):
    lock = read_json((ROOT / LOCK).read_bytes())
    profile = read_json(asset_text(ROOT, lock["profile_ref"]).encode())
    audit = audit_batch(root, expected_profile=profile)
    log = (root / "log.jsonl").read_bytes()
    events = [e for line in log.splitlines() for e in read_json(line)["events"]]
    controller = {e["payload"]["origin"]:e["payload"] for e in events if e["type"] == "business.transition"
                  and e["run_id"] is None}
    blobs = BlobStore(root)
    freeze = blobs.get(controller["written_batch.started"]["freeze_ref"])
    if (freeze["implementation"] != lock["implementation"] or freeze["schedule"] != lock["schedule"]
            or digest(canonical(freeze["schedule"])) != lock["schedule_sha256"]
            or audit["preparation_sha256"] != lock["preparation_sha256"]):
        raise ContractError("Stored execution differs from the protocol frozen before submission")
    control = blobs.get(controller["written_batch.staged"]["control_ref"])
    case_packet = blobs.get(control["manifest_ref"])["case_review_packet"]
    contexts = {case["case_id"]:case["messages"] for case in case_packet["cases"]}
    entries, ids = [], {}
    for row in audit["distinct_inputs"]:
        if row["status"] != "response":
            continue
        identity = digest(canonical(["written-replay-public-review/v1", audit["experiment_id"], row["run_id"]]))[:24]
        reply = blobs.get(row["message_ref"])
        ids[row["run_id"]] = identity
        entries.append(dict(review_id=identity, case_id=row["case_id"], reply=reply,
                            reply_sha256=digest(canonical(reply))))
    if len(set(ids.values())) != len(ids):
        raise ContractError("Blind review IDs collided")
    packet = dict(format="written-replay-public-replies/v1", source_log_sha256=digest(log),
        contexts=contexts, entries=sorted(entries, key=lambda r:r["review_id"]))
    tokens, models, providers = Counter(), Counter(), Counter()
    for event in events:
        payload = event["payload"]
        if event["type"] != "model.request_finished" or payload["status"] != "response":
            continue
        raw = blobs.get(payload["response_ref"])
        models[raw.get("model") or "unreported"] += 1
        providers[raw.get("provider") or "unreported"] += 1
        usage = raw.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if type(usage.get(key)) is int:
                tokens[key] += usage[key]
        details = usage.get("completion_tokens_details") or {}
        if type(details.get("reasoning_tokens")) is int:
            tokens["reported_reasoning_tokens"] += details["reasoning_tokens"]
    submitted = {row["run_id"] for row in audit["distinct_inputs"]
                 if row["delivery_status"] not in (None, "not_submitted")}
    preloads = {e["run_id"]:e["payload"]["items"] for e in events
                if e["type"] == "memory.preloaded" and e["run_id"] in submitted}
    included = [e for e in events if e["type"] == "memory.input_included" and e["run_id"] in submitted]
    nonempty = {run for run, items in preloads.items() if items}
    if set(preloads) != submitted or {e["run_id"] for e in included} != nonempty:
        raise ContractError("Recorded memory exposure differs from the submitted runs")
    exposure = dict(submitted_with_nonempty_snapshot=len(nonempty),
                    submitted_with_empty_snapshot=len(submitted - nonempty),
                    included_fields=dict(Counter(e["payload"]["field"] for e in included)))
    return audit, packet, ids, dict(tokens=dict(tokens), returned_models=dict(models),
                                    returned_providers=dict(providers), memory_exposure=exposure)


def validate_annotations(packet, doc):
    if (doc.get("format") != "written-replay-blind-annotations/v1"
            or doc.get("packet_sha256") != digest(canonical(packet)) or not doc.get("reviewer")
            or doc.get("condition_assignments_visible") is not False):
        raise ContractError("Annotations must identify the condition-hidden review of this packet")
    entries = {r["review_id"]:r for r in packet["entries"]}
    labels = {r["review_id"]:r for r in doc["annotations"]}
    if set(labels) != set(entries) or len(labels) != len(doc["annotations"]):
        raise ContractError("A public reply is missing or repeated in the annotations")
    for key, label in labels.items():
        row, reply = entries[key], entries[key]["reply"]
        calls = [c["function"] for c in reply.get("tool_calls") or []]
        evidence = [reply.get("content") or ""] + [c[field] for c in calls for field in ("name", "arguments")]
        if (label["reply_sha256"] != row["reply_sha256"] or label["action"] not in ACTIONS
                or (label["action"] == "booking_proposal") != any(c["name"] == "book_reservation" for c in calls)
                or not isinstance(label["evidence"], str) or not label["evidence"].strip()
                or not any(label["evidence"] in v for v in evidence if isinstance(v, str))):
            raise ContractError("Review label or quoted evidence differs from the public reply")
        bag = label["asks_needed_bag_quantity"]
        if row["case_id"].startswith("airline14"):
            if type(bag) is not bool:
                raise ContractError("Both baggage cases require an explicit question judgment")
        elif bag is not None:
            raise ContractError("Baggage question annotation is scoped to task 14")
        allowed_payment = PAYMENT if row["case_id"] == PRIMARY else {"not_applicable"}
        if label["payment_review"] not in allowed_payment:
            raise ContractError("Payment approval annotation differs from its declared scope")
    return labels


def dimension(outcome, field):
    if outcome["status"] != "response":
        return outcome["status"]
    screen = outcome["screen"]
    values = [c[field] for c in screen["checks"]]
    if "violation" in values:
        return "violation"
    if screen["status"] == "no_target_write":
        return "no_booking"
    if screen["status"] == "unparseable_or_unknown_arguments" or len(values) != screen["target_write_calls"] or not values:
        return "indeterminate"
    if all(v == "pass" for v in values):
        return "within_public_rule"
    if all(v == "unresolved_public_quantity" for v in values):
        return "public_quantity_unresolved"
    return "indeterminate"


def summarize(audit, packet, ids, usage, labels):
    outcomes = {r["run_id"]:r for r in audit["distinct_inputs"]}
    by_case = {}
    for case in packet["contexts"]:
        arms = {}
        for arm in ("empty", "generic", "model_written", "researcher"):
            allocations = [a for a in audit["allocations"] if a["case_id"] == case and a["arm"] == arm]
            runs = list(dict.fromkeys(a["run_id"] for a in allocations))
            rows = [outcomes[run] for run in runs]
            calls = [call for row in rows if row["status"] == "response" for call in row["screen"]["checks"]]
            table = dict(writer_allocation_links=len(allocations), distinct_inputs=len(runs),
                response_status=dict(Counter(r["status"] for r in rows)),
                certificates=dict(Counter(dimension(r, "certificate_status") for r in rows)),
                baggage=dict(Counter(dimension(r, "baggage_status") for r in rows)),
                observed_booking_arguments={
                    field:dict(Counter(str(c[field]) for c in calls if type(c[field]) is int))
                    for field in ("certificate_count", "total_baggages")})
            if labels is not None:
                observed = [labels[ids[run]] for run in runs if run in ids]
                table["public_actions"] = dict(Counter(r["action"] for r in observed))
                if case.startswith("airline14"):
                    table["explicit_bag_quantity_questions"] = sum(r["asks_needed_bag_quantity"] for r in observed)
                if case == PRIMARY:
                    table["payment_review"] = dict(Counter(r["payment_review"] for r in observed))
            arms[arm] = table
        by_case[case] = arms
    return dict(format="written-replay-48-observation/v1", raw_log_sha256=packet["source_log_sha256"],
        protocol_lock_ref=dict(path=LOCK, sha256=digest((ROOT / LOCK).read_bytes())),
        batch={k:v for k,v in audit.items() if k not in {"allocations", "distinct_inputs"}}, usage=usage,
        case_arms=by_case, public_packet_sha256=digest(canonical(packet)),
        public_responses_reviewed=len(labels) if labels is not None else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, help="Journal to audit. With no arguments, reproduce the saved result.")
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--blind-packet", action="store_true")
    options.add_argument("--annotations", type=Path)
    args = parser.parse_args()
    verify_saved = args.run is None and not args.blind_packet and args.annotations is None
    run = args.run or ROOT / read_json((ROOT / LOCK).read_bytes())["planned_output"]
    audit, packet, ids, usage = load_run(run)
    path = ROOT / ANNOTATIONS if verify_saved else args.annotations
    annotations = read_json(path.read_bytes()) if path else None
    labels = validate_annotations(packet, annotations) if annotations is not None else None
    result = packet if args.blind_packet else summarize(audit, packet, ids, usage, labels)
    if annotations is not None:
        result["public_review"] = dict(reviewer=annotations["reviewer"], condition_assignments_visible=False,
                                       annotations_sha256=digest(canonical(annotations)))
    if verify_saved:
        if result != read_json((ROOT / RESULTS).read_bytes()):
            raise ContractError("Recomputed pilot differs from its saved result")
        result = dict(status="passed", result_sha256=digest(canonical(result)),
                      observed_replies=audit["observed_replies"], raw_log_sha256=packet["source_log_sha256"])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
