"""Reproduce the 120-request pilot, a condition-hidden packet and preserved annotations.

No model/network calls. Criteria and packet ID algorithm were fixed before
collection; this analysis implementation is separate from the frozen executor.
"""

import argparse
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.recording.journal import BlobStore
from agent_memory_eval.runner.decision_replay import audit_replay, public_reply


ROOT = Path(__file__).resolve().parents[1]
LOCK = Path("artifacts/pilots/20261116-decision-replay-protocol.json")
RECEIPT = Path("artifacts/pilots/20261116-decision-replay-results.json")
ANALYSIS = Path("artifacts/analyses/20261116-decision-replay-review.json")
CATEGORIES = {"known_error", "valid_direct_response", "clarification_or_observation", "unclear_or_incomplete"}
CORE = {"present", "corrected", "not_tested", "unclear"}


def ref(path):
    return dict(path=str(path.relative_to(ROOT)), sha256=digest(path.read_bytes()))


def review_id(run_id):
    return digest(("decision-review-v1:" + run_id).encode())[:16]


def load_source():
    lock = read_json((ROOT / LOCK).read_bytes())
    refs = lock["implementation_files"] + [v for v in lock.values() if isinstance(v, dict) and "path" in v and "sha256" in v]
    for item in refs:
        assert digest((ROOT / item["path"]).read_bytes()) == item["sha256"], item["path"]
    root = ROOT / lock["planned_output"]
    audit = audit_replay(root)
    raw_log = (root / "log.jsonl").read_bytes()
    assert digest(raw_log) == audit["log_sha256"]
    last = read_json(raw_log.splitlines()[-1])
    blobs = BlobStore(root)
    control = blobs.get(blobs.get(last["state_ref"])["decision_replay"]["control_ref"])
    assert canonical(control["profile"]) == canonical(read_json((ROOT / lock["profile_ref"]["path"]).read_bytes()))
    assert [{k: v for k, v in r.items() if k not in {"run_id", "session_id"}} for r in control["schedule"]] == lock["allocations"]
    assert audit["planned"] == lock["planned_requests"]
    return audit, control, blobs


def packet_for(audit, control, blobs):
    contexts, entries = {}, []
    for row in audit["runs"]:
        if row["status"] != "response" or row["judgement"]["status"] != "manual_review_required":
            continue
        case = control["cases"][row["case_id"]]
        contexts.setdefault(row["case_id"], dict(message_index=case["message_index"],
            messages=blobs.get(case["request_ref"])["messages"]))
        reply = public_reply(blobs.get(row["response_ref"])["choices"][0]["message"])
        entries.append(dict(review_id=review_id(row["run_id"]), case_id=row["case_id"],
            public_reply=reply, reply_sha256=digest(canonical(reply)), finish_reason=row["finish_reason"]))
    assert len({r["review_id"] for r in entries}) == len(entries)
    return dict(format="agent-memory-decision-review-packet/v1", source_log_sha256=audit["log_sha256"],
        condition_names_hidden=True, possible_condition_inference_from_reply=True, contexts=contexts,
        entries=sorted(entries, key=lambda r: r["review_id"]))


def validate_annotations(packet, annotations):
    entries = {r["review_id"]: r for r in packet["entries"]}
    assert len({a["review_id"] for a in annotations}) == len(annotations)
    assert {a["review_id"] for a in annotations} == set(entries), "Review every returned manual-target reply exactly once"
    for note in annotations:
        assert set(note) == {"review_id", "category", "core_error", "needs_followup", "note_ko", "evidence_excerpt"}
        assert note["category"] in CATEGORIES and note["core_error"] in CORE
        assert type(note["needs_followup"]) is bool
        assert isinstance(note["note_ko"], str) and note["note_ko"].strip()
        evidence = note["evidence_excerpt"]
        message = entries[note["review_id"]]["public_reply"]
        rendered = (message.get("content") or "") + "\n" + canonical(message).decode()
        assert isinstance(evidence, str) and len(evidence) <= 400
        assert evidence in rendered and (evidence or not message.get("content") and not message.get("tool_calls"))


def aggregate(rows):
    responses = [r for r in rows if r["status"] == "response"]
    usage = [r.get("usage") or {} for r in rows if r["status"] != "skipped"]
    reasoning = [(u.get("completion_tokens_details") or {}).get("reasoning_tokens") for u in usage]
    reported = [v for v in reasoning if type(v) is int]
    return dict(planned=len(rows), statuses=dict(Counter(r["status"] for r in rows)),
        argument_screen=dict(Counter(r["judgement"]["status"] for r in responses if r["judgement"]["screen"] is not None)),
        manual_categories=dict(Counter(r["manual_annotation"]["category"] for r in responses if "manual_annotation" in r)),
        manual_core_error=dict(Counter(r["manual_annotation"]["core_error"] for r in responses if "manual_annotation" in r)),
        manual_needs_followup=sum(r.get("manual_annotation", {}).get("needs_followup", False) for r in responses),
        finish_reasons=dict(Counter(r["finish_reason"] for r in responses)),
        known_cost_usd=str(sum((Decimal(str(r["reported_cost_usd"])) for r in rows if r.get("cost_status") == "observed"), Decimal(0))),
        unknown_cost_requests=sum(r.get("cost_status") == "unknown" for r in rows),
        reported_tokens=sum(u.get("total_tokens", 0) for u in usage),
        reasoning_tokens=dict(total=sum(reported), zeros=sum(v == 0 for v in reported),
            missing=len(reasoning)-len(reported), mean=sum(reported)/len(reported) if reported else None))


def build_review(annotations):
    audit, control, blobs = load_source()
    packet = packet_for(audit, control, blobs)
    validate_annotations(packet, annotations)
    notes = {a["review_id"]: a for a in annotations}
    rows = deepcopy(audit["runs"])
    for row in rows:
        key = review_id(row["run_id"])
        if key in notes:
            row["manual_annotation"] = notes[key]
    conditions = control["profile"]["conditions"]
    analysis = dict(format="agent-memory-decision-replay-review/v1", method_ref=ref(Path(__file__).resolve()),
        protocol_lock_ref=ref(ROOT / LOCK), source_log_sha256=audit["log_sha256"],
        review_packet_sha256=digest(canonical(packet)), source_annotations=annotations,
        reviewer=dict(kind="single_analysis_agent", condition_names_hidden=True,
            possible_condition_inference=True, human_adjudication=False, independent_adjudication=False),
        by_condition={c: aggregate([r for r in rows if r["condition"] == c]) for c in conditions},
        by_case={case: {c: aggregate([r for r in rows if r["case_id"] == case and r["condition"] == c])
            for c in conditions} for case in sorted(control["cases"])}, runs=rows,
        limits=["Selected historical worker-error snapshots, five repetitions per condition.",
            "One reply only; argument screens and semantic annotations are different measurements.",
            "Researcher-authored memo prompt intervention, no autonomous writing/retrieval or episode outcome.",
            "No generic reminder or dedicated initially-correct-state control."])
    receipt = dict(format="agent-memory-decision-replay-pilot-results/v1", protocol_lock_ref=analysis["protocol_lock_ref"],
        audit=audit, review_ref=dict(path=str(ANALYSIS), sha256=digest(canonical(analysis)+b"\n")))
    return receipt, analysis


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path)
    parser.add_argument("--annotations", type=Path)
    args = parser.parse_args()
    assert not (args.packet and args.annotations)
    if args.packet:
        audit, control, blobs = load_source()
        packet = packet_for(audit, control, blobs)
        if args.packet.exists():
            assert canonical(read_json(args.packet.read_bytes())) == canonical(packet)
        else:
            with args.packet.open("xb") as stream:stream.write(canonical(packet)+b"\n")
        print(json.dumps(dict(status="packet_verified", entries=len(packet["entries"]),
            contexts=len(packet["contexts"]), source_log_sha256=packet["source_log_sha256"])))
        return
    analysis_path, receipt_path = ROOT / ANALYSIS, ROOT / RECEIPT
    if args.annotations:
        assert not analysis_path.exists() and not receipt_path.exists(), "Never overwrite a past review"
        annotations = read_json(args.annotations.read_bytes())
    else:
        annotations = read_json(analysis_path.read_bytes())["source_annotations"]
    receipt, analysis = build_review(annotations)
    for path, value in ((analysis_path, analysis), (receipt_path, receipt)):
        if args.annotations:
            with path.open("xb") as stream:stream.write(canonical(value)+b"\n")
        else:
            assert canonical(read_json(path.read_bytes())) == canonical(value)
    print(json.dumps(dict(status="review_verified", counts=receipt["audit"]["counts"],
        http_calls=receipt["audit"]["http_calls"], reported_cost_usd=receipt["audit"]["reported_cost_usd_decimal"],
        manual_annotations=len(annotations), benchmark_or_model_calls=0), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
