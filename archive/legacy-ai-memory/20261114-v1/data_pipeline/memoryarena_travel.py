"""Build explicit-reference memory pairs from pinned travel sequences.

This uses reference histories, NOT real worker trajectories or utility labels.
Every input memory predates the current question. Split by the base plan, not
by one of its six name/request variants. Annotation rules are intentionally
conservative; excluded clauses are counted rather than silently guessed.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import re

DATA_SHA = "2f955d444f6f3ad3c5da2064359ab19f8fc1f90621ff9d00723a450a009c3732"
DATA_REVISION = "da1a37c8b19280e18627ca01cf368195a5e1d92e"
SEED = 20261108
SLOTS = ("current_city", "transportation", "breakfast", "attraction", "lunch", "dinner", "accommodation")
LABELS = ["reuse_reference", "comparison_reference", "not_referenced"]
ORDINALS = dict(zip(("first", "second", "third", "fourth", "fifth", "sixth", "seventh"), range(1, 8)))
DAY_RE = re.compile(r"\b(?:(first|second|third|fourth|fifth|sixth|seventh|[0-9]+(?:st|nd|rd|th))[-\s]+day|day[-\s]+([0-9]+))\b", re.I)
SLOT_PATTERNS = {
    "accommodation": r"\b(?:accommodation|hotel|room|stay|staying|lodge|lodging|place to stay)\b",
    "breakfast": r"\bbreakfast\b", "lunch": r"\blunch\b", "dinner": r"\bdinner\b",
    "transportation": r"\b(?:flight|flights|transportation)\b",
    "attraction": r"\b(?:attraction|attractions|sightseeing)\b",
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def text_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def write_jsonl(path, rows):
    Path(path).write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows))


def name_from_query(query):
    match = re.match(r"I am ([^\n.]+)\.", query)
    if not match:
        raise ValueError("Unrecognized speaker header")
    return match.group(1).strip()


def group_split(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[text_hash(row["base_person"]["daily_plans"])].append(row)
    if len(groups) != 45 or set(map(len, groups.values())) != {6}:
        raise ValueError("Unexpected base-plan grouping")
    exposed = next(key for key, rs in groups.items() if any(r["id"] == 1 for r in rs))
    by_days = defaultdict(list)
    for key, rs in groups.items():
        by_days[len(rs[0]["base_person"]["daily_plans"])].append(key)
    assignment = {}
    for days, keys in sorted(by_days.items()):
        if len(keys) != 15:
            raise ValueError("Unexpected trip-duration distribution")
        keys = sorted(keys)
        random.Random(SEED + days).shuffle(keys)
        dev_n = 3 if days == 3 else 2
        if exposed in keys:
            keys.remove(exposed)
            keys.insert(0, exposed)
        for key in keys[:dev_n]:
            assignment[key] = "dev"
        for key in keys[dev_n:dev_n + 10]:
            assignment[key] = "train"
        for key in keys[dev_n + 10:]:
            assignment[key] = "test"
    return {
        "seed": SEED, "split_unit": "identical_base_daily_plan",
        "official_benchmark_split": False, "previously_exposed_ids": [1],
        "groups": [{"source_group": key, "split": assignment[key],
                    "days": len(rs[0]["base_person"]["daily_plans"]),
                    "row_ids": sorted(r["id"] for r in rs)} for key, rs in sorted(groups.items())],
    }


def paragraphs(query):
    start = 0
    for boundary in list(re.finditer(r"\n\s*\n", query)) + [None]:
        end = boundary.start() if boundary is not None else len(query)
        raw = query[start:end]
        offset = start
        lines = raw.splitlines(keepends=True)
        while lines and (re.match(r"\s*I am [^.]+\.\s*$", lines[0]) or
                         re.match(r"\s*I['’]m (?:joining|traveling|travelling|going)\b", lines[0])):
            offset += len(lines.pop(0))
        raw = "".join(lines)
        focal = raw.strip()
        offset += len(raw) - len(raw.lstrip())
        if focal:
            assert query[offset:offset + len(focal)] == focal
            yield focal, offset, offset + len(focal)
        start = boundary.end() if boundary is not None else len(query)


def day_mentions(text):
    mentions = []
    for match in DAY_RE.finditer(text):
        value = (match.group(1) or match.group(2)).lower()
        day = ORDINALS[value] if value in ORDINALS else int(re.match(r"[0-9]+", value).group())
        mentions.append((match.start(), match.end(), day))
    return mentions


def slots_in(text):
    return {slot for slot, pattern in SLOT_PATTERNS.items() if re.search(pattern, text, re.I)}


def reference_from_clause(clause, previous_names):
    found = [(name, list(re.finditer(r"\b" + re.escape(name) + r"\b", clause))) for name in previous_names]
    found = [(name, matches) for name, matches in found if matches]
    if len(found) != 1:
        return None, "no_unique_explicit_prior_person"
    name, matches = found[0]
    if len(matches) != 1:
        return None, "multiple_mentions_of_person"
    if re.search(r"\b(?:than|higher|lower|cheaper|different|within|price|priced|cost|costs|rating|rated|cuisine|rule|rules|room type)\b", clause, re.I):
        kind = "comparison_reference"
    elif re.search(r"\b(?:same|join|share|match|identical)\b", clause, re.I):
        kind = "reuse_reference"
    else:
        return None, "uncovered_reference_relation"
    days = day_mentions(clause)
    after_name = [day for start, end, day in days if start >= matches[0].end()]
    if len(set(after_name)) == 1:
        source_day = after_name[0]
    elif not after_name and len({day for start, end, day in days}) == 1:
        source_day = days[0][2]
    else:
        return None, "ambiguous_or_missing_reference_day"
    source_slots = slots_in(clause[matches[0].end():])
    if not source_slots:
        source_slots = slots_in(clause)
    if not source_slots and re.search(r"same place", clause, re.I):
        source_slots = {"accommodation"}
    if len(source_slots) != 1:
        return None, "ambiguous_or_missing_reference_slot"
    return {"person": name, "day": source_day, "slot": next(iter(source_slots)), "kind": kind,
            "target_day": days[0][2]}, None


def cards_for_plan(row_id, person, prior_round, plan):
    result = []
    for day in plan:
        day_idx = int(day.get("days", day.get("day")))
        for slot in SLOTS:
            value = str(day.get(slot, "-")).strip()
            if not value or value == "-":
                continue
            result.append({
                "memory_id": f"r{row_id}-p{prior_round}-d{day_idx}-{slot}",
                "person": person, "day": day_idx, "slot": slot, "value": value,
                "prior_round": prior_round,
                "text": f"Recorded plan for {person}. Day {day_idx}. {slot.replace('_', ' ').title()}: {value}",
            })
    return result


def choose_candidates(cards, reference, query_id):
    positives = [c for c in cards if all(c[key] == reference[key] for key in ("person", "day", "slot"))]
    if len(positives) != 1:
        return None
    positive = positives[0]
    other = [c for c in cards if c["memory_id"] != positive["memory_id"]]
    rng = random.Random(int(hashlib.sha256(query_id.encode()).hexdigest()[:16], 16))
    rng.shuffle(other)
    selected = [positive]
    predicates = [
        lambda c: c["person"] == positive["person"] and c["slot"] == positive["slot"] and c["day"] != positive["day"],
        lambda c: c["person"] != positive["person"] and c["slot"] == positive["slot"] and c["day"] == positive["day"],
        lambda c: c["person"] == positive["person"] and c["slot"] != positive["slot"] and c["day"] == positive["day"],
        lambda c: c["value"] == positive["value"],
        lambda c: True,
    ]
    for predicate in predicates:
        pick = next((c for c in other if c not in selected and predicate(c)), None)
        if pick is not None:
            selected.append(pick)
    for candidate in other:
        if len(selected) == 6:
            break
        if candidate not in selected:
            selected.append(candidate)
    if len(selected) != 6:
        return None
    rng.shuffle(selected)
    return selected


def build(source, output):
    if output.exists():
        raise FileExistsError(output)
    if digest(source) != DATA_SHA:
        raise ValueError("Unexpected MemoryArena source")
    source_rows = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    split_plan = group_split(source_rows)
    assignment = {row_id: g for g in split_plan["groups"] for row_id in g["row_ids"]}
    inputs, labels, provenance, query_info = [], [], [], []
    exclusions = Counter()
    for row in source_rows:
        group = assignment[row["id"]]
        previous_names = [row["base_person"]["name"]]
        cards = cards_for_plan(row["id"], previous_names[0], 0, row["base_person"]["daily_plans"])
        for current_round, query in enumerate(row["questions"], 1):
            for clause_idx, (clause, start, end) in enumerate(paragraphs(query)):
                ref, reason = reference_from_clause(clause, previous_names)
                if reason:
                    exclusions[(group["split"], reason)] += 1
                    continue
                qid = f"travel-{row['id']}-turn-{current_round}-clause-{clause_idx}"
                selected = choose_candidates(cards, ref, qid)
                if selected is None:
                    exclusions[(group["split"], "reference_record_absent_or_too_few_candidates")] += 1
                    continue
                target = next(c for c in selected if all(c[k] == ref[k] for k in ("person", "day", "slot")))
                query_info.append({"query_id": qid, "split": group["split"], "source_group": group["source_group"],
                                   "source_row_id": row["id"], "current_round": current_round,
                                   "reference": ref, "target_value": target["value"],
                                   "same_value_negative_count": sum(c["value"] == target["value"] and c != target for c in selected)})
                for candidate in selected:
                    positive = candidate["memory_id"] == target["memory_id"]
                    label = LABELS.index(ref["kind"]) if positive else 2
                    sid = qid + "--" + candidate["memory_id"]
                    inputs.append({"sample_id": sid, "query_id": qid, "memory_id": candidate["memory_id"],
                                   "source_group": group["source_group"], "split": group["split"],
                                   "input": {"state_text": f"Current traveller: {name_from_query(query)}. Trip length: {group['days']} days.\nReference to assess:\n" + clause,
                                             "candidate_text": candidate["text"]}})
                    labels.append({"sample_id": sid, "label": label})
                    provenance.append({"sample_id": sid, "source_row_id": row["id"],
                                       "current_round": current_round, "clause_span": [start, end],
                                       "reference": ref, "candidate": {k: v for k, v in candidate.items() if k != "text"},
                                       "history_mode": "public_reference_history", "current_answer_in_input": False})
                    if candidate["prior_round"] >= current_round:
                        raise ValueError("Future record exposed")
            # Append only AFTER creating all inputs for this current round.
            name = name_from_query(query)
            if name in previous_names:
                raise ValueError("Ambiguous repeated speaker")
            previous_names.append(name)
            cards.extend(cards_for_plan(row["id"], name, current_round, row["answers"][current_round - 1]))
    output.mkdir(parents=True)
    write_json(output / "split-plan.json", split_plan)
    write_jsonl(output / "inputs.jsonl", inputs)
    write_jsonl(output / "supervision.jsonl", labels)
    write_jsonl(output / "provenance.jsonl", provenance)
    write_jsonl(output / "queries.jsonl", query_info)
    summary = {
        "status": "draft_requires_annotation_review", "source_sha256": DATA_SHA,
        "dataset_revision": DATA_REVISION, "history_mode": "public_reference_history",
        "official_benchmark_score": False, "utility_labels": 0, "labels": LABELS,
        "base_plan_groups": dict(Counter(g["split"] for g in split_plan["groups"])),
        "query_counts": dict(Counter(q["split"] for q in query_info)),
        "pair_counts": dict(Counter(r["split"] for r in inputs)),
        "train_dev_annotation_counts": {split: {
            "relation": dict(Counter(q["reference"]["kind"] for q in query_info if q["split"] == split)),
            "slot": dict(Counter(q["reference"]["slot"] for q in query_info if q["split"] == split)),
            "different_target_and_reference_day": sum(q["reference"]["day"] != q["reference"]["target_day"] for q in query_info if q["split"] == split),
        } for split in ("train", "dev")},
        "exclusions": [{"split": s, "reason": r, "count": n} for (s, r), n in sorted(exclusions.items())],
        "generator_sha256": digest(__file__),
    }
    write_json(output / "summary.json", summary)
    write_json(output / "artifact-index.json", {"status": "draft_requires_annotation_review",
               "sha256": {p.name: digest(p) for p in sorted(output.iterdir()) if p.is_file()}})
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output), indent=2))


if __name__ == "__main__":
    main()
