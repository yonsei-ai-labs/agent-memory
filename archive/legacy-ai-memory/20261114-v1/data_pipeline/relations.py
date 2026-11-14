"""Inspect a released relation dataset or prepare the existing training export.

Inspection needs only Python's standard library and the released files. Full
source-log verification is a separate operation documented in datasets/README.md.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from training.memory_selector.prepare_relations_benchmark import FILES, read_data

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "datasets/memory_relations/20261107-discovery-v1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_dataset(source: Path) -> dict:
    """Check released learning files, grouped folds, and contrast membership."""
    source = source.resolve()
    index_path = source / "artifact-index.json"
    index = json.loads(index_path.read_text())
    hashes = {}
    for name in FILES:
        path = source / name
        relative = path.relative_to(ROOT).as_posix()
        matches = [r for r in index["files"] if r["path"] == relative]
        if len(matches) != 1 or sha256(path) != matches[0]["sha256"]:
            raise ValueError(f"Released dataset hash mismatch: {relative}")
        hashes[name] = matches[0]["sha256"]
    rows, splits = read_data(source)
    by_id = {row["sample_id"]: row for row in rows}
    contrasts = json.loads((source / "paired-contrasts.json").read_text())
    groups = {}
    for key in ["same_memory_question_changed_current_with_label_change", "same_state_changed_memory"]:
        for group in contrasts[key]:
            ids = group["sample_ids"]
            if len(ids) < 2 or len(ids) != len(set(ids)) or not set(ids) <= by_id.keys():
                raise ValueError(f"Invalid contrast members: {key}")
            if len({splits["sample_group"][i] for i in ids}) != 1:
                raise ValueError(f"Contrast crosses source families: {key}")
            if "labels" in group and group["labels"] != [by_id[i]["label_name"] for i in ids]:
                raise ValueError(f"Contrast labels disagree: {key}")
        groups[key] = len(contrasts[key])
    return {
        "status": "passed",
        "verification_scope": "released_learning_files_and_grouped_development_contract",
        "dataset": source.relative_to(ROOT).as_posix(),
        "source_index_sha256": sha256(index_path),
        "rows": len(rows),
        "source_experiences": len({r["source_instance_id"] for r in rows}),
        "connected_families": len(set(splits["sample_group"].values())),
        "folds": len(splits["folds"]),
        "label_counts": dict(sorted(Counter(r["label_name"] for r in rows).items())),
        "contrast_groups": groups,
        "learning_files_sha256": hashes,
        "raw_source_logs_verified": False,
        "independent_confirmation": False,
        "utility_labels_used": False,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "prepare"])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, help="New training export directory; required for prepare")
    args = parser.parse_args(argv)
    if args.command == "check" and args.output is not None:
        parser.error("--output applies only to prepare")
    if args.command == "prepare" and args.output is None:
        parser.error("prepare requires a new --output directory")
    try:
        summary = inspect_dataset(args.dataset)
        if args.command == "check":
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            # Keep the executed export implementation and Colab's flat bundle
            # import path intact. It checks the complete frozen dataset index.
            subprocess.run([
                sys.executable, str(ROOT / "training/memory_selector/prepare_relations_benchmark.py"),
                "--source", str(args.dataset.resolve()), "--output", str(args.output.resolve()),
            ], check=True)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Dataset check/preparation failed: {exc}\n")


if __name__ == "__main__":
    main()
