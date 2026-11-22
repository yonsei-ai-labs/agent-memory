"""Relate delivered note wording to premature handoffs across the sealed retail second-pass studies."""

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.journal import safe_path
from scripts.analyze_recheck_rounds import MUTATIONS
from scripts.analyze_retail_full_factors import (
    business_events,
    transfer_position,
    verified,
)
from scripts.analyze_retail_sample_expansion import bootstrap_customer_interval

# Wording that sends a delivered note to a blinded reader. A note without it counts as not
# mentioning a handoff; the verdict for every other note comes from the annotation file.
CANDIDATE = re.compile(
    r"transfer|human|escalat|live (agent|person)|hand ?off|handed off|specialist|supervisor"
    r"|representative|manager|staff|\brefer(red|ring)? (the|this|them|him|her|it|to)\b"
    r"|hand(ed|ing)? (it |this |them |the case |the customer )?(over|off)|out of scope"
    r"|outside (the |its |available )?scope|beyond (the |its |available )?scope"
    r"|cannot be handled|can't be handled|another (team|department|agent|channel)|support team",
    re.IGNORECASE,
)
STANCES = ("recommends", "discourages", "mixed", "records")
INITIATORS = ("customer", "agent", "unannounced")
EVIDENCE = Path("artifacts/analyses")
INPUTS = {
    "20261119-retail-full-cells.json": "34d877c4d778cee4a39490a640ab11dcd9455ec991df44788fc29f13663907e2",
    "20261120-retail-sample-expansion-cells.json": "ff32b8b009456f4bd1efee362dc1b3ef8fbd2df08079a1eccf81f55bd093f31b",
    "20261121-retail-related-transfer-results.json": "faf315af21ff984d5760fd709bce9519a42c9fd9d23dd377e9dd83d9cd260c84",
    "20261121-retail-transfer-revision-results.json": "e3a25bf8d0f9ce2392bdc50ba3cc776015e48e76bc71498b71a37b20fe1afa58",
}
# Blinded verdicts on note content and on who first raised each early handoff.
ANNOTATIONS = (
    "20261122-retail-handoff-annotations.json",
    "7a212765fe5d40e41e2187a43f0970534e9eb4f2dcb7f4a8c463715ce0a04402",
)
CONTROLS = ("no_memory", "empty_preload")
CONDITIONS = (*CONTROLS, "memo")
GROUPS = (*CONTROLS, "writer_empty", "note_plain", "note_escalation")
MEMO = GROUPS[2:]
NOTES = GROUPS[3:]
EARLY = "transfer_without_successful_change"
WORDING_PAIRS = (
    ("note_escalation", "no_memory"),
    ("note_plain", "no_memory"),
    ("note_escalation", "empty_preload"),
    ("note_plain", "empty_preload"),
    ("note_escalation", "note_plain"),
    ("writer_empty", "empty_preload"),
    ("writer_empty", "no_memory"),
    ("empty_preload", "no_memory"),
)


def require(test, message):
    if not test:
        raise ContractError(message)


def outcome_kind(official, db_match, position):
    """Name a graded run's result; a missing score is never converted into a failure."""
    require(type(official) is bool, "An ungraded run has no outcome kind")
    if official:
        return "success"
    require(type(db_match) is bool, "A failed run needs its database verdict")
    if db_match:
        return "explanation_only"
    if position == EARLY:
        return "early_handoff"
    return "database_mismatch" if position == "no_transfer" else "handoff_with_changes"


def note_label(items, labels):
    """Return the blinded verdict for a delivered note after checking it against the note text."""
    if not items:
        return None
    label = labels.get(digest(canonical(items)))
    parts = [part for item in items for part in (item["title"], item["body"])]
    if label is None:
        require(
            not any(CANDIDATE.search(part) for part in parts),
            "A note with handoff wording has no verdict",
        )
        return None
    require(type(label["mentions_handoff"]) is bool, "A note verdict is not boolean")
    require(
        label["stance"] in STANCES
        if label["mentions_handoff"]
        else label["stance"] is None,
        "Note stance and mention verdict disagree",
    )
    require(
        all(any(quote in part for part in parts) for quote in label["evidence"]),
        "Note evidence is not in the note",
    )
    return label


def note_group(arm, items, label):
    """Classify a run by its arm and by the verdict on the notes actually delivered to it."""
    if arm == "no_memory":
        require(not items, "A no-memory run received notes")
        return arm
    require(items is not None, "A preload run has no delivery record")
    if arm == "empty_preload":
        require(not items, "The empty control received notes")
        return arm
    if not items:
        return "writer_empty"
    return "note_escalation" if label and label["mentions_handoff"] else "note_plain"


def spoken(message):
    """Public text of a message; worker replies wrap their text in a JSON object."""
    content = message.get("content") or ""
    try:
        value = json.loads(content)
    except ValueError:
        return content
    return (
        str(value["message"])
        if isinstance(value, dict) and "message" in value
        else content
    )


def initiator_label(messages, turn, label):
    """Check a verdict on who first raised the handoff before the transfer call at ``turn``."""
    who, index = label["label"], label["message_index"]
    require(who in INITIATORS, "Unknown handoff initiator")
    if who == "unannounced":
        require(index is None, "An unannounced handoff cites a message")
        return who
    require(
        type(index) is int and 0 <= index < turn,
        "Initiator evidence is not before the handoff",
    )
    message = messages[index]
    require(
        message["role"] == "user"
        if who == "customer"
        else message["role"] == "assistant" and not message.get("tool_calls"),
        "Initiator evidence comes from the wrong speaker",
    )
    require(
        bool(label["quote"]) and label["quote"] in spoken(message),
        "Initiator quote is not in the cited message",
    )
    return who


def paired(cells, left, right, value):
    """Compare two note groups inside each study, task and repeat; customers weigh equally."""
    units = defaultdict(lambda: ([], []))
    for cell in cells:
        if cell["official"] is None:
            continue
        side = 0 if cell["group"] in left else 1 if cell["group"] in right else None
        if side is not None:
            key = (cell["customer"], cell["study"], cell["task"], cell["stage"])
            units[key][side].append(int(value(cell)))
    counts = Counter()
    by_task = defaultdict(list)
    for (customer, study, task, _), (lefts, rights) in units.items():
        for a in lefts:
            for b in rights:
                counts[(a, b)] += 1
                by_task[(customer, study, task)].append(a - b)
    by_customer = defaultdict(list)
    for (customer, _, _), observations in by_task.items():
        by_customer[customer].append(mean(observations))
    values = [mean(scores) for _, scores in sorted(by_customer.items())]
    return {
        "pairs": sum(counts.values()),
        "left_only": counts[(1, 0)],
        "right_only": counts[(0, 1)],
        "both": counts[(1, 1)],
        "neither": counts[(0, 0)],
        "scored_tasks": len(by_task),
        "scored_customers": len(values),
        "customer_difference": mean(values) if values else None,
        "customer_interval": bootstrap_customer_interval(values),
    }


def read_run(workspace, cell):
    """Read one run's delivered notes and graded trajectory from hash-checked original records."""
    journal = safe_path(workspace, cell["journal"])
    items = trajectory = None
    for line in verified(journal, cell["journal_sha256"]).splitlines():
        for event in read_json(line)["events"]:
            if event["run_id"] != cell["run"]:
                continue
            payload = event["payload"]
            if event["type"] == "memory.preloaded":
                ref = payload["public_message_ref"]
                message = read_json(
                    verified(safe_path(journal.parent, ref["path"]), ref["sha256"])
                )
                delivered = read_json(message["content"].encode())["items"]
                items = [
                    {"title": item["title"], "body": item["body"]} for item in delivered
                ]
            elif event["type"] == "outcome.scored" and payload.get(
                "official_artifact_ref"
            ):
                ref = payload["official_artifact_ref"]
                trajectory = read_json(
                    verified(safe_path(journal.parent, ref["path"]), ref["sha256"])
                )
    return items, trajectory


def describe(study, repeat, audited, items, trajectory, annotations):
    """Join one audited cell with the notes it received and the behavior in its trajectory."""
    arm = audited["arm"]
    label = note_label(items, annotations["notes"])
    group = note_group(arm, items, label)
    row = {
        "study": study,
        "repeat": repeat,
        "task": audited["task"],
        "stage": audited["stage"],
        "customer": audited["customer"],
        "source": None if arm in CONTROLS else audited.get("source", audited["task"]),
        "arm": arm,
        "run": audited["run"],
        "journal": audited["journal"],
        "job": int(Path(audited["journal"]).parent.name),
        "official": audited["official"],
        "condition": arm if arm in CONTROLS else "memo",
        "group": group,
        "stance": label["stance"] if group == "note_escalation" else None,
        "note_sha256": digest(canonical(items)) if items else None,
        "kind": None,
        "position": None,
        "initiator": None,
        "reference_changes": None,
        "multi_item_change": None,
    }
    if trajectory is not None:
        require(
            trajectory["task_id"] == audited["task"],
            "Trajectory belongs to another task",
        )
        updates, transfers = business_events(trajectory["messages"])
        row["position"] = transfer_position(updates, transfers)
        verdict = annotations["initiators"].get(audited["run"])
        if transfers and verdict is not None:
            row["initiator"] = initiator_label(
                trajectory["messages"], transfers[0]["turn"], verdict
            )
        row["reference_changes"] = any(
            check["action"]["name"] in MUTATIONS
            for check in trajectory["reward_info"]["action_checks"] or ()
        )
        row["multi_item_change"] = any(
            update["success"]
            and update["name"] == "modify_pending_order_items"
            and len(update["args"]["item_ids"]) > 1
            for update in updates
        )
    if audited["official"] is not None:
        require(trajectory is not None, "A graded run has no trajectory")
        row["kind"] = outcome_kind(
            audited["official"], audited["grade"]["db_match"], row["position"]
        )
    return row


def succeeded(cell):
    return cell["official"]


def handed_off_early(cell):
    return cell["position"] == EARLY


def agent_initiated(cell):
    return handed_off_early(cell) and cell["initiator"] in ("agent", "unannounced")


def wording_counts(cells):
    return {
        "runs": len(cells),
        "early_handoffs": sum(map(handed_off_early, cells)),
        "agent_initiated": sum(map(agent_initiated, cells)),
        "customer_requested": sum(
            handed_off_early(c) and c["initiator"] == "customer" for c in cells
        ),
        "successes": sum(map(succeeded, cells)),
    }


def contrasts(rows, left, right):
    return {
        "success": paired(rows, {left}, {right}, succeeded),
        "early_handoff": paired(rows, {left}, {right}, handed_off_early),
        "agent_initiated": paired(rows, {left}, {right}, agent_initiated),
    }


def by_group(cells, key, order, measure):
    found = {name: [c for c in cells if c[key] == name] for name in order}
    return {name: measure(group) for name, group in found.items() if group}


def kinds(cells):
    return dict(sorted(Counter(c["kind"] for c in cells).items()))


def repeat_discordance(cells):
    """Count same-task, same-arm repeat pairs whose official outcomes differ."""
    scores = defaultdict(dict)
    for cell in cells:
        if cell["official"] is not None:
            scores[(cell["arm"], cell["task"])][cell["repeat"]] = cell["official"]
    totals = defaultdict(lambda: [0, 0])
    for (arm, _), repeats in sorted(scores.items()):
        values = [repeats[k] for k in sorted(repeats)]
        for i, first in enumerate(values):
            for second in values[i + 1 :]:
                totals[arm][0] += first != second
                totals[arm][1] += 1
    return {arm: pair for arm, pair in totals.items() if pair[1]}


def summarize(cells):
    """Summarize graded second-pass runs; first-pass cells only split the same-task study."""
    second = [c for c in cells if c["repeat"] >= 1]
    graded = [c for c in second if c["official"] is not None]
    needed = [c for c in graded if c["changes_required"]]
    studies = sorted({c["study"] for c in second})

    def scoped(rows):
        return {
            "all": rows,
            **{s: [c for c in rows if c["study"] == s] for s in studies},
        }

    assigned = {
        (c["study"], c["task"], c["stage"])
        for c in second
        if c["group"] == "note_escalation"
    }
    split = {
        name: [
            c
            for c in needed
            if ((c["study"], c["task"], c["stage"]) in assigned) is wanted
        ]
        for name, wanted in (
            ("with_escalation_notes", True),
            ("without_escalation_notes", False),
        )
    }
    same_task = [c for c in cells if c["study"] == "same_task"]
    first = {c["task"]: c["official"] for c in same_task if c["repeat"] == 0}
    first_pass = {}
    for name, outcome in (("success", True), ("failure", False)):
        rows = [
            c
            for c in same_task
            if c["repeat"] >= 1
            and c["official"] is not None
            and first.get(c["task"]) is outcome
        ]
        first_pass[name] = {
            "groups": by_group(
                rows,
                "group",
                GROUPS,
                lambda g: {"graded": len(g), "successes": sum(map(succeeded, g))},
            ),
            "notes_vs_no_memory": paired(rows, NOTES, {"no_memory"}, succeeded),
            "notes_vs_empty": paired(
                rows, NOTES, {"empty_preload", "writer_empty"}, succeeded
            ),
            "empty_vs_no_memory": paired(
                rows, {"empty_preload", "writer_empty"}, {"no_memory"}, succeeded
            ),
        }
    delivered = {c["note_sha256"]: c["stance"] for c in second if c["group"] in NOTES}
    return {
        "scope": {
            "graded": by_group(graded, "condition", CONDITIONS, len),
            "customers": len({c["customer"] for c in graded}),
            "tasks": len({c["task"] for c in graded}),
        },
        "outcomes": {
            name: by_group(
                rows,
                "condition",
                CONDITIONS,
                lambda g: {
                    "graded": len(g),
                    "early_handoff_runs": sum(map(handed_off_early, g)),
                    "kinds": kinds(g),
                },
            )
            for name, rows in scoped(graded).items()
        },
        "paired_early_handoff": {
            name: {
                "behavior": paired(rows, MEMO, {"no_memory"}, handed_off_early),
                "failure": paired(
                    rows, MEMO, {"no_memory"}, lambda c: c["kind"] == "early_handoff"
                ),
            }
            for name, rows in scoped(graded).items()
        },
        "paired_success": {
            name: {
                "memo_vs_no_memory": paired(rows, MEMO, {"no_memory"}, succeeded),
                "memo_vs_empty_preload": paired(
                    rows, MEMO, {"empty_preload"}, succeeded
                ),
                "empty_preload_vs_no_memory": paired(
                    rows, {"empty_preload"}, {"no_memory"}, succeeded
                ),
                "notes_vs_no_memory": paired(rows, NOTES, {"no_memory"}, succeeded),
            }
            for name, rows in scoped(graded).items()
        },
        "repeats": repeat_discordance([c for c in same_task if c["repeat"] >= 1]),
        "wording": {
            name: by_group(rows, "group", GROUPS, wording_counts)
            for name, rows in scoped(needed).items()
        },
        "paired_wording": {
            name: {
                f"{left}_vs_{right}": contrasts(rows, left, right)
                for left, right in WORDING_PAIRS
            }
            for name, rows in scoped(needed).items()
        },
        "stance": {
            name: by_group(
                [c for c in rows if c["stance"]], "stance", STANCES, wording_counts
            )
            for name, rows in scoped(needed).items()
        },
        "paired_stance": {
            name: {
                f"{left}_vs_{right}": contrasts(
                    [{**c, "group": c["stance"] or c["group"]} for c in rows],
                    left,
                    right,
                )
                for left, right in (
                    *((stance, "no_memory") for stance in STANCES),
                    ("recommends", "records"),
                )
                if any(c["stance"] == left for c in rows)
            }
            for name, rows in scoped(needed).items()
        },
        "source_tool_defect": {
            name: by_group(
                [c for c in needed if c["source_tool_defect"] is wanted],
                "group",
                NOTES,
                wording_counts,
            )
            for name, wanted in (("affected", True), ("unaffected", False))
        },
        "same_tasks": {
            name: by_group(rows, "group", GROUPS, kinds) for name, rows in split.items()
        },
        "initiators": by_group(
            [c for c in needed if handed_off_early(c)],
            "group",
            GROUPS,
            lambda g: dict(sorted(Counter(c["initiator"] for c in g).items())),
        ),
        "first_pass": first_pass,
        "checks": {
            "ungraded": {
                study: by_group(
                    [c for c in second if c["study"] == study],
                    "condition",
                    CONDITIONS,
                    lambda g: [sum(c["official"] is None for c in g), len(g)],
                )
                for study in studies
            },
            "job_position": {
                study: by_group(
                    [c for c in second if c["study"] == study],
                    "condition",
                    CONDITIONS,
                    lambda g: round(mean(c["job"] for c in g), 3),
                )
                for study in studies
            },
        },
        "notes": {
            "distinct": len(delivered),
            "with_escalation": sum(map(bool, delivered.values())),
            "stances": dict(sorted(Counter(filter(None, delivered.values())).items())),
        },
    }


def audited_cells(workspace):
    """Yield (study, repeat, cell) for the first-pass sources and every second-pass solve."""
    full, expansion, related, revision = (
        read_json(verified(safe_path(workspace, str(EVIDENCE / name)), sha))
        for name, sha in INPUTS.items()
    )
    for cell in (*full["cells"], *expansion["new_cells"]):
        if cell["stage"] != "write":
            yield (
                "same_task",
                0 if cell["stage"] == "source" else int(cell["stage"][-1]),
                cell,
            )
    for study, cells in (
        ("related_transfer", related["cells"]),
        ("transfer_revision", revision["solves"]),
    ):
        for cell in cells:
            yield study, int(cell["stage"][-1]), cell


def collect(workspace):
    """Build one row per run and check it against what the sealed analyses recorded."""
    name, sha = ANNOTATIONS
    annotations = read_json(verified(safe_path(workspace, str(EVIDENCE / name)), sha))
    rows, notes = [], set()
    for study, repeat, cell in audited_cells(workspace):
        items, trajectory = read_run(workspace, cell)
        row = describe(study, repeat, cell, items, trajectory, annotations)
        if "item_count" in cell:
            require(
                len(items or ()) == cell["item_count"], "Delivered note count differs"
            )
        elif row["official"] is not None:
            require(
                bool(items) == (cell["input_spans"] > 0),
                "Delivered note exposure differs",
            )
        if cell.get("transfer_position"):
            require(
                row["position"] == cell["transfer_position"], "Handoff position differs"
            )
        if row["reference_changes"] is not None and "target_changes_db" in cell:
            require(
                row["reference_changes"] == cell["target_changes_db"],
                "Reference mutations differ",
            )
        if items:
            notes.add(row["note_sha256"])
        rows.append(row)
    require(len({r["run"] for r in rows}) == len(rows), "A run was counted twice")
    reference = defaultdict(set)
    for row in rows:
        if row["reference_changes"] is not None:
            reference[row["task"]].add(row["reference_changes"])
    require(
        all(len(v) == 1 for v in reference.values()),
        "A task has two reference definitions",
    )
    defect = {
        r["task"]: r["multi_item_change"]
        for r in rows
        if r["study"] == "same_task" and r["repeat"] == 0
    }
    for row in rows:
        row["changes_required"] = next(iter(reference.get(row["task"], [None])))
        row["source_tool_defect"] = defect[row["source"]] if row["source"] else None
        del row["reference_changes"]
    require(
        all(
            r["initiator"]
            for r in rows
            if r["repeat"]
            and r["kind"]
            and r["changes_required"]
            and handed_off_early(r)
        ),
        "An early handoff has no initiator verdict",
    )
    require(
        set(annotations["initiators"]) == {r["run"] for r in rows if r["initiator"]}
        and set(annotations["notes"]) <= notes,
        "A verdict matches no run or delivered note",
    )
    rows.sort(
        key=lambda r: (r["study"], int(r["task"]), r["stage"], r["arm"], r["run"])
    )
    return rows, annotations


def analyze(workspace):
    rows, annotations = collect(workspace)
    used = Counter(r["note_sha256"] for r in rows if r["repeat"] and r["note_sha256"])
    verdicts = annotations["notes"]
    return {
        "format": "agent-memory-retail-handoff-factors/v2",
        "inputs": {**INPUTS, ANNOTATIONS[0]: ANNOTATIONS[1]},
        "definitions": {
            "candidate_wording": CANDIDATE.pattern,
            "note_verdict": "A delivered note counts as mentioning a handoff only when the "
            "blinded verdict says so; notes without candidate wording were not read.",
            "early_handoff": "A successful transfer_to_human_agents call with no successful "
            "order or address mutation in the whole trajectory.",
            "agent_initiated": "An early handoff whose verdict is that the agent offered it "
            "first or called the tool before anyone mentioned it.",
            "pairing": "Runs pair inside one study, task and repeat. Differences are averaged per "
            "task, then per customer; the interval resamples customers 10,000 times.",
            "selection": "Post hoc classification of delivered note text; not a preregistered contrast.",
            "source_tool_defect": "The first-pass source episode contained a successful "
            "modify_pending_order_items call for two or more items; the pinned retail tool then "
            "returns the last item's price and options for every changed item.",
        },
        **summarize(rows),
        "escalation_notes": [
            {
                "note_sha256": sha,
                "second_pass_runs": used[sha],
                "stance": verdicts[sha]["stance"],
                "evidence": verdicts[sha]["evidence"],
            }
            for sha in sorted(used)
            if sha in verdicts and verdicts[sha]["mentions_handoff"]
        ],
        "rejected_candidates": [
            {"note_sha256": sha, "second_pass_runs": used[sha]}
            for sha in sorted(used)
            if sha in verdicts and not verdicts[sha]["mentions_handoff"]
        ],
        "cells": rows,
        "model_calls": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output path")
    with (
        patch(
            "socket.socket.connect", side_effect=AssertionError("Offline analysis")
        ) as connect,
        patch(
            "socket.socket.connect_ex", side_effect=AssertionError("Offline analysis")
        ) as connect_ex,
    ):
        result = analyze(args.workspace.resolve())
        require(
            not connect.called and not connect_ex.called,
            "Analysis attempted network access",
        )
    write_json(args.output, result)
    graded = result["scope"]["graded"]
    print(
        f"{sum(graded.values())} graded second-pass runs, {result['notes']['distinct']} distinct notes"
    )
