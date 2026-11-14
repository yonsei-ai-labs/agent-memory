"""Read-only feasibility check for proposed outcome-decomposition supervision.

Uses existing train outcomes only. No model, generated label, or new worker run.
Run at repository root; --check compares the committed/saved audit JSON.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent


def audit():
    judgments = ROOT / "datasets/api_worker_utility/20261108-v3/judgments.jsonl"
    training = ROOT / "datasets/api_transfer/20261108-v1/train.jsonl"
    families = {}
    for row in map(json.loads, training.read_text().splitlines()):
        if row["state_id"] in families:
            assert families[row["state_id"]] == row["family"]
        families[row["state_id"]] = row["family"]
    groups = defaultdict(dict)
    repeats = 0
    for row in map(json.loads, judgments.read_text().splitlines()):
        if row["split"] != "train":
            continue
        if "-repeat-" in row["job_id"]:
            repeats += 1
            continue
        state_id, action = row["state_id"], row["action"]
        assert state_id in families
        assert action not in groups[state_id]
        groups[state_id][action] = row
    actions_expected = {"none", "all_full"} | {
        recipe + "_" + view
        for recipe in ("returned", "receiver", "copied_returned", "copied_receiver")
        for view in ("full", "procedure")
    }
    labels = {(0, 0): "both_fail", (0, 1): "help", (1, 0): "harm", (1, 1): "both_pass"}
    counts, state_counts = Counter(), Counter()
    by_family = defaultdict(Counter)
    extra_tokens = []
    for state_id, outcomes in sorted(groups.items()):
        assert set(outcomes) == actions_expected
        baseline = outcomes["none"]
        baseline_success = int(baseline["all_passed"])
        state_counts["baseline_pass" if baseline_success else "baseline_fail"] += 1
        for action, row in sorted(outcomes.items()):
            if action in ("none", "all_full"):
                continue
            label = labels[baseline_success, int(row["all_passed"])]
            counts[label] += 1
            by_family[families[state_id]][label] += 1
            extra_tokens.append(
                row["input_tokens"] + row["output_tokens"]
                - baseline["input_tokens"] - baseline["output_tokens"]
            )
    assert len(groups) == 50 and sum(counts.values()) == 400
    return {
        "scope": "Existing 4B train outcomes only; eight single-memory actions per state.",
        "new_worker_outcomes": 0,
        "input_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (judgments, training)
        },
        "states": len(groups),
        "state_baseline": dict(sorted(state_counts.items())),
        "candidate_pairs": sum(counts.values()),
        "four_case_labels": dict(sorted(counts.items())),
        "neutral_fraction_in_three_level_gain": (counts["both_fail"] + counts["both_pass"]) / sum(counts.values()),
        "by_family": {k: dict(sorted(v.items())) for k, v in sorted(by_family.items())},
        "extra_worker_tokens_min": min(extra_tokens),
        "extra_worker_tokens_max": max(extra_tokens),
        "repeat_rows_excluded": repeats,
        "independence_unit": "State/source family, not each of the eight derived action pairs.",
        "limitations": [
            "One fixed greedy worker outcome per state-action, not an estimated stochastic success probability.",
            "Four-case targets are observed paired outcomes under this protocol, not identified individual causal types in arbitrary logs.",
            "No development/test scores, model fitting, or threshold selection in this audit.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = audit()
    if args.check:
        assert result == json.loads((HERE / "train-label-audit.json").read_text())
        print("Train-label audit matches: 50 states, 400 paired candidate outcomes.")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
