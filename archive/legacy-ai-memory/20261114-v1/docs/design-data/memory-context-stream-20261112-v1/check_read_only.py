"""CPU feasibility check for cumulative sessions with read-only questions.

This is a design check, not a streaming worker runner or a utility evaluation.
Only the exported four-session pilot is read; sealed source content is not read.
"""

import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA = ROOT / "datasets/hidden_memory_traps/pilot-20261112-v1"
QUESTIONS = {
    "ref", "ref_changed", "ref_conditional", "ref_void", "ref_complement",
    "ref_lookalike", "open_book", "absent", "history",
}
FACTS = {"book", "change", "preference", "void", "complement"}
UNKNOWN = {"dinner": ["UNKNOWN"], "accommodation": ["UNKNOWN"]}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def readl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def menu_from_prompt(text):
    entries = {}
    for line in text.split("Session task:", 1)[0].splitlines():
        if not re.match(r"^[RH]\d+ \| ", line):
            continue
        fields = line.split(" | ")
        require(len(fields) == 7, "Ambiguous catalog row")
        ident, _, _, price, _, attrs, available = fields
        entries[ident] = {
            "price": float(price), "attributes": attrs.split(", "),
            "available": available == "true",
        }
    require(len(entries) == 20, "Expected the public 20-entry catalog")
    return entries


def permitted(rule, entry):
    kind, attrs = rule["type"], entry["attributes"]
    if kind == "price_cap":
        return entry["price"] <= rule["max"]
    if kind == "cuisine_only":
        return rule["cuisine"] in attrs
    if kind == "room_type":
        return rule["room"] in attrs
    if kind == "avoid_house_rule":
        rules = {part.strip() for attr in attrs for part in attr.split("&")}
        return rule["rule"] not in rules
    raise ValueError("Unsupported preference type: " + kind)


def replay(events, session, menu, commit_scripted_answers):
    """Compare original scripted bookings with an external-facts-only ledger."""
    bookings, voided, preferences = defaultdict(list), set(), {}
    answers, committed = {}, []

    def active(target):
        key = target["person"], target["day"]
        return bookings[key][-1] if bookings[key] and key not in voided else None

    for event in events:
        kind = event["kind"]
        require(kind in FACTS | QUESTIONS, "Unrecognized event type: " + kind)
        key = event.get("person"), event.get("day")
        answer = None
        if kind == "open_book":
            slot = event["open_slot"]
            category = "restaurant" if slot == "dinner" else "hotel"
            answer = {s: [p] for s, p in event["named"].items()}
            answer[slot] = [p for p in session["menu"][category]
                            if menu[p]["available"] and permitted(preferences[event["person"]], menu[p])]
        elif kind == "history":
            target = event["target"]
            old = bookings[target["person"], target["day"]]
            answer = {s: [p] for s, p in old[0].items()} if old else dict(UNKNOWN)
        elif kind in QUESTIONS:
            picked = active(event["target"])
            if kind == "ref_conditional" and picked is None:
                picked = active(event["fallback"])
            answer = {s: [p] for s, p in picked.items()} if picked else dict(UNKNOWN)

        if answer is not None:
            require(set(answer) == set(UNKNOWN) and all(answer.values()), "Incomplete answer set")
            answers[event["index"]] = answer
        if kind == "preference":
            preferences[event["person"]] = event["rule"]
        elif kind in {"book", "change"}:
            bookings[key].append(event["places"])
            voided.discard(key)
        elif kind == "void":
            voided.add(key)
        elif (commit_scripted_answers and kind in QUESTIONS - {"history"}
              and answer != UNKNOWN):
            bookings[key].append({s: answer[s][0] for s in UNKNOWN})
            voided.discard(key)
            committed.append(event["index"])
        # Complement observations do not replace the original booking.
    return answers, committed


def run(data, output):
    require(not output.exists(), "Output exists; choose a new directory")
    raw_manifest = (data / "manifest.json").read_bytes()
    manifest = json.loads(raw_manifest)
    for name, info in manifest["files"].items():
        raw = (data / name).read_bytes()
        require(digest(raw) == info["sha256"] and len(raw) == info["bytes"], "Changed raw data: " + name)
    sessions = {s["session_id"]: s for s in readl(data / "sessions.jsonl")}
    events = defaultdict(list)
    for event in readl(data / "events.jsonl"):
        events[event["session_id"]].append(event)
    labels = {(r["session_id"], r["index"]): r for r in readl(data / "relations.jsonl")}
    require(len(labels) == len(readl(data / "relations.jsonl")), "Duplicate decision identity")
    none_hashes = {c["conditions"]["none"]["input_sha256"]: c["session_id"]
                   for c in readl(data / "cases.jsonl") if c["tier"] == "S"}
    menus = {}
    with gzip.open(data / "inputs-S.jsonl.gz", "rt") as stream:
        for line in stream:
            row = json.loads(line)
            if row["input_sha256"] in none_hashes:
                sid = none_hashes[row["input_sha256"]]
                menu = menu_from_prompt(row["messages"][-1]["content"])
                require(sid not in menus or menus[sid] == menu, "Catalog changes within session")
                menus[sid] = menu
    require(set(menus) == set(sessions), "Missing public catalog")
    rows, session_counts = [], []
    reference_kinds, commits = Counter(), 0
    for sid, session in sorted(sessions.items()):
        stream = events[sid]
        require([e["index"] for e in stream] == sorted({e["index"] for e in stream}), "Unordered/repeated event")
        original, applied = replay(stream, session, menus[sid], True)
        read_only, _ = replay(stream, session, menus[sid], False)
        require(set(original) == {i for s, i in labels if s == sid}, "Decision coverage differs")
        commits += len(applied)
        by_index = {e["index"]: e for e in stream}
        for index, answer in sorted(read_only.items()):
            label = labels[sid, index]
            prefix = [e for e in stream if e["index"] <= index]
            prefix_answers, _ = replay(prefix, session, menus[sid], False)
            required_kinds = []
            for memory_id in label["required_memory_ids"]:
                require(memory_id.startswith(sid + "-t"), "Cross-session evidence")
                target_index = int(memory_id.rsplit("-t", 1)[1])
                require(target_index < index, "Future evidence")
                required_kinds.append(by_index[target_index]["kind"])
            reference_kinds.update(required_kinds)
            rows.append({
                "state_id": label["state_id"], "session_id": sid, "index": index,
                "kind": label["kind"], "acceptable": label["acceptable"],
                "original_replay_matches": original[index] == label["acceptable"],
                "read_only_replay_matches": answer == label["acceptable"],
                "prefix_replay_matches": prefix_answers[index] == answer,
                "required_memory_kinds": required_kinds,
                "required_memory_comes_from_question_answer": bool(set(required_kinds) & QUESTIONS),
                "prior_confirmed_fact_events": sum(e["kind"] in FACTS for e in prefix[:-1]),
                "prior_scripted_answer_commits_omitted": sum(i < index for i in applied),
            })
        session_counts.append({"session_id": sid, "family_id": session["family_id"],
                               "events": len(stream), "external_fact_events": sum(e["kind"] in FACTS for e in stream),
                               "questions": len(read_only), "scripted_answer_commits_omitted": len(applied)})
    original_errors = sum(not r["original_replay_matches"] for r in rows)
    read_only_errors = sum(not r["read_only_replay_matches"] for r in rows)
    prefix_errors = sum(not r["prefix_replay_matches"] for r in rows)
    dependencies = sum(r["required_memory_comes_from_question_answer"] for r in rows)
    require(not (original_errors or read_only_errors or prefix_errors or dependencies), "Read-only feasibility check failed")
    summary = {
        "format": "memory-context-stream-feasibility-v1", "date": "2026-11-12",
        "parent_manifest_sha256": digest(raw_manifest), "raw_files_verified": len(manifest["files"]),
        "checker_sha256": digest(Path(__file__).read_bytes()),
        "sessions": session_counts, "decisions": len(rows),
        "original_replay_disagreements": original_errors,
        "read_only_replay_disagreements": read_only_errors,
        "prefix_replay_disagreements": prefix_errors,
        "scripted_answer_commits_omitted": commits,
        "decisions_depending_on_prior_question_answer_evidence": dependencies,
        "required_evidence_event_occurrences_by_kind": dict(sorted(reference_kinds.items())),
        "unknown_answers": sum(r["acceptable"] == UNKNOWN for r in rows),
        "unknown_answers_with_required_evidence": sum(r["acceptable"] == UNKNOWN and bool(r["required_memory_kinds"]) for r in rows),
        "worker_calls": 0, "training_runs": 0, "sealed_source_content_read": False,
        "scope": "Structured answer-dependency check only; no new question wording, stream schedules, worker inputs, or utility labels are validated.",
    }
    output.mkdir(parents=True)
    (output / "feasibility.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    (output / "read-only-answer-checks.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))
    print(json.dumps({k: v for k, v in summary.items() if k != "sessions"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.data, args.output)
