"""Build an adapted, two-stage restaurant decision pilot from pinned travel data.

The original reference clauses and person/day/meal topology are retained. Past
bookings are NEW model decisions on constructed catalog menus, not benchmark
answer plans presented as real trajectories. No original current answer enters
the worker bundle. This is not a full MemoryArena rollout.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import random
import re

from data_pipeline.memoryarena_travel import choose_candidates


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def readl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def key(text):
    return hashlib.sha256(("travel-worker-v1:" + text).encode()).hexdigest()


def entity_key(value):
    name, sep, city = value.rpartition(", ")
    return name + sep + re.sub(r"\([^)]*\)$", "", city).strip()


def constraints(clause, kind, cuisines):
    """Conservative supported subset; judge all parsed constraints conjunctively."""
    if kind == "reuse_reference":
        if re.search(r"\b(?:except|but|different|instead|not)\b", clause, re.I):
            return None
        return {"kind": "reuse"}
    lower = clause.lower()
    if re.search(r"\b(?:only|all|same|different|either|between|not)\b", lower):
        return None
    price_patterns = [
        (r"at least ([0-9]+)% (more|less)", "percent_min"),
        (r"at least \$([0-9]+) (more|less)", "absolute_min"),
        (r"(?:priced|costs?|costing) within ([0-9]+)%", "percent_within"),
        (r"(?:priced|costs?|costing) within \$([0-9]+)", "absolute_within"),
        (r"(?:costs?|costing|priced) (more|less) than", "strict"),
    ]
    prices = [(mode, match.groups()) for pattern, mode in price_patterns
              for match in re.finditer(pattern, lower)]
    if len(prices) != 1:
        return None
    mode, values = prices[0]
    price = {"mode": mode, "amount": float(values[0]) if mode != "strict" else None,
             "direction": values[-1] if mode in ("strict", "percent_min", "absolute_min") else None}
    rating = None
    found = re.search(r"rating within ([0-9]+(?:\.[0-9]+)?)", lower)
    if found:
        rating = {"mode": "within", "amount": float(found.group(1))}
    elif re.search(r"rated at least as high", lower):
        rating = {"mode": "ge"}
    elif re.search(r"rated higher", lower):
        rating = {"mode": "gt"}
    elif re.search(r"rated lower", lower):
        rating = {"mode": "lt"}
    elif re.search(r"\b(?:rated|rating)\b", lower):
        return None
    named = [c for c in cuisines if re.search(r"\b" + re.escape(c.lower()) + r"\b", lower)]
    if len(named) > 1 or ("cuisine" in lower and not named):
        return None
    return {"kind": "comparison", "price": price, "rating": rating,
            "cuisine": named[0] if named else None}


def satisfies(spec, candidate, reference):
    if spec["kind"] == "reuse":
        return candidate["id"] == reference["id"]
    price, a, b = spec["price"], candidate["cost"], reference["cost"]
    amount, direction, mode = price["amount"], price["direction"], price["mode"]
    if mode == "percent_min":
        ok = a >= b * (1 + amount / 100) - 1e-8 if direction == "more" else a <= b * (1 - amount / 100) + 1e-8
    elif mode == "absolute_min":
        ok = a >= b + amount if direction == "more" else a <= b - amount
    elif mode == "percent_within":
        ok = abs(a - b) <= b * amount / 100 + 1e-8
    elif mode == "absolute_within":
        ok = abs(a - b) <= amount + 1e-8
    else:
        ok = a > b if direction == "more" else a < b
    if not ok or (spec["cuisine"] and spec["cuisine"] not in candidate["cuisines"]):
        return False
    rating = spec["rating"]
    if rating:
        a, b = candidate["rating"], reference["rating"]
        return {"gt": a > b, "lt": a < b, "ge": a >= b,
                "within": abs(a - b) <= rating.get("amount", 0) + 1e-8}[rating["mode"]]
    return True


def build(source, csv_path, output):
    if output.exists():
        raise FileExistsError(output)
    frozen = json.loads((source / "artifact-index.json").read_text())
    for name, digest in frozen["sha256"].items():
        if sha(source / name) != digest:
            raise ValueError("Changed reference dataset " + name)
    catalog, by_value, by_city = {}, defaultdict(list), defaultdict(list)
    for row in csv.DictReader(csv_path.open()):
        entity = {"id": "R" + row[""], "name": row["Name"], "city": row["City"],
                  "cost": float(row["Average Cost"]), "rating": float(row["Aggregate Rating"]),
                  "cuisines": sorted(x.strip() for x in row["Cuisines"].split(","))}
        catalog[entity["id"]] = entity
        by_value[entity_key(entity["name"] + ", " + entity["city"])].append(entity)
        by_city[entity["city"]].append(entity)
    cuisines = sorted({c for e in catalog.values() for c in e["cuisines"]})
    states = {}
    for row in readl(source / "inputs.jsonl"):
        states.setdefault(row["query_id"], row["input"]["state_text"])
    cards = {}
    for row in readl(source / "provenance.jsonl"):
        c = row["candidate"]
        if c["slot"] in ("breakfast", "lunch", "dinner") and c["prior_round"] > 0 and len(by_value[entity_key(c["value"])]) == 1:
            cards[c["memory_id"]] = dict(c, source_row_id=row["source_row_id"])
    queries = readl(source / "queries.jsonl")
    split_plan = json.loads((source / "split-plan.json").read_text())
    groups = split_plan["groups"]
    dev_groups = {min((g for g in groups if g["split"] == "dev" and g["days"] == days),
                      key=lambda g: key(g["source_group"]))["source_group"] for days in (3, 5, 7)}
    wanted = [g for g in groups if g["split"] == "test" or g["source_group"] in dev_groups]
    eligible = defaultdict(list)
    excluded = Counter()
    for q in queries:
        if q["source_group"] not in {g["source_group"] for g in wanted}:
            continue
        ref = q["reference"]
        if ref["slot"] not in ("breakfast", "lunch", "dinner"):
            continue
        clause = states[q["query_id"]].split("Reference to assess:\n", 1)[1]
        spec = constraints(clause, ref["kind"], cuisines)
        if spec is None:
            excluded["unsupported_or_ambiguous_constraints"] += 1
            continue
        pool = [c for c in cards.values() if c["source_row_id"] == q["source_row_id"] and c["prior_round"] < q["current_round"]]
        selected = choose_candidates(pool, ref, q["query_id"])
        if not selected:
            excluded["no_generated_prior_slot_or_fewer_than_six_restaurant_records"] += 1
            continue
        eligible[(q["source_group"], ref["kind"])].append((q, spec, selected))
    cases, judgments, source_jobs = [], [], {}
    for group in wanted:
        for kind in ("reuse_reference", "comparison_reference"):
            choices = eligible[(group["source_group"], kind)]
            if not choices:
                raise ValueError("No eligible case for group and kind " + str((group, kind)))
            q, spec, selected = min(choices, key=lambda v: key(v[0]["query_id"]))
            reference = next(c for c in selected if all(c[k] == q["reference"][k] for k in ("person", "day", "slot")))
            city = by_value[entity_key(reference["value"])][0]["city"]
            options = [e["id"] for e in sorted(by_city[city], key=lambda e: key(q["query_id"] + e["id"]))]
            candidate_metadata = []
            for c in selected:
                meta = {k: c[k] for k in ("memory_id", "person", "day", "slot", "prior_round")}
                candidate_metadata.append(meta)
                original = by_value[entity_key(c["value"])][0]
                menu = sorted(by_city[original["city"]], key=lambda e: key(c["memory_id"] + e["id"]))[:6]
                cuisine = sorted(menu[0]["cuisines"], key=lambda s: key(c["memory_id"] + s))[0]
                source_jobs[c["memory_id"]] = {**meta, "source_row_id": c["source_row_id"],
                    "menu_ids": [e["id"] for e in menu], "cuisine": cuisine, "city": original["city"],
                    "goal": "Choose the cheapest listed restaurant serving the requested cuisine; break equal costs by higher rating, then lexicographically smaller ID."}
            cases.append({"case_id": q["query_id"], "split": q["split"], "source_group": q["source_group"],
                          "state_text": states[q["query_id"]], "clause": states[q["query_id"]].split("Reference to assess:\n", 1)[1],
                          "current_round": q["current_round"], "city": city, "option_ids": options,
                          "candidates": candidate_metadata})
            judgments.append({"case_id": q["query_id"], "reference_memory_id": reference["memory_id"],
                              "reference": q["reference"], "constraints": spec})
    used = {e for s in source_jobs.values() for e in s["menu_ids"]} | {e for c in cases for e in c["option_ids"]}
    output.mkdir(parents=True)
    write(output / "cases.json", cases)
    write(output / "source-jobs.json", sorted(source_jobs.values(), key=lambda x: key(x["memory_id"])))
    write(output / "catalog.json", {k: catalog[k] for k in sorted(used)})
    write(output / "judgments.json", judgments)
    write(output / "protocol.json", {
        "version": "travel-worker-utility-v1", "official_memoryarena_score": False,
        "history": "new_worker_choices_on_constructed_restaurant_menus", "current_clauses": "unchanged_original_reference_clauses",
        "source_task_is_original_full_trip": False, "current_task_is_original_full_trip": False,
        "policies": ["none", "all", "bm25", "learned", "rule"],
        "current_conditions": ["memory_needed", "reference_visible_now"],
        "selection_threshold": 0.25, "selector_frozen_before_this_run": True,
        "success": "any listed restaurant satisfying all local constraints relative to actual generated prior choice; NONE iff no feasible listed restaurant",
        "invalid_source": "record without usable restaurant; preserve case as unavailable-history and report separately; do not replace with benchmark answer",
        "repeat": "repeat all five policies for the first case per split in memory_needed condition",
        "counts": {"base_clauses": dict(Counter(c["split"] for c in cases)), "source_jobs": len(source_jobs),
                   "eligible_by_group_kind": {g + ":" + k: len(v) for (g, k), v in eligible.items()}, "excluded": dict(excluded)},
        "data_origin": {"reference_index_sha256": sha(source / "artifact-index.json"), "restaurant_csv_sha256": sha(csv_path),
                        "restaurant_csv_upstream_commit": "6cd9de14b71915e39ac742a20dc33785e14b6aab"},
        "selection": "one reuse and one supported comparison per group, SHA256 order; 3 dev and 8 test base plans; no worker outcomes used"})
    write(output / "manifest.json", {"sha256": {p.name: sha(p) for p in sorted(output.iterdir())},
                                    "generator_sha256": sha(__file__)})
    return json.loads((output / "protocol.json").read_text())["counts"]


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    for name in ("source", "csv", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(build(a.source, a.csv, a.output), indent=2))
