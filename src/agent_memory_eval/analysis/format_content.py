"""Masked semantic grading and paired summaries for the four-format decision comparison."""

from collections import Counter
from copy import deepcopy
import math
import re

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import FORMS


FIELDS = ("free", "paid", "fee_usd")


def review_packet(raw, profile):
    entries = {}
    for row in raw["rows"]:
        score = row["score"]
        if score is None or not score.get("review_id"):
            continue
        entries[score["review_id"]] = dict(case=row["case"], text=score["public_text"])
    return dict(format="agent-memory-baggage-text-review/v1",
        queries={k: v["text"] for k, v in profile["cases"].items() if v["kind"] == "baggage"},
        unit="Distinct trimmed customer-facing text within case. Assignment, frequency, memory and ordering withheld.",
        replies={key: entries[key] for key in sorted(entries)})


def literal_quote(text):
    """Only cross-check unambiguous fully structured outputs. All direct texts also get a masked review."""
    try:
        obj = read_json(text.encode())
    except ValueError:
        obj = None
    if isinstance(obj, dict) and set(obj) == set(FIELDS) and all(type(v) is int and v >= 0 for v in obj.values()):
        return obj
    pattern = r"\s*free\s*[=:]\s*(\d+)\s*[,;]\s*paid\s*[=:]\s*(\d+)\s*[,;]\s*fee_usd\s*[=:]\s*\$?(\d+)(?:\.0+)?\s*\.?\s*"
    matched = re.fullmatch(pattern, text, re.I)
    return dict(zip(FIELDS, map(int, matched.groups()), strict=True)) if matched else None


def validate_labels(packet, review):
    if review.get("packet_sha256") != digest(canonical(packet)) or set(review["labels"]) != set(packet["replies"]):
        raise ContractError("Masked coding must cover exactly the frozen packet")
    for key, record in review["labels"].items():
        if set(record) != {"status", "values"} or record["status"] not in {"asserted", "conditional", "unclear", "contradictory"}:
            raise ContractError("Invalid masked price-quote annotation")
        values = record["values"]
        if (not isinstance(values, dict) or set(values) != set(FIELDS) or any(v is not None and
                (type(v) not in (int, float) or not math.isfinite(v)) for v in values.values()) or
                (record["status"] == "asserted" and any(v is None for v in values.values()))):
            raise ContractError("Masked totals must identify every asserted field")
        parsed = literal_quote(packet["replies"][key]["text"])
        if parsed is not None and (record["status"] != "asserted" or values != parsed):
            raise ContractError("Masked coding disagrees with the explicitly structured public quote")


def summarize(raw, profile, review):
    result = deepcopy(raw)
    packet = review_packet(raw, profile)
    validate_labels(packet, review)
    rows = result["rows"]
    for row in rows:
        score, case = row["score"], row["case"]
        if score is None:
            continue
        if profile["cases"][case]["kind"] == "baggage":
            label = review["labels"].get(score["review_id"])
            values = label["values"] if label and label["status"] == "asserted" else None
            score.update(masked_annotation=label, exact_action=values == profile["cases"][case]["expected"],
                         stale_tier_quote=case == "changed_tier" and values == profile["cases"]["previous_tier"]["expected"])
        score["first_action"] = "+".join(c["name"] for c in score["calls"]) or "customer_message"
    counts, paired, by_form = {}, {}, {}
    for case in profile["cases"]:
        counts[case], paired[case], by_form[case] = {}, {}, {}
        base = {r["repeat"]: r["score"] for r in rows if r["arm"] == "empty" and r["case"] == case and r["score"] is not None}
        for arm in profile["arms"]:
            group = {r["repeat"]: r["score"] for r in rows if r["arm"] == arm and r["case"] == case and r["score"] is not None}
            both = group.keys() & base.keys()
            counts[case][arm] = dict(planned=profile["repetitions"], scored=len(group),
                exact=sum(s["exact_action"] for s in group.values()), first_actions=dict(Counter(s["first_action"] for s in group.values())),
                stale_date_search=sum(bool(s.get("stale_date_search")) for s in group.values()),
                stale_tier_quote=sum(bool(s.get("stale_tier_quote")) for s in group.values()),
                unauthorized_mutation=sum(s["unauthorized_mutation"] for s in group.values()))
            paired[case][arm] = dict(paired=len(both),
                gained=sum(group[i]["exact_action"] and not base[i]["exact_action"] for i in both),
                lost=sum(base[i]["exact_action"] and not group[i]["exact_action"] for i in both))
        for form in FORMS:
            written = [counts[case][f"written_{form}_{i}"] for i in (1, 2)]
            by_form[case][form] = dict(matched=counts[case]["matched_" + form]["exact"],
                matched_scored=counts[case]["matched_" + form]["scored"], written=[x["exact"] for x in written],
                written_scored=[x["scored"] for x in written])
    result.update(counts=counts, paired_to_empty=paired, forms=by_form,
                  masked_packet_sha256=digest(canonical(packet)), masked_review_sha256=digest(canonical(review)))
    return result
