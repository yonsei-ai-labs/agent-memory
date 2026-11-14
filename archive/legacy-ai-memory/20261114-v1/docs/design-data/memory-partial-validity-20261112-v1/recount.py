"""Recount prior stored outcomes; no new judgments, inference, or source edits."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "evaluation/results/external-update-masking-20261111-v1/case-effects.csv"
CONDITIONS = ("current_new", "current_new_plus_old", "old_values_marked", "old_values_masked")


def recount():
    with SOURCE.open() as handle:
        rows = list(csv.DictReader(handle))
    if len({r["case_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate case IDs")
    out = {}
    for source in sorted({r["source"] for r in rows}):
        group = [r for r in rows if r["source"] == source]
        scores = {}
        for condition in CONDITIONS:
            for row in group:
                correct, unknown = row[condition + "_correct"], row[condition + "_unknown"]
                if correct not in {"0", "1"} or unknown not in {"0", "1"} or correct == unknown == "1":
                    raise ValueError("Invalid outcome flags")
            scores[condition] = {
                "correct": sum(int(r[condition + "_correct"]) for r in group),
                "unknown": sum(int(r[condition + "_unknown"]) for r in group),
                "incorrect_nonabstention": sum(
                    not int(r[condition + "_correct"]) and not int(r[condition + "_unknown"])
                    for r in group
                ),
            }
        transitions = {}
        for left, right in [("current_new", "current_new_plus_old"),
                            ("current_new_plus_old", "old_values_masked"),
                            ("old_values_marked", "old_values_masked")]:
            transitions[left + "_to_" + right] = {
                "gain_ids": sorted(r["case_id"] for r in group
                                   if int(r[right + "_correct"]) > int(r[left + "_correct"])),
                "harm_ids": sorted(r["case_id"] for r in group
                                   if int(r[right + "_correct"]) < int(r[left + "_correct"])),
            }
        out[source] = {"n": len(group), "recorded_family_ids": len({r["family_id"] for r in group}),
                       "scores": scores, "paired_transitions": transitions}
    return {
        "source_file": str(SOURCE.relative_to(ROOT)),
        "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "recount_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Existing development outputs only; no relabeling or new model execution",
        "family_note": "Counts of stored IDs; semantic independence is not established by counting IDs",
        "by_source": out,
    }


if __name__ == "__main__":
    print(json.dumps(recount(), ensure_ascii=False, indent=2))
