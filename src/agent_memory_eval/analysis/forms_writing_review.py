"""Validate masked content annotations before joining them to assigned writing policies."""

from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

from agent_memory_eval.contracts.core import ContractError, read_json


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def analyze_contents(packet_directory, annotations):
    packet_directory = Path(packet_directory)
    keys = read_json((packet_directory / "writing-key.json").read_bytes())
    content, source, reviews = {}, {}, {}
    for i, file in enumerate(annotations, 1):
        masked = read_json((packet_directory / f"writing-{i}.json").read_bytes())
        judged = read_json(Path(file).read_bytes())["banks"]
        if set(masked["banks"]) != set(judged) or set(content) & set(judged):
            raise ContractError("Review coverage differs from its masked packet")
        content.update(masked["banks"])
        source.update(masked["sources"])
        reviews.update(judged)
    if set(reviews) != set(keys):
        raise ContractError("Missing or extra banks before the assignment join")
    joined = []
    buckets = defaultdict(list)
    kinds = ("fact", "episode", "lesson", "procedure")
    for bank, label in reviews.items():
        unit = content[bank]
        notes = unit["notes"]
        src = source[unit["source_id"]]
        if any(type(label[k]) is not bool for k in kinds):
            raise ContractError("Writing content labels must be independent booleans")
        if label["grounding"] not in {"empty", "supported", "mixed", "unverified"}:
            raise ContractError("Unknown content grounding label")
        if notes:
            if label["grounding"] == "empty" or not label["main_note_quote"]:
                raise ContractError("A written bank lacks its note quote or grounding")
            if label["grounding"] in {"supported", "mixed"} and not label["main_source_quote"]:
                raise ContractError("An attributed source claim lacks a public quote")
            if label["grounding"] in {"mixed", "unverified"} and not label["suspect_quote"]:
                raise ContractError("An uncertain claim lacks its note excerpt")
        elif label["grounding"] != "empty" or any(label[k] for k in kinds):
            raise ContractError("Empty bank has nonempty labels")
        for field, material in (("main_note_quote", notes), ("suspect_quote", notes), ("main_source_quote", src)):
            quote = label[field]
            if not isinstance(quote, str) or (quote and not any(quote in text for text in strings(material))):
                raise ContractError(f"{bank}: {field} does not occur in the archived public text")
        meta = keys[bank]
        row = dict(bank=bank, **meta, **label, item_count=len(notes),
                   title_body_utf8_bytes=sum(len(s.encode()) for note in notes for s in (note["title"], note["body"])))
        joined.append(row)
        buckets[meta["arm"]].append(row)
    by_arm = {}
    for arm, rows in buckets.items():
        completed = [r for r in rows if r["status"] in {"completed", "empty"}]
        nonempty = [r for r in completed if r["item_count"]]
        by_arm[arm] = dict(reviewed=len(rows), eligible=len(completed), eligible_nonempty=len(nonempty),
            statuses=dict(Counter(r["status"] for r in rows)),
            overlapping_labels={k: sum(r[k] for r in completed) for k in kinds},
            grounding=dict(Counter(r["grounding"] for r in completed)),
            median_title_body_utf8_bytes=median([r["title_body_utf8_bytes"] for r in completed]) if completed else None)
    return dict(format="agent-memory-two-pass-forms-grounding/v1", banks_reviewed=len(joined),
                by_assigned_arm=by_arm, annotations=sorted(joined, key=lambda row: row["index"]))
