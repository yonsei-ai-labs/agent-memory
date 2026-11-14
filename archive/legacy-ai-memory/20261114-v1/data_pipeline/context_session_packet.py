"""Build a new, offline-only four-question session packet from the frozen pilot."""
from __future__ import annotations

import argparse
import gzip
from importlib.metadata import version
import json
from pathlib import Path
import re

from evaluation.context_session_inputs import (
    CODE_FILES, DEFAULT_PACKET, FACTS, MODES, POLICIES, ROOT, SOURCE, SOURCE_SHA,
    ReferenceTokens, canonical, digest, readl, record_key, require, text_sha,
    verify_parent, write, writel,
)

WORKER_INSTRUCTION = (
    "Answer read-only questions about the observed trip log and catalog. Do not execute bookings or change the world. "
    "Observed records and additional confirmed past records are external facts. Earlier assistant answers are "
    "unverified previous answers, not booking confirmations; check them against external facts. "
    "A later change replaces the earlier booking for the same traveller and day. A booking that will not be used "
    "is not in force. An extra visit does not replace a booking. A one-time exception does not replace a persistent "
    "preference. For a first/original booking, report the earliest one even if it later changed or ceased to be used. "
    "For a hypothetical recommendation, choose any available catalog ID satisfying all established persistent "
    "preferences and the named constraints. A recommendation for a new day does not require an existing booking; "
    "reporting an ID does not create one. Apply the displayed catalog attributes even when names suggest otherwise. "
    "If the supplied evidence cannot establish a required booking or preference for a component, return UNKNOWN "
    "for that component. Return only a JSON object with exactly two string fields: dinner and accommodation."
)


def chunks(text, count, cap=320):
    result = []
    while text:
        if count(text) <= cap:
            result.append(text)
            break
        low, high = 1, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if count(text[:mid]) <= cap:
                low = mid
            else:
                high = mid - 1
        require(count(text[:low]) <= cap, "A character exceeds the fixed chunk cap")
        result.append(text[:low])
        text = text[low:]
    return result


def select_questions(cases):
    """Fix four questions by source kind and chronology, without worker results."""
    first = lambda kind: min((c for c in cases if c["kind"] == kind), key=lambda c: c["index"])
    history = first("history")
    following = min((c for c in cases if c["kind"] == "ref_changed" and c["index"] > history["index"]),
                    key=lambda c: c["index"])
    chosen = sorted([first("absent"), first("open_book"), history, following], key=lambda c: c["index"])
    require(len({c["state_id"] for c in chosen}) == 4, "Repeated setup question")
    return chosen


def question_text(case, event):
    if case["kind"] == "open_book":
        quoted = re.sub(r"^Request \d+\. ", "", case["request_text"])
        return ("Read-only hypothetical recommendation: which dinner and accommodation IDs would satisfy this "
                "request and the traveller's established preferences? An existing booking for the new day is not "
                "required, and choosing IDs does not create a booking.\nQuoted request: " + quoted)
    target = event["target"]
    if case["kind"] == "history":
        return (f"Read-only lookup: which dinner and accommodation IDs were first booked for {target['person']} "
                f"on day {target['day']}? Report the original booking, even if it later changed.")
    # Identical wording for missing and present current bookings avoids an UNKNOWN cue.
    return (f"Read-only lookup: which dinner and accommodation IDs are currently in force for {target['person']} "
            f"on day {target['day']}?")


def prepare(output=DEFAULT_PACKET, source=SOURCE):
    output, source = Path(output), Path(source)
    require(not output.exists(), "Packet exists; choose a new revision or directory")
    verify_parent(source)
    dependency_path = ROOT / "docs/design-data/memory-context-stream-20261112-v1/checks/feasibility.json"
    dependency = json.loads(dependency_path.read_text())
    require(dependency["parent_manifest_sha256"] == SOURCE_SHA and dependency["decisions"] == 92
            and all(dependency[k] == 0 for k in ("original_replay_disagreements", "read_only_replay_disagreements",
                    "prefix_replay_disagreements", "decisions_depending_on_prior_question_answer_evidence")),
            "Read-only answer dependency check is missing or belongs to another revision")
    session = min(readl(source / "sessions.jsonl"), key=lambda s: s["session_id"])
    sid, surface, tier = session["session_id"], "hidden", "M"
    cases = [c for c in readl(source / "cases.jsonl")
             if c["session_id"] == sid and c["surface"] == surface and c["tier"] == tier]
    chosen = select_questions(cases)
    events = {e["index"]: e for e in readl(source / "events.jsonl") if e["session_id"] == sid}
    records = sorted([r for r in readl(source / "records.jsonl")
                      if r["session_id"] == sid and r["surface"] == surface and r["tier"] == tier],
                     key=lambda r: (r["index"], r["memory_id"]))
    catalog = None
    none_hash = chosen[0]["conditions"]["none"]["input_sha256"]
    with gzip.open(source / "inputs-S.jsonl.gz", "rt") as stream:
        for line in stream:
            row = json.loads(line)
            if row["input_sha256"] == none_hash:
                catalog = row["messages"][-1]["content"].split("Session task:", 1)[0].rstrip()
                break
    require(catalog is not None and "Catalog columns:" in catalog, "Public catalog missing")
    count = ReferenceTokens()
    provenance, observations, keys_by_mid = [], [], {}
    for ordinal, record in enumerate(records, 1):
        event = events.get(record["index"])
        kind = event["kind"] if event else "background"
        identity = [SOURCE_SHA, sid, surface, tier, record["memory_id"]]
        body_sha = text_sha(record["text"])
        key = record_key(identity, body_sha)
        kept = kind in FACTS or kind == "background"
        provenance.append({"record_key": key, "source_identity": identity, "source_text_sha256": body_sha,
                           "at": record["index"], "source_kind": kind, "kept": kept,
                           "exclusion_reason": None if kept else "scripted_question_answer"})
        if not kept:
            continue
        keys_by_mid[record["memory_id"]] = key
        for part, text in enumerate(chunks(record["text"], count)):
            observations.append({"unit_id": f"obs-{ordinal:03d}-part{part:03d}", "record_key": key,
                                 "at": record["index"], "part": part, "text": text, "text_sha256": text_sha(text)})
    questions, labels = [], []
    parent_labels = readl(source / "relations.jsonl")
    source_labels = {r["state_id"]: r for r in parent_labels}
    for ordinal, case in enumerate(chosen, 1):
        query_id = f"q{ordinal:02d}"
        questions.append({"query_id": query_id, "at": case["index"],
                          "text": question_text(case, events[case["index"]])})
        label = source_labels[case["state_id"]]
        candidates = []
        for record in records:
            mid = record["memory_id"]
            if mid not in keys_by_mid or record["index"] >= case["index"]:
                continue
            annotation = label["candidate_relations"].get(mid)
            supports = annotation["judge_with"] if annotation else []
            require(set(supports) <= keys_by_mid.keys(), "Judgment requires excluded or missing source records")
            candidates.append({"record_key": keys_by_mid[mid], "relation": annotation["relation"] if annotation else None,
                               "judge_with_record_keys": [keys_by_mid[m] for m in supports], "loss_mask": int(annotation is not None)})
        required = [keys_by_mid[mid] for mid in label["required_memory_ids"]]
        unknown = all(values == ["UNKNOWN"] for values in label["acceptable"].values())
        labels.append({"query_id": query_id, "source_state_id": case["state_id"], "source_kind": case["kind"],
                       "acceptable": label["acceptable"], "answer_kind": "unknown" if unknown else "catalog_ids",
                       "evidence_requirement": "past_fact" if required else "no_required_positive_record",
                       "required_record_keys": required, "candidate_relations": candidates,
                       "utility": None, "ambiguous": None, "training_approved": False})
    config = {"format": "context-session-offline-v2", "parent_manifest_sha256": SOURCE_SHA,
              "surface": surface, "tier": tier, "reference_encoding": "o200k_base",
              "tiktoken_version": version("tiktoken"), "reference_encoding_is_native": False,
              "recent_only_budget": 4096, "recent_memory_budget": 2048, "memory_budget": 2048,
              "full_input_reference_cap": 6144, "chunk_reference_cap": 320,
              "modes": list(MODES), "policies": list(POLICIES), "live_enabled": False, "max_new_model_calls": 0}
    contract = {
        "version": "read-only-memory-evaluation-v2", "worker_instruction": WORKER_INSTRUCTION,
        "scope": "One previously inspected design family and four questions. Offline preparation only; dev, no training.",
        "measurements": {
            "relation": "Script-defined needed/invalid/distractor/irrelevant roles. Unannotated candidates are null with loss_mask=0.",
            "local_utility": "Only observed provide/withhold outcomes from the exact same predecision state and worker contract can define utility.",
            "session_effect": "Cumulative versus isolated trajectories include the history and fact-displacement effects of each policy.",
        },
        "unknown_policy": {
            "answer_kind_is_not_memory_need": True,
            "description": "UNKNOWN can require an invalidation record. An UNKNOWN answer never implies a no-memory worker succeeds.",
            "completion": "UNKNOWN is task-correct only where the acceptable answer set includes UNKNOWN.",
            "with_missing_evidence": "UNKNOWN for an otherwise concrete gold answer is unresolved task completion; report abstention separately from wrong concrete claims.",
        },
        "record_identity_fields": ["parent_manifest_sha256", "session_id", "surface", "tier", "memory_id", "source_text_sha256"],
        "input_rules": {
            "confirmed_facts": "Only public external observations enter the immutable fact bank; scripted answers and worker answers never enter it.",
            "cumulative_history": "Keep actual prior question/answer turns under the same chronological bounded-window rule; never substitute gold answers.",
            "recent_window": "Take the newest complete groups until the next older group does not fit; do not skip an oversized newest group.",
            "memory_selection": "BM25 over public chunks outside the recent window; query text only, fixed tie order, no label-derived support injection.",
            "missing_support": "judge_with is private audit metadata. If a future selector omits a necessary premise, record insufficient evidence rather than invent a negative label.",
            "never_model_input": ["labels.jsonl", "record-provenance.jsonl", "protocol.json", "manifest.json", "contract metadata other than worker_instruction"],
        },
        "budget_rules": {
            "main_comparison": "Recent-only 4096 versus recent 2048 plus selected memory 2048, under a shared 4096 variable-input budget.",
            "serialization": "Count canonical JSON of the complete messages, including role wrappers, separators, record IDs and memory header.",
            "memory": "Both the rendered memory block and its serialized full-input increment must fit 2048 reference tokens.",
            "common_input": "Catalog, common instruction and current question are outside the shared variable allocation but inside the shared 6144 full-input cap.",
            "native_limit": "Reference counts are not native chat-template counts. A future live setup must fix the worker and native input contract first.",
        },
        "future_execution_gates": ["Independent semantic review remains pending.",
            "Fix model revision, decoding, native counting/caps and repeat/cache contract before a small live setup.",
            "Record actual cumulative exposure; offline fixtures alone cannot establish it for a real trajectory.",
            "Pair local outcomes by the complete predecision snapshot and worker contract; do not pair different policy trajectories.",
            "Keep the four parent sessions as design dev; reserve different source families before confirmatory evaluation."],
    }
    protocol = {"format": "context-session-offline-protocol-v2", "parent_manifest_sha256": SOURCE_SHA,
                "source_session_id": sid, "source_family_id": session["family_id"], "split": "design_dev",
                "independent_source_families": 1, "independent_sources_added": 0,
                "selection_rule": "Smallest session ID; first absent, first open_book, first history, then first ref_changed after that history; sort by source time.",
                "source_question_times": [c["index"] for c in chosen],
                "schedule_change_from_v1": "Replace the earlier ref_changed at 39 by the first ref_changed after history (48); the history/current pair has no intervening external observation.",
                "selection_uses_worker_outputs": False, "external_event_times_modified": False,
                "original_question_labels_reused_after_answer_dependency_check": True,
                "parent_dependency_check": "docs/design-data/memory-context-stream-20261112-v1/checks/feasibility.json",
                "parent_dependency_check_sha256": digest(dependency_path.read_bytes()),
                "parent_unknown_answer_states": sum(all(v == ["UNKNOWN"] for v in l["acceptable"].values()) for l in parent_labels),
                "parent_unknown_states_with_required_evidence": sum(all(v == ["UNKNOWN"] for v in l["acceptable"].values()) and bool(l["required_memory_ids"]) for l in parent_labels),
                "offline_fixtures": ["Constant UNKNOWN JSON", "Label-independent near-256-reference-token JSON"],
                "fixture_answers_are_not_model_outputs": True, "fixture_answers_graded": False,
                "independent_human_semantic_review_complete": False, "utility_measured": False,
                "original_packet_modified": False, "full_benchmark_started": False}
    output.mkdir(parents=True)
    write(output / "session.json", {"session_id": sid, "family_id": session["family_id"], "catalog_text": catalog})
    writel(output / "observations.jsonl", observations)
    writel(output / "record-provenance.jsonl", provenance)
    writel(output / "questions.jsonl", questions)
    writel(output / "labels.jsonl", labels)
    write(output / "config.json", config)
    write(output / "contract.json", contract)
    write(output / "protocol.json", protocol)
    write(output / "manifest.json", {"format": "context-session-offline-v2", "parent_manifest_sha256": SOURCE_SHA,
          "code_sha256": {name: digest((ROOT / name).read_bytes()) for name in CODE_FILES},
          "files": {p.name: digest(p.read_bytes()) for p in sorted(output.iterdir())}})
    return {"packet": str(output), "source_families": 1, "questions": len(questions),
            "question_times": [q["at"] for q in questions], "observed_chunks": len(observations), "model_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_PACKET)
    parser.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output, args.source), ensure_ascii=False, indent=2))
