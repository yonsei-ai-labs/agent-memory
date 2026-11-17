"""Recompute saved writing metrics and memory exports without keys, source runs or HTTP."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from agent_memory_eval.analysis.writing_export import collect
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, safe_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, default=ROOT / "artifacts/pilots/20261117-writing-10-results.json")
    args = parser.parse_args()
    receipt = read_json(args.receipt.read_bytes())
    if receipt["format"] != "agent-memory-writing-result/v1":
        raise ContractError("Unknown writing result receipt")
    root = safe_path(ROOT, receipt["source_root"])
    before = digest((root / "log.jsonl").read_bytes())
    if before != receipt["raw_log_sha256"]:
        raise ContractError("Writing log differs from its recorded result")
    review = read_json(asset_text(ROOT, receipt["semantic_review_ref"]).encode())
    annotations = read_json(asset_text(ROOT, review["annotation_ref"]).encode())
    views = collect(root)
    metrics = views["metrics"]
    mapping = views["mapping"]["cases"]
    if (metrics["audit"] != receipt["audit"] or metrics["by_feedback"] != receipt["by_feedback"]
            or digest(canonical(views["blind"]) + b"\n") != annotations["blind_export_sha256"]):
        raise ContractError("Writing metrics or blinded presentation changed")
    actual = {row["run_id"]:row for row in metrics["runs"]}
    for row in review["runs"]:
        expected = actual[row["run_id"]]
        if any(row[k] != v for k,v in expected.items() if k != "items"):
            raise ContractError("Content review metrics changed")
        if [{k:item[k] for k in ("id", "version", "title", "body")} for item in row["items"]] != expected["items"]:
            raise ContractError("Stored memory content changed")
        if mapping[row["case_id"]]["run_id"] != row["run_id"]:
            raise ContractError("Content case mapping changed")
    for level, group in metrics["by_feedback"].items():
        rows = [a for a in annotations["cases"] if mapping[a["case_id"]]["feedback_level"] == level]
        expected = dict(group, target_baggage_distinction=dict(Counter(a["target_baggage_distinction"] for a in rows)))
        for field in ("recounts_six_bag_allowance", "route_ambiguity_hypothesis", "generic_confirmation",
                      "retains_transaction_specific_numbers", "retains_reservation_or_flight_ids"):
            expected[field] = sum(a[field] for a in rows)
        if review["by_feedback"][level] != expected:
            raise ContractError("Semantic count table differs from preserved annotations")
    comparison = read_json(asset_text(ROOT, receipt["input_comparison_ref"]).encode())
    starts = {}
    for line in (root / "log.jsonl").read_bytes().splitlines():
        for event in read_json(line)["events"]:
            if event["type"] == "model.request_started" and event["run_id"] in actual:
                starts.setdefault(event["run_id"], event)
    hashes, normalized, blobs = defaultdict(set), set(), BlobStore(root)
    for run, event in starts.items():
        wire = blobs.get(event["payload"]["request_ref"])
        hashes[actual[run]["feedback_level"]].add(event["payload"]["request_ref"]["sha256"])
        wire["messages"][2] = dict(role="user", content="<feedback-only-difference>")
        normalized.add(digest(canonical(wire)))
    if (len(normalized) != 1 or comparison["normalized_request_sha256"] not in normalized
            or {k:len(v) for k,v in hashes.items()} != comparison["first_requests_each_level_identical"]):
        raise ContractError("Initial input equality or feedback-only difference changed")
    if digest((root / "log.jsonl").read_bytes()) != before:
        raise ContractError("Review changed the immutable writing log")
    print(json.dumps(dict(status="passed", allocations=len(actual), audit=receipt["audit"],
        content_and_metrics_recomputed=True, annotations_preserved_not_independently_rejudged=True,
        new_model_or_catalog_calls=0), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
