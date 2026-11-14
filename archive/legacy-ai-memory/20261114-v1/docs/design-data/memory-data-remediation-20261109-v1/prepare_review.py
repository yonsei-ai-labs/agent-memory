"""Prepare a training-only blinded review batch; do not relabel source data."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import re

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "datasets/development_memory_leads/20261108-v1"

def readl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def words(text):
    return set(re.findall(r"[a-z][a-z0-9_]{2,}", text.lower()))

def prepare(output):
    if output.exists():
        raise FileExistsError(output)
    inputs = readl(SOURCE / "inputs.jsonl")
    labels = {r["sample_id"]: r for r in readl(SOURCE / "supervision.jsonl")}
    evidence = {r["memory_id"]: r for r in json.loads((SOURCE / "source-evidence.json").read_text())}
    rows = [r for r in inputs if r["split"] == "train"]
    positives = [r for r in rows if labels[r["sample_id"]]["label_name"] == "shared_diagnostic_mechanism"]
    unresolved = [r for r in rows if labels[r["sample_id"]]["label_name"] == "insufficient_connection"]
    negatives = [r for r in rows if labels[r["sample_id"]]["label_name"] == "different_mechanism"]
    # Diagnostic lexical similarity only proposes candidates for reannotation.
    # It does not establish a positive/negative or a utility label.
    scores = {}
    for row in negatives:
        a, b = words(row["input"]["state_text"]), words(row["input"]["candidate_text"])
        scores[row["sample_id"]] = len(a & b) / max(1, len(b))
    ranked = sorted(negatives, key=lambda r: (-scores[r["sample_id"]], r["sample_id"]))
    selected_negatives, seen_queries = [], set()
    for row in ranked:
        if row["query_id"] not in seen_queries:
            selected_negatives.append(row)
            seen_queries.add(row["query_id"])
        if len(selected_negatives) == 23:
            break
    if len(selected_negatives) < 23:
        selected_ids = {r["sample_id"] for r in selected_negatives}
        selected_negatives.extend(r for r in ranked if r["sample_id"] not in selected_ids)
        selected_negatives = selected_negatives[:23]
    selected = positives + unresolved + selected_negatives
    random.Random(731).shuffle(selected)
    reviewer, maintainer = output / "reviewer", output / "maintainer"
    reviewer.mkdir(parents=True)
    maintainer.mkdir()
    cards, key, forms = [], [], []
    memory_keys, source_rows = {}, {}
    for number, row in enumerate(selected, 1):
        rid = f"review-{number:03d}"
        mid = row["memory_id"]
        if mid not in memory_keys:
            mkey = f"source-{len(memory_keys) + 1:02d}"
            memory_keys[mid] = mkey
            # The source is public evidence. Original v1 relation annotations and
            # their rationale are intentionally omitted from reviewer material.
            source_rows[mkey] = {"source_key": mkey, "source_evidence": evidence[mid]}
        mkey = memory_keys[mid]
        cards.append({"review_id": rid, "current_observation": row["input"]["state_text"],
                      "candidate_memory": row["input"]["candidate_text"], "source_key": mkey})
        key.append({"review_id": rid, "original_sample_id": row["sample_id"],
                    "original_query_id": row["query_id"], "original_memory_id": mid,
                    "split": row["split"], "original_label": labels[row["sample_id"]]["label_name"],
                    "original_rationale": labels[row["sample_id"]].get("rationale"),
                    "negative_proposal_score": scores.get(row["sample_id"]),
                    "input_sha256": hashlib.sha256(json.dumps(row["input"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()})
        forms.append({"review_id": rid, "reviewer_id": None, "relation": None,
                      "source_alignment": None, "added_information": None,
                      "current_evidence_quote": None, "source_event_indices": [],
                      "reason": None, "missing_evidence": None})
    for name, value in [("cases.jsonl", cards), ("review-form.jsonl", forms)]:
        (reviewer / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in value))
    dump(reviewer / "source-evidence.json", list(source_rows.values()))
    dump(maintainer / "adjudication-key.json", key)
    metadata = {"status": "prepared_unannotated", "scope": "Existing training split only; no dev/test current rows.",
                "selection": {"seed": 731, "old_label_counts": dict(Counter(r["original_label"] for r in key)),
                              "negative_proposal": "Token-overlap ranking, one per query until 23 candidates. Not verified hard negatives."},
                "rows": len(cards), "current_issues": len({r["original_query_id"] for r in key}),
                "memories": len(memory_keys), "new_source_families": 0, "new_supervised_labels": 0,
                "independent_reviews_completed": 0, "training_allowed": False,
                "source_files": {n: sha(SOURCE / n) for n in ("inputs.jsonl", "supervision.jsonl", "source-evidence.json")},
                "preserves_original_labels": True,
                "blinding": "Share reviewer/ only. An agent with access to the parent repository must be instructed not to inspect original labels."}
    dump(maintainer / "selection.json", metadata)
    # Verify the exact copies and absence of v1 supervision fields in public cards.
    assert len({r["review_id"] for r in cards}) == len(cards) == 64
    assert all(r["split"] == "train" for r in key)
    by_id = {r["sample_id"]: r for r in rows}
    for card, mapping in zip(cards, key):
        original = by_id[mapping["original_sample_id"]]
        assert card["current_observation"] == original["input"]["state_text"]
        assert card["candidate_memory"] == original["input"]["candidate_text"]
        assert set(card) == {"review_id", "current_observation", "candidate_memory", "source_key"}
    assert all(r["relation"] is None for r in forms)
    print(json.dumps({k: metadata[k] for k in ("status", "rows", "current_issues", "memories", "new_supervised_labels", "training_allowed")}, ensure_ascii=False))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    prepare(parser.parse_args().output)

