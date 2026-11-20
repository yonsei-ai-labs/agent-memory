"""Explain the observed retail retry gain using the original scores, notes and tool calls."""

import argparse
import json
from collections import Counter, defaultdict
from functools import cache
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.journal import BlobStore, safe_path
from scripts.analyze_full_retail import require
from scripts.analyze_recheck_rounds import MUTATIONS, status
from scripts.analyze_retail_full_factors import (
    business_events,
    case_excerpt,
    difference_kind,
    named_values,
    transfer_position,
    verified,
)
from scripts.analyze_retail_sample_expansion import ROUNDS
from scripts.replay_full_retail_fields import reference_state, state_hashes
from scripts.run_retail_sample_expansion import prepare

PARENT = "20261119-retail-full-cells.json"
DETAIL = "20261120-retail-sample-expansion-cells.json"
SUMMARY = "20261120-retail-sample-expansion-results.json"
INPUTS = {
    PARENT: "34d877c4d778cee4a39490a640ab11dcd9455ec991df44788fc29f13663907e2",
    DETAIL: "ff32b8b009456f4bd1efee362dc1b3ef8fbd2df08079a1eccf81f55bd093f31b",
    SUMMARY: "3f7ae1946d7e445277a2b19f46def8cc604e6b9a70a8cee30e82dd76bc68bfb1",
}
CONTROLS = ("no_memory", "empty_preload")

# Retrospective excerpts for the two largest recoveries and the decisions discussed in the report.
CASE_TURNS = {
    ("90", "source", "no_memory"): (17, 23, 25, 28),
    ("90", "solve1", "no_memory"): (18, 19, 21, 25, 26),
    ("80", "source", "no_memory"): (15, 16, 17, 18),
    ("80", "solve2", "no_memory"): (14, 15, 16, 19, 20),
    ("110", "solve2", "fact_original"): (13, 14, 15, 17, 28, 31),
    ("110", "solve2", "no_memory"): (21, 26, 27),
    ("49", "solve3", "episode_original"): (16, 17, 18),
    ("49", "solve3", "no_memory"): (20, 21, 22),
    ("109", "solve2", "lesson_original"): (12, 14),
    ("111", "solve2", "lesson_original"): (17, 18, 19, 20, 22, 23, 33, 43),
    ("27", "solve3", "procedure_original"): (14, 15, 16, 17, 18, 21),
    ("60", "solve1", "procedure_original"): (8, 9, 12, 13, 14),
    ("36", "solve1", "fact_original"): (30, 31, 32),
    ("78", "solve3", "episode_original"): (14, 17, 18, 19, 20),
    ("68", "solve2", "procedure_original"): (18, 19, 20, 21, 22),
    ("104", "solve3", "lesson_original"): (38, 41, 42, 43, 44, 48),
}


def read_trace(workspace, cell):
    raw = read_json(
        verified(safe_path(workspace, cell["report"]), cell["report_sha256"])
    )
    require(raw["task_id"] == cell["task"], "Native trajectory belongs to another task")
    messages = raw["messages"]
    writes, transfers = business_events(messages)
    require(
        [{k: v for k, v in w.items() if k != "accepted"} for w in writes]
        == cell["behavior"]["writes"],
        "Native mutation attempts differ from the audited cell",
    )
    position = transfer_position(writes, transfers)
    require(
        position == cell.get("transfer_position", position), "Handoff position changed"
    )
    replies = {m["id"]: m for m in messages if m["role"] == "tool"}
    attempts, call_ids = [], []
    for message in messages:
        for call in message.get("tool_calls") or []:
            reply = replies.get(call["id"])
            if (
                call["name"] in MUTATIONS
                and reply
                and reply.get("execution_status") != "rejected"
            ):
                attempts.append(
                    (
                        call["name"],
                        call["requestor"],
                        call["arguments"],
                        not reply["error"],
                    )
                )
                call_ids.append(call["id"])
    require(
        [c[3] for c in attempts] == [w["success"] for w in writes if w["accepted"]],
        "Attempted mutation responses differ",
    )
    first_user = next(m["content"] for m in messages if m["role"] == "user")
    return {
        "updates": writes,
        "transfer_position": position,
        "db_match": cell["grade"]["db_match"],
        "failed_assertions": [
            n["assertion"] for n in cell["grade"]["nl"] if not n["met"]
        ],
        "attempts": attempts,
        "call_ids": call_ids,
        "run": cell["run"],
        "context": canonical(raw["language_context"]),
        "persona": digest(canonical(raw["language_context"]["persona"])),
        "first_user": digest(canonical(first_user)),
    }


def make_replayer(tasks, references):
    from tau2.data_model.language import LanguageTaskInfo
    from tau2.data_model.message import ToolCall
    from tau2.multilingual.runtime import language_environment_kwargs
    from tau2.registry import registry
    from tau2.runner.presets import reload_recorded_call

    make = registry.get_env_constructor("retail")

    @cache
    def configuration(task, context):
        options = (
            language_environment_kwargs(
                reload_recorded_call(
                    LanguageTaskInfo.model_validate_json(context), "retail"
                )
            )
            or {}
        )
        env = make(**options)
        require(
            tasks[task]["initial_state"] is None, "Unsupported retail initial state"
        )
        initial = state_hashes(env)
        target, errors = reference_state(
            env, tasks[task]["evaluation_criteria"]["actions"]
        )
        expected = references[task]
        applicable = (
            [expected[digest(context)]]
            if digest(context) in expected
            else expected.values()
        )
        require(
            all(
                tuple(ref["initial"]) == initial
                and tuple(ref["target"]) == target
                and ref["errors"] == errors
                for ref in applicable
            ),
            "Reference DB changed",
        )
        return options, initial, target

    @cache
    def execute(task, context, encoded_calls):
        options, initial, target = configuration(task, context)
        env = make(**options)
        require(
            state_hashes(env) == initial,
            "Action replay did not start from the clean database",
        )
        failures = []
        for i, (name, actor, args) in enumerate(read_json(encoded_calls)):
            reply = env.get_response(
                ToolCall(name=name, requestor=actor, arguments=args)
            )
            if reply.error:
                failures.append(i)
        final = state_hashes(env)
        return {"match": final == target, "db": final, "failed_calls": failures}

    return execute


def argument_changes(win, lose):
    """Align successful mutations while retaining their indices in each full attempted sequence."""
    left, right = [
        [(i, c) for i, c in enumerate(t["attempts"]) if c[3]] for t in (win, lose)
    ]
    changes = []
    for (i, a), (j, b) in zip(left, right, strict=True):
        require(
            a[:2] == b[:2] and a[2].keys() == b[2].keys(), "Unequal tool signatures"
        )
        for key in sorted(a[2]):
            if a[2][key] != b[2][key]:
                changes.append(
                    {
                        "tool": a[0],
                        "field": key,
                        "winner_index": i,
                        "loser_index": j,
                        "winner_value": a[2][key],
                        "loser_value": b[2][key],
                    }
                )
    return changes


def replay_edits(task, win, lose, changes, execute):
    bases = []
    for trace in (win, lose):
        calls = [c[:3] for c in trace["attempts"]]
        base = execute(task, trace["context"], canonical(calls))
        require(
            base["match"] == trace["db_match"]
            and base["failed_calls"]
            == [i for i, c in enumerate(trace["attempts"]) if not c[3]],
            "Original tool replay differs from the audited DB or tool replies",
        )
        bases.append(calls)

    def swap(side, selected):
        calls = list(bases[side])
        index, value = (
            ("winner_index", "loser_value")
            if not side
            else ("loser_index", "winner_value")
        )
        for change in selected:
            i = change[index]
            name, actor, args = calls[i]
            calls[i] = name, actor, {**args, change["field"]: change[value]}
        trace = (win, lose)[side]
        return execute(task, trace["context"], canonical(calls))

    subsets = [[i] for i in range(len(changes))]
    if len(changes) > 1:
        subsets.append(list(range(len(changes))))
    outcomes = []
    for indices in subsets:
        selected = [changes[i] for i in indices]
        outcomes.append(
            {
                "changes": indices,
                "winner_with_loser_values": swap(0, selected),
                "loser_with_winner_values": swap(1, selected),
            }
        )
    return outcomes


def compare_controls(solver, controls):
    values = [c["official"] for c in controls]
    if None in values:
        return "incomplete"
    return ("neither", "one", "both")[sum(v == solver["official"] for v in values)]


def item_lookup_reader(workspace, cells):
    @cache
    def read_run(run):
        cell = cells[run]
        journal = safe_path(workspace, cell["journal"])
        blobs = BlobStore(journal.parent)
        started, decisions = {}, {}
        for line in verified(journal, cell["journal_sha256"]).splitlines():
            for event in read_json(line)["events"]:
                p = event["payload"]
                if (
                    event["type"] == "model.request_started"
                    and p["actor_role"] == "worker"
                ):
                    started[p["request_id"]] = p
                if (
                    event["type"] != "model.request_finished"
                    or p["request_id"] not in started
                ):
                    continue
                raw = blobs.get(p["response_ref"])
                emitted = [
                    c
                    for c in raw["choices"][0]["message"].get("tool_calls") or []
                    if c["function"]["name"]
                    in {"modify_pending_order_items", "exchange_delivered_order_items"}
                ]
                if not emitted:
                    continue
                ref = started[p["request_id"]]["request_ref"]
                messages = blobs.get(ref)["messages"]
                names = {
                    c["id"]: c["function"]["name"]
                    for m in messages
                    for c in m.get("tool_calls") or []
                }
                products, orders = set(), set()
                for message in messages:
                    if message["role"] != "tool":
                        continue
                    tool = names.get(message["tool_call_id"])
                    if tool not in {"get_product_details", "get_order_details"}:
                        continue
                    try:
                        value = json.loads(message["content"])
                    except ValueError:
                        continue
                    if not isinstance(value, dict):
                        continue
                    if tool == "get_product_details":
                        products.update(
                            k
                            for k, v in value.get("variants", {}).items()
                            if v.get("available") is True
                        )
                    else:
                        orders.update(v["item_id"] for v in value.get("items", []))
                for call in emitted:
                    require(
                        call["id"] not in decisions, "Duplicated native mutation ID"
                    )
                    decisions[call["id"]] = (
                        call["function"],
                        p["request_id"],
                        ref["sha256"],
                        products,
                        orders,
                    )
        return decisions

    def evidence(trace, index, identifiers):
        call_id = trace["call_ids"][index]
        fn, request, sha, products, orders = read_run(trace["run"])[call_id]
        attempt = trace["attempts"][index]
        require(
            fn["name"] == attempt[0] and json.loads(fn["arguments"]) == attempt[2],
            "Recorded worker response differs from the native action",
        )
        return {
            "request": request,
            "wire_sha256": sha,
            "catalog": sorted(identifiers & products),
            "order": sorted(identifiers & orders),
        }

    return evidence


def summarize_swaps(transitions):
    fields, item_arms, item_notes = (
        defaultdict(Counter),
        defaultdict(Counter),
        defaultdict(Counter),
    )
    in_tool_inputs = defaultdict(Counter)
    for pair in transitions:
        if "db_swaps" not in pair:
            continue
        changes = pair["argument_changes"]
        keys = "+".join(sorted({c["field"] for c in changes}))
        direction = pair["state"]
        joint = pair["db_swaps"][-1]
        require(
            joint["changes"] == list(range(len(changes))), "Missing full argument swap"
        )
        fields[keys][direction] += 1
        if (
            joint["loser_with_winner_values"]["match"]
            and not joint["winner_with_loser_values"]["match"]
        ):
            fields[keys][direction + "_reversed_both_databases"] += 1
        if keys != "new_item_ids":
            continue
        item_arms[pair["arm"]][direction] += 1
        lookups = pair["item_lookups"]
        if all(v["winning_ids"] for v in lookups):
            visible = []
            for side in ("winner", "loser"):
                complete = all(
                    set(v["winning_ids"]).issubset(
                        v[side]["catalog"] + v[side]["order"]
                    )
                    for v in lookups
                )
                if complete:
                    visible.append(side)
                    in_tool_inputs[direction][side] += 1
            if len(visible) == 2:
                in_tool_inputs[direction]["both"] += 1
        else:
            in_tool_inputs[direction]["no_distinct_winning_id"] += 1
        if pair["arm"] not in CONTROLS:
            mentioned = [
                side
                for side in ("winner", "loser")
                if any(any(c[side + "_literal_in_note"].values()) for c in changes)
            ]
            item_notes[pair["exposure"] + "/" + direction][
                "+".join(mentioned) or "neither"
            ] += 1
    return {
        "fields": fields,
        "item_only_by_arm": item_arms,
        "item_only_note_mentions": item_notes,
        "winning_ids_in_tool_inputs": in_tool_inputs,
    }


def excerpts(workspace, index):
    result = []
    for (task, stage, arm), turns in CASE_TURNS.items():
        cell = index[task, stage, arm]
        bank = index.get((task, "write", arm))
        raw = read_json(
            verified(safe_path(workspace, cell["report"]), cell["report_sha256"])
        )
        result.append(
            dict(
                task=task,
                stage=stage,
                arm=arm,
                run=cell["run"],
                official=cell["official"],
                report=cell["report"],
                report_sha256=cell["report_sha256"],
                writer=bank["run"] if bank else None,
                **case_excerpt(raw, turns, bank["notes"] if bank else []),
            )
        )
    return result


def analyze(workspace):
    cfg, _, _, rows, tasks = prepare(workspace)
    inputs = {
        n: read_json(verified(workspace / "artifacts/analyses" / n, sha))
        for n, sha in INPUTS.items()
    }
    source_data, expanded, summary = inputs[PARENT], inputs[DETAIL], inputs[SUMMARY]
    require(
        expanded["rows"] == rows and summary["detail_sha256"] == INPUTS[DETAIL],
        "Expansion differs from the audited allocations",
    )
    cells = source_data["cells"] + expanded["new_cells"]
    index = {(c["task"], c["stage"], c["arm"]): c for c in cells}
    require(len(index) == len(cells) == 1518, "Duplicate or missing analytical cells")
    arms = [f + "_original" for f in cfg["forms"]] + list(CONTROLS)
    traces = {
        c["run"]: read_trace(workspace, c) for c in cells if c["official"] is not None
    }
    require(len(traces) == 1201, "Incomplete scored trajectories")
    execute = make_replayer(tasks, expanded["reference_databases"])
    lookup = item_lookup_reader(workspace, {c["run"]: c for c in cells})
    by_arm = {a: Counter() for a in arms}
    by_kind, by_work, by_arm_kind = (
        defaultdict(Counter),
        defaultdict(Counter),
        defaultdict(lambda: defaultdict(Counter)),
    )
    by_task, by_exposure, by_controls = {}, defaultdict(Counter), defaultdict(Counter)
    by_cohort = defaultdict(Counter)
    contexts = defaultdict(lambda: defaultdict(Counter))
    transitions, bank_results = [], []
    for row in rows:
        task, task_counts = row["task"], Counter()
        retry_personas = set()
        source = index[task, "source", "no_memory"]
        first = traces[source["run"]]
        workflow = "+".join(sorted(set(row["operations"]))) or "no_reference_change"
        for arm in arms:
            bank = index.get((task, "write", arm))
            exposure = (
                ("written" if bank["notes"] else "voluntarily_empty")
                if bank
                else "control"
            )
            bank_counts = Counter()
            for stage in ROUNDS:
                solver = index[task, stage, arm]
                state = status(solver["official"], source["official"])
                by_arm[arm][state] += 1
                by_cohort[row["cohort"]][state] += 1
                by_exposure[exposure][state] += 1
                by_work[workflow][state] += 1
                bank_counts[state] += 1
                task_counts[state] += 1
                if state == "missing":
                    continue
                second = traces[solver["run"]]
                require(
                    solver["writer_run"] == (bank["run"] if bank else None)
                    and (solver["input_spans"] > 0) == (exposure == "written"),
                    "Writer or delivered memory does not match the assignment",
                )
                retry_personas.add(second["persona"])
                for field in ("first_user", "persona"):
                    group = "same" if first[field] == second[field] else "different"
                    contexts[field][group][state] += 1
                if state not in {"gain", "loss"}:
                    continue
                winner, loser = (second, first) if state == "gain" else (first, second)
                kind = difference_kind(winner, loser)
                by_kind[kind][state] += 1
                by_arm_kind[arm][kind][state] += 1
                point = {
                    "task": task,
                    "cohort": row["cohort"],
                    "stage": stage,
                    "arm": arm,
                    "state": state,
                    "kind": kind,
                    "source": source["run"],
                    "second": solver["run"],
                    "exposure": exposure,
                    "losing_failed_assertions": loser["failed_assertions"],
                }
                if bank:
                    ctrls = [index[task, stage, a] for a in CONTROLS]
                    matches = compare_controls(solver, ctrls)
                    point.update(
                        writer=bank["run"],
                        control_runs=[c["run"] for c in ctrls],
                        controls_with_same_result=matches,
                    )
                    by_controls[state][matches] += 1
                if kind == "same_tools_and_order_different_arguments":
                    changes = argument_changes(winner, loser)
                    require(bool(changes), "No differing field in an argument contrast")
                    point["argument_changes"] = changes
                    point["db_swaps"] = replay_edits(
                        task, winner, loser, changes, execute
                    )
                    if all(ch["field"] == "new_item_ids" for ch in changes):
                        lookups = []
                        for ch in changes:
                            ids = set(ch["winner_value"]) - set(ch["loser_value"])
                            lookups.append(
                                {
                                    "winning_ids": sorted(ids),
                                    "winner": lookup(winner, ch["winner_index"], ids),
                                    "loser": lookup(loser, ch["loser_index"], ids),
                                }
                            )
                        point["item_lookups"] = lookups
                    if bank:
                        for ch in changes:
                            for side, other in [
                                ("winner", "loser"),
                                ("loser", "winner"),
                            ]:
                                ch[side + "_literal_in_note"] = named_values(
                                    ch[side + "_value"],
                                    ch[other + "_value"],
                                    bank["notes"],
                                )
                transitions.append(point)
            if bank:
                bank_results.append(
                    {
                        "task": task,
                        "arm": arm,
                        "writer": bank["run"],
                        "snapshot": bank["snapshot"],
                        "titles": [n["title"] for n in bank["notes"]],
                        "status": bank["status"],
                        "outcomes": bank_counts,
                    }
                )
        require(
            len(retry_personas) == 1, "The repeat arms used different customer settings"
        )
        by_task[task] = {
            "cohort": row["cohort"],
            "workflow": workflow,
            "source": source["run"],
            "first_success": source["official"],
            "same_persona": first["persona"] in retry_personas,
            "counts": task_counts,
        }
    for arm in arms:
        expected = summary["groups"]["combined"]["arms"][arm]["first_to_second"]
        require(dict(by_arm[arm]) == expected, "Source transition counts changed")
    require(
        len(transitions) == 277 and len(bank_results) == 264,
        "Incomplete transition or writer coverage",
    )
    return {
        "input_sha256": INPUTS,
        "scored_trajectories": len(traces),
        "by_context": contexts,
        "by_arm": by_arm,
        "by_decision": by_kind,
        "by_arm_decision": by_arm_kind,
        "by_workflow": by_work,
        "by_task": by_task,
        "by_cohort": by_cohort,
        "by_exposure": by_exposure,
        "by_contemporaneous_controls": by_controls,
        "swaps": summarize_swaps(transitions),
        "writers": bank_results,
        "transitions": transitions,
        "post_hoc_examples": excerpts(workspace, index),
        "replay_scope": "Successful operations match in name and order and differ in arguments. "
        "Swap each differing field and all together, retaining every other original "
        "accepted call including failures. Measure the final DB only.",
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
    print(
        f"{len(result['transitions'])} score changes from {result['scored_trajectories']} graded trajectories"
    )
