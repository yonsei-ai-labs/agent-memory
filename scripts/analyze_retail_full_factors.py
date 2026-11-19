"""Trace exposure, customer openings and business decisions in the sealed full-retail study."""

import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.journal import BlobStore, safe_path

if __package__:
    from .analyze_recheck_rounds import MUTATIONS, status
else:
    from analyze_recheck_rounds import MUTATIONS, status


STUDY = "tau2-retail-full-forms-01"
EVIDENCE = Path("artifacts/analyses")
PREFIX = "20261119-retail-full-"
HASHES = {
    "cells": "34d877c4d778cee4a39490a640ab11dcd9455ec991df44788fc29f13663907e2",
    "field-replay": "557e1a50349775ffce3d4f3ee8568dc09361a6f59ab377a4980750944a477aaa",
}

# Post hoc evidence for the examples in the report. Indices refer to the complete native trajectory.
CASE_TURNS = {
    ("36", "fact_original"): (13, 30, 31, 32),
    ("36", "episode_original"): (9, 13, 24),
    ("36", "no_memory"): (13, 32, 35, 37, 38),
    ("55", "fact_original"): (15, 19, 22, 23, 24, 26, 28),
    ("55", "no_memory"): (15, 19, 24, 25, 30, 32),
    ("97", "fact_original"): (16, 17, 18, 20),
    ("97", "no_memory"): (16, 17, 18, 20, 21, 22),
    ("100", "fact_original"): (20, 21, 22, 23, 24, 26),
    ("100", "no_memory"): (26, 27, 28, 29, 30, 31, 32, 34, 37, 40),
    ("38", "fact_original"): (31, 32, 33, 34),
    ("38", "episode_original"): (32, 33, 35, 36),
    ("38", "no_memory"): (32, 33, 34),
    ("60", "procedure_original"): (8, 9, 12, 13, 14),
    ("60", "episode_original"): (10, 11, 12, 13, 14),
    ("60", "no_memory"): (8, 9, 10),
    ("68", "episode_original"): (16, 20, 21, 22, 23),
    ("68", "no_memory"): (18, 19, 20),
}


# One retrospective reading of all customer turns before each changed-target, no-change handoff.
HANDOFF_REQUESTS = {
    ("45", "empty_preload"): ("requested_contact", 21, "Can I talk to someone"),
    ("45", "fact_original"): ("requested_contact", 9, "can you escalate"),
    ("45", "lesson_original"): ("requested_contact", 25, "transfer me to someone"),
    ("108", "lesson_original"): (
        "requested_contact",
        21,
        "put me through to a human agent",
    ),
    ("111", "fact_original"): (
        "requested_contact",
        21,
        "please transfer me to a human agent",
    ),
    ("111", "procedure_original"): (
        "requested_contact",
        31,
        "Can I talk to a supervisor?",
    ),
    ("108", "episode_original"): (
        "asked_who_can_verify",
        17,
        "Is there someone who could look into the adjustments",
    ),
    ("108", "no_memory"): ("asked_who_can_verify", 17, "someone who could confirm"),
}
HANDOFF_NO_REQUEST = {
    ("27", "episode_original"),
    ("27", "lesson_original"),
    ("27", "procedure_original"),
    ("32", "episode_original"),
    ("32", "fact_original"),
    ("32", "no_memory"),
    ("32", "procedure_original"),
    ("36", "episode_original"),
    ("36", "fact_original"),
    ("45", "procedure_original"),
    ("51", "procedure_original"),
}


def reviewed_handoff(c, before, messages):
    key = c["task"], c["arm"]
    user = {
        i: m["content"] for i, m in enumerate(messages[:before]) if m["role"] == "user"
    }
    if key in HANDOFF_REQUESTS:
        label, turn, quote = HANDOFF_REQUESTS[key]
        if quote not in user[turn]:
            raise ContractError(
                "Handoff annotation does not match the quoted customer turn"
            )
    elif key in HANDOFF_NO_REQUEST:
        label, turn, quote = "no_customer_contact_request", None, None
    else:
        raise ContractError("Handoff outside the reviewed cohort")
    return dict(
        task=c["task"],
        arm=c["arm"],
        run=c["run"],
        at=before,
        label=label,
        turn=turn,
        quote=quote,
        customer_before=[dict(turn=i, text=text) for i, text in user.items()],
    )


def verified(path, sha):
    raw = path.read_bytes()
    if digest(raw) != sha:
        raise ContractError(f"Archived evidence differs: {path}")
    return raw


def original(workspace, name):
    return read_json(
        verified(workspace / EVIDENCE / (PREFIX + name + ".json"), HASHES[name])
    )


def signature(value):
    return digest(canonical(value))


def public_message(message):
    """Compare spoken text and tool actions without generated call IDs or timestamps."""
    return dict(
        role=message["role"],
        content=message.get("content"),
        calls=[
            (call["name"], call["arguments"], call["requestor"])
            for call in message.get("tool_calls") or []
        ],
        error=message.get("error"),
        execution=message.get("execution_status"),
    )


def opening(workspace, cell, messages, notes):
    journal = safe_path(workspace, cell["journal"])
    raw = verified(journal, cell["journal_sha256"])
    blobs = BlobStore(journal.parent)
    customer_request = customer_id = customer_answer = None
    for line in raw.splitlines():
        for event in read_json(line)["events"]:
            if event["run_id"] != cell["run"]:
                continue
            p = event["payload"]
            if (
                event["type"] == "model.request_finished"
                and p["request_id"] == customer_id
            ):
                customer_answer = blobs.get(p["response_ref"])["choices"][0]["message"][
                    "content"
                ]
            if event["type"] != "model.request_started":
                continue
            if customer_request is None:
                if p["actor_role"] != "user_simulator":
                    raise ContractError("Opening must start with the simulator")
                customer_request, customer_id = (
                    p["request_ref"]["sha256"],
                    p["request_id"],
                )
                sim_wire = blobs.get(p["request_ref"])
                if sim_wire["messages"][-1] != dict(
                    role="user", content=messages[0]["content"]
                ):
                    raise ContractError(
                        "Simulator did not receive the recorded greeting"
                    )
                continue
            if p["actor_role"] != "worker" or customer_answer != messages[1]["content"]:
                raise ContractError("Simulator opening was not passed to the worker")
            wire = blobs.get(p["request_ref"])
            wire_msgs = wire["messages"]
            suffix = [dict(role=m["role"], content=m["content"]) for m in messages[:2]]
            transmitted = [
                {k: m[k] for k in ("role", "content")} for m in wire_msgs[-2:]
            ]
            if (
                transmitted != suffix
                or wire_msgs[-2].get("tool_calls")
                or [m["role"] for m in suffix] != ["assistant", "user"]
            ):
                raise ContractError(
                    "First worker request differs from its recorded public history"
                )
            if cell["arm"] == "no_memory":
                if len(wire_msgs) != 3:
                    raise ContractError("Unexpected input to no-memory worker")
            else:
                if len(wire_msgs) != 4 or wire_msgs[1]["role"] != "user":
                    raise ContractError("Expected one preload message")
                items = read_json(wire_msgs[1]["content"].encode())["items"]
                visible = [{k: item[k] for k in ("title", "body")} for item in items]
                if visible != notes:
                    raise ContractError(
                        "Actually transmitted title or body differs from writer"
                    )
            if bool(notes) != (cell["input_spans"] > 0):
                raise ContractError(
                    "Verified input spans disagree with visible note content"
                )
            return dict(
                simulator_request=customer_request,
                customer_text=signature(customer_answer),
                worker_request=p["request_ref"]["sha256"],
                worker_input_without_opening=signature(
                    dict(wire, messages=wire_msgs[:-1])
                ),
                worker_reply=signature(public_message(messages[2])),
            )
    raise ContractError("Incomplete opening")


def business_events(messages):
    replies = {m["id"]: (i, m) for i, m in enumerate(messages) if m["role"] == "tool"}
    updates, transfers = [], []
    for i, message in enumerate(messages):
        if message["role"] != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            name = call["name"]
            if name not in MUTATIONS and name != "transfer_to_human_agents":
                continue
            pos, reply = replies.get(call["id"], (None, {}))
            accepted = pos is not None and reply.get("execution_status") != "rejected"
            event = dict(
                turn=i,
                returned=pos,
                name=name,
                args=call["arguments"],
                accepted=accepted,
                success=accepted and not reply["error"],
            )
            if name in MUTATIONS:
                updates.append(event)
            elif event["success"]:
                last = next(
                    (j for j in range(i - 1, -1, -1) if messages[j]["role"] == "user"),
                    None,
                )
                transfers.append(
                    dict(
                        turn=i,
                        reason=call["arguments"]["summary"],
                        last_user_turn=last,
                        last_user_text=messages[last]["content"]
                        if last is not None
                        else None,
                    )
                )
    return updates, transfers


def transfer_position(updates, transfers):
    if not transfers:
        return "no_transfer"
    first = next((e["turn"] for e in updates if e["success"]), None)
    if first is None:
        return "transfer_without_successful_change"
    return (
        "transfer_before_first_change"
        if transfers[0]["turn"] < first
        else "transfer_after_change"
    )


def difference_kind(winner, loser):
    if loser["db_match"]:
        if not loser["failed_assertions"]:
            raise ContractError(
                "Matching DB does not explain a loss without a failed assertion"
            )
        return "natural_language_only"
    if loser["transfer_position"] == "transfer_without_successful_change":
        return "transfer_without_successful_change"
    wins, losses = ([u for u in e["updates"] if u["success"]] for e in (winner, loser))
    win_names, loss_names = ([u["name"] for u in seq] for seq in (wins, losses))
    if Counter(win_names) != Counter(loss_names):
        return "different_successful_tool_types_or_counts"
    if win_names != loss_names:
        return "same_tool_counts_different_order"
    if [u["args"] for u in wins] != [u["args"] for u in losses]:
        return "same_tools_and_order_different_arguments"
    return "same_successful_writes"


def first_difference(left, right):
    for i, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return dict(turn=i, left=a["role"], right=b["role"])
    if len(left) == len(right):
        return None
    i = min(len(left), len(right))
    return dict(
        turn=i,
        left=left[i]["role"] if i < len(left) else "end",
        right=right[i]["role"] if i < len(right) else "end",
    )


def named_values(chosen, other, notes):
    """Exact distinguishing IDs or phrases. Shared elements in two item lists carry no evidence."""
    chosen = chosen if isinstance(chosen, list) else [chosen]
    other = other if isinstance(other, list) else [other]
    text = "\n".join(n["title"] + "\n" + n["body"] for n in notes)
    return {
        str(v): bool(
            re.search(r"(?<!\w)" + re.escape(str(v)) + r"(?!\w)", text, re.IGNORECASE)
        )
        for v in chosen
        if v not in other
    }


def past_choices(fields, episodes, writers):
    first = {e["task"]: e for e in episodes.values() if e["stage"] == "source"}
    results = []
    for p in fields["pairs"]:
        if not p["fields"]:
            continue
        task, arm = p["task"], p["form"]
        source = first[task]
        notes = writers[task, arm]["notes"]
        updates = [u for u in episodes[p["memory"]]["updates"] if u["accepted"]]
        for f in p["fields"]:
            op = updates[f["call_index"]]
            key = f["field"]
            mem, control = f["memory_swap"]["old"], f["control_swap"]["old"]
            if op["name"] != f["operation"] or op["args"][key] != mem:
                raise ContractError(
                    "Changed field does not match the recorded second-pass action"
                )
            # Identify the business target before comparing the chosen value across sessions.
            original_actions = [
                u
                for u in source["updates"]
                if u["success"]
                and u["name"] == op["name"]
                and all(
                    u["args"].get(k) == op["args"].get(k)
                    for k in ("order_id", "item_ids")
                )
            ]
            results.append(
                dict(
                    task=task,
                    arm=arm,
                    outcome=p["outcome"],
                    memory=p["memory"],
                    control=p["control"],
                    source=source["run"],
                    writer=writers[task, arm]["run"],
                    exposed=bool(notes),
                    operation=op["name"],
                    order=op["args"].get("order_id"),
                    field=key,
                    source_values=[u["args"][key] for u in original_actions],
                    memory_value=mem,
                    control_value=control,
                    memory_values_named=named_values(mem, control, notes),
                    control_values_named=named_values(control, mem, notes),
                )
            )
    return results


def case_excerpt(report, indices, notes):
    messages = report["messages"]
    excerpts = []
    for i in indices:
        m = messages[i]
        entry = dict(turn=i, role=m["role"])
        if m["role"] == "tool":
            try:
                response = read_json(m["content"].encode())
            except ValueError:
                response = None
            if isinstance(response, dict) and "order_id" in response:
                entry["order"] = {
                    k: response.get(k)
                    for k in (
                        "order_id",
                        "status",
                        "return_items",
                        "return_payment_method_id",
                    )
                }
            else:
                entry["error"] = m["error"]
        else:
            entry["text"] = m.get("content")
            entry["calls"] = [
                dict(tool=c["name"], args=c["arguments"])
                for c in m.get("tool_calls") or []
            ]
        excerpts.append(entry)
    return dict(
        notes=notes,
        turns=excerpts,
        native_assertions=report["reward_info"]["nl_assertions"],
    )


def opening_totals(pairs):
    result = {}
    for control in ("no_memory", "empty_preload"):
        selected = [p for p in pairs if p["control"] == control]
        for name, group in [
            ("all", selected),
            ("blank_writer", [p for p in selected if not p["exposed"]]),
            ("nonempty_writer", [p for p in selected if p["exposed"]]),
            ("discordant", [p for p in selected if p["state"] in {"gain", "loss"}]),
        ]:
            result[control + "/" + name] = dict(
                pairs=len(group),
                same_customer_request=sum(
                    p["equal_opening"]["simulator_request"] for p in group
                ),
                different_customer_opening=sum(
                    not p["equal_opening"]["customer_text"] for p in group
                ),
                identical_worker_request=sum(
                    p["equal_opening"]["worker_request"] for p in group
                ),
                different_first_worker_reply_given_identical_request=sum(
                    p["equal_opening"]["worker_request"]
                    and not p["equal_opening"]["worker_reply"]
                    for p in group
                ),
                states=dict(Counter(p["state"] for p in group)),
            )
    return result


def analyze(workspace):
    data, fields = original(workspace, "cells"), original(workspace, "field-replay")
    targets = {r["task"]: r["changed"] for r in fields["reference_databases"]}
    study_rows = {r["task"]: r for r in data["rows"]}
    writers = {(c["task"], c["arm"]): c for c in data["cells"] if c["stage"] == "write"}
    episodes, public, examples = {}, {}, []
    handoffs = []
    workflow = defaultdict(Counter)
    groups = defaultdict(Counter)
    for c in data["cells"]:
        if c["stage"] == "write":
            continue
        report = read_json(
            verified(safe_path(workspace, c["report"]), c["report_sha256"])
        )
        messages = report["messages"]
        updates, transfers = business_events(messages)
        if [
            {k: u[k] for k in ("turn", "returned", "name", "args", "success")}
            for u in updates
        ] != c["behavior"]["writes"]:
            raise ContractError("Recorded mutation attempts differ from original audit")
        w = writers.get((c["task"], c["arm"]))
        notes = w["notes"] if w else []
        snap = opening(workspace, c, messages, notes)
        key = c["task"], c["arm"]
        if c["stage"] == "solve1" and key in CASE_TURNS:
            examples.append(
                dict(
                    task=c["task"],
                    arm=c["arm"],
                    run=c["run"],
                    report_sha256=c["report_sha256"],
                    **case_excerpt(report, CASE_TURNS[key], notes),
                )
            )
        where = transfer_position(updates, transfers)
        if (
            c["stage"] == "solve1"
            and targets[c["task"]]
            and where == "transfer_without_successful_change"
        ):
            handoffs.append(reviewed_handoff(c, transfers[0]["turn"], messages))
        row = dict(
            task=c["task"],
            arm=c["arm"],
            stage=c["stage"],
            run=c["run"],
            writer=c.get("writer_run"),
            exposed_notes=len(notes),
            opening=snap,
            official=c["official"],
            target_changes_db=targets[c["task"]],
            db_match=c["grade"]["db_match"],
            failed_assertions=[
                n["assertion"] for n in c["grade"]["nl"] if not n["met"]
            ],
            updates=updates,
            transfer_position=where,
            transfers=transfers,
            report_sha256=c["report_sha256"],
        )
        episodes[c["run"]] = row
        public[c["run"]] = [public_message(m) for m in messages]
        if c["stage"] == "solve1":
            subset = (
                "changed_target" if row["target_changes_db"] else "unchanged_target"
            )
            workflow[subset + "/" + where][
                "success" if c["official"] else "failure"
            ] += 1
    sources = {e["task"]: e for e in episodes.values() if e["stage"] == "source"}
    from_first = []
    for e in episodes.values():
        if e["stage"] != "solve1":
            continue
        s = sources[e["task"]]
        state = status(e["official"], s["official"])
        kind = None
        if state in {"gain", "loss"}:
            winner, loser = (e, s) if e["official"] else (s, e)
            kind = difference_kind(winner, loser)
        from_first.append(
            dict(
                task=e["task"],
                arm=e["arm"],
                source=s["run"],
                second=e["run"],
                state=state,
                difference_kind=kind,
            )
        )
    pairs = []
    for p in data["pairs"]:
        m, ctrl = (episodes[p[key]] for key in ("memory_run", "control_run"))
        a, b = m["opening"], ctrl["opening"]
        same = {key: a[key] == b[key] for key in a}
        diff = first_difference(public[m["run"]], public[ctrl["run"]])
        if not same["simulator_request"] or (
            p["control"] == "empty_preload"
            and not m["exposed_notes"]
            and not same["worker_input_without_opening"]
        ):
            raise ContractError(
                "Paired customer prompt or identical empty-memory treatment changed"
            )
        kind = None
        if p["state"] in {"gain", "loss"}:
            win, lose = (m, ctrl) if m["official"] else (ctrl, m)
            kind = difference_kind(win, lose)
            groups[p["control"] + "/" + kind][p["state"]] += 1
        pairs.append(
            dict(
                task=p["task"],
                arm=m["arm"],
                prior_customer=study_rows[p["task"]]["prior_customer"],
                control=ctrl["arm"],
                state=p["state"],
                source_success=p["source_success"],
                source=p["source"],
                memory=m["run"],
                baseline=ctrl["run"],
                exposed=bool(m["exposed_notes"]),
                equal_opening=same,
                first_public_difference=diff,
                difference_kind=kind,
            )
        )
    if {(h["task"], h["arm"]) for h in handoffs} != set(
        HANDOFF_REQUESTS
    ) | HANDOFF_NO_REQUEST:
        raise ContractError("Incomplete reviewed handoff cohort")
    if (
        len(episodes) != 280
        or len(pairs) != 320
        or len(writers) != 160
        or len(examples) != len(CASE_TURNS)
    ):
        raise ContractError("Full retail cohort incomplete")
    return dict(
        study=STUDY,
        input_sha256=HASHES,
        historical_field_choices=past_choices(fields, episodes, writers),
        opening_totals=opening_totals(pairs),
        first_pass_comparisons=from_first,
        post_hoc_examples=examples,
        post_hoc_handoff_review=handoffs,
        episodes=list(episodes.values()),
        pairs=pairs,
        by_transfer={k: dict(v) for k, v in workflow.items()},
        by_decision={k: dict(v) for k, v in groups.items()},
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", type=Path, default=ROOT)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    if args.output.exists():
        p.error("Choose a new analysis output")
    write_json(args.output, analyze(args.workspace.resolve()))
