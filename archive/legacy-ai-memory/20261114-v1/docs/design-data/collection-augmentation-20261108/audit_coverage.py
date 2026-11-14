"""Read-only coverage audit for the current diagnostic-memory annotations."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent


def audit():
    folder = ROOT / "datasets/development_memory_leads/20261108-v1"
    paths = [folder / "inputs.jsonl", folder / "supervision.jsonl"]
    inputs = {r["sample_id"]: r for r in map(json.loads, paths[0].read_text().splitlines())}
    labels = {r["sample_id"]: r for r in map(json.loads, paths[1].read_text().splitlines())}
    assert set(inputs) == set(labels)
    coverage = defaultdict(lambda: defaultdict(list))
    for sid, label in labels.items():
        row = inputs[sid]
        if label["label"] == 0:
            coverage[row["memory_id"]][row["split"]].append(row)
    memories = []
    for mid in sorted({r["memory_id"] for r in inputs.values()}):
        item = {"memory_id": mid}
        for split in ("train", "dev", "test"):
            items = coverage[mid][split]
            item[split] = {
                "positive_pairs": len(items),
                "current_repositories": sorted({r["repository"] for r in items}),
                "current_issues": len({r["query_id"] for r in items}),
            }
        memories.append(item)
    positive_train = [r for r in memories if r["train"]["positive_pairs"]]
    return {
        "scope": "Existing semantic annotations only; no independent label validation or utility measurement.",
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        "input_pairs": len(inputs),
        "current_issues": len({r["query_id"] for r in inputs.values()}),
        "memory_cards": len(memories),
        "training_positive_pairs": sum(r["train"]["positive_pairs"] for r in memories),
        "memories_with_training_positive": len(positive_train),
        "positive_train_pairs_per_covered_memory": [min(r["train"]["positive_pairs"] for r in positive_train), max(r["train"]["positive_pairs"] for r in positive_train)],
        "memories_with_training_positives_in_multiple_current_repositories": sum(len(r["train"]["current_repositories"]) >= 2 for r in memories),
        "observed_utility_labels": sum(r["observed_utility"] is not None for r in labels.values()),
        "split_pair_counts": dict(sorted(Counter(r["split"] for r in inputs.values()).items())),
        "coverage": memories,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = audit()
    if args.check:
        assert result == json.loads((HERE / "coverage.json").read_text())
        print("Coverage matches: 640 pairs; 18 train positives across 10 memory cards; no utility labels.")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
