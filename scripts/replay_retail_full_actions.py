"""Test business-action ordering, omitted actions and joint argument changes in the original retail DB."""

import argparse
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, canonical
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.journal import safe_path

if __package__:
    from .analyze_retail_full_factors import HASHES, STUDY, original, verified
    from .analyze_recheck_rounds import MUTATIONS
    from .replay_full_retail_fields import aligned_fields, state_hashes
    from .run_full_retail import modules_for
else:
    from analyze_retail_full_factors import HASHES, STUDY, original, verified
    from analyze_recheck_rounds import MUTATIONS
    from replay_full_retail_fields import aligned_fields, state_hashes
    from run_full_retail import modules_for


def call_key(c):
    return canonical([c.name, c.requestor, c.arguments])


def omitted_index(success, failure, success_errors):
    """Only a unique, successful insertion may distinguish these full attempted mutation sequences."""
    if len(success) != len(failure) + 1:
        return None
    good, bad = [call_key(c) for c in success], [call_key(c) for c in failure]
    options = [
        i
        for i in range(len(good))
        if good[:i] + good[i + 1 :] == bad and not success_errors[i]
    ]
    return options[0] if len(options) == 1 else None


def switched(calls, other, fields):
    edited = list(calls)
    for i, key in fields:
        args = {**edited[i].arguments, key: other[i].arguments[key]}
        edited[i] = edited[i].model_copy(update={"arguments": args})
    return edited


def replay(workspace):
    data, evidence = original(workspace, "cells"), original(workspace, "field-replay")
    cells = {c["run"]: c for c in data["cells"] if c["stage"] == "solve1"}
    refs = {r["task"]: r for r in evidence["reference_databases"]}
    prior = {(p["memory"], p["control"]): p for p in evidence["pairs"]}
    modules_for(workspace)
    from tau2.data_model.language import LanguageTaskInfo
    from tau2.data_model.simulation import SimulationRun
    from tau2.multilingual.runtime import language_environment_kwargs
    from tau2.registry import registry
    from tau2.runner.presets import reload_recorded_call

    make = registry.get_env_constructor("retail")

    @lru_cache(maxsize=None)
    def options(context):
        return (
            language_environment_kwargs(
                reload_recorded_call(
                    LanguageTaskInfo.model_validate_json(context), "retail"
                )
            )
            or {}
        )

    def execute(calls, ctx, task):
        env = make(**options(ctx))
        if list(state_hashes(env)) != refs[task]["initial_db"]:
            raise ContractError(
                "Action intervention did not start from the original clean DB"
            )
        errors = []
        for i, call in enumerate(calls):
            reply = env.get_response(call)
            if reply.error:
                errors.append(dict(index=i, tool=call.name, response=reply.content))
        hashes = list(state_hashes(env))
        return dict(
            db_match=hashes == refs[task]["expected_db"], db=hashes, errors=errors
        )

    @lru_cache(maxsize=None)
    def trace(run):
        c = cells[run]
        sim = SimulationRun.model_validate_json(
            verified(safe_path(workspace, c["report"]), c["report_sha256"])
        )
        if sim.task_id != c["task"]:
            raise ContractError("Mismatched task in native action probe")
        ctx = sim.language_context.model_dump_json()
        env = make(**options(ctx))
        env.set_state(
            initialization_data=None,
            initialization_actions=None,
            message_history=sim.messages,
            strict=True,
        )
        replies = {m.id: m for m in sim.messages if m.role == "tool"}
        calls = [
            k
            for m in sim.messages
            for k in getattr(m, "tool_calls", None) or []
            if k.name in MUTATIONS
            and k.id in replies
            and replies[k.id].execution_status != "rejected"
        ]
        baseline = execute(calls, ctx, sim.task_id)
        flags = [replies[k.id].error for k in calls]
        if (
            baseline["db"] != list(state_hashes(env))
            or baseline["db_match"] != c["grade"]["db_match"]
            or [e["index"] for e in baseline["errors"]]
            != [i for i, error in enumerate(flags) if error]
        ):
            raise ContractError(
                "Mutation replay differs from the recorded tool responses or strictly graded DB"
            )
        return calls, ctx, baseline, flags

    def run_edit(run, calls):
        return execute(calls, trace(run)[1], cells[run]["task"])

    # Fix every other call and argument in episodes that actually completed both changes on the same order.
    ordering = []
    for run, c in cells.items():
        updates = [a for a in c["behavior"]["writes"] if a["success"]]
        addresses = [a for a in updates if a["name"] == "modify_pending_order_address"]
        items = [a for a in updates if a["name"] == "modify_pending_order_items"]
        orders = {a["args"]["order_id"] for a in addresses} & {
            a["args"]["order_id"] for a in items
        }
        if not orders:
            continue
        calls, _, before, flags = trace(run)
        for order in sorted(orders):
            positions = [
                (i, k)
                for i, k in enumerate(calls)
                if not flags[i] and k.arguments.get("order_id") == order
            ]
            for ai, a in positions:
                for bi, b in positions:
                    if (
                        a.name != "modify_pending_order_address"
                        or b.name != "modify_pending_order_items"
                    ):
                        continue
                    swapped = list(calls)
                    swapped[ai], swapped[bi] = swapped[bi], swapped[ai]
                    ordering.append(
                        dict(
                            task=c["task"],
                            arm=c["arm"],
                            run=run,
                            order=order,
                            address_at=ai,
                            items_at=bi,
                            before=before,
                            after=run_edit(run, swapped),
                        )
                    )

    discordant = []
    for p in data["pairs"]:
        if p["state"] not in {"gain", "loss"}:
            continue
        memory, control = p["memory_run"], p["control_run"]
        winner, loser = (memory, control) if p["state"] == "gain" else (control, memory)
        good, _, good_db, flags = trace(winner)
        bad, _, bad_db, _ = trace(loser)
        probe = dict(
            task=p["task"],
            arm=p["arm"],
            control=p["control"],
            state=p["state"],
            winner=winner,
            loser=loser,
            winner_before=good_db,
            loser_before=bad_db,
            argument_swaps=[],
            insertion=None,
        )
        discordant.append(probe)
        if bad_db["db_match"]:
            probe["scope"] = "failed_only_on_natural_language"
            continue
        status, fields = aligned_fields(good, bad)
        probe["scope"] = status
        if 1 <= len(fields) <= 2:
            for width in range(1, len(fields) + 1):
                for subset in combinations(fields, width):
                    g = run_edit(winner, switched(good, bad, subset))
                    b = run_edit(loser, switched(bad, good, subset))
                    details = [
                        dict(
                            index=i,
                            name=good[i].name,
                            field=k,
                            winner_value=good[i].arguments[k],
                            loser_value=bad[i].arguments[k],
                        )
                        for i, k in subset
                    ]
                    if width == 1 and (memory, control) in prior:
                        i, k = subset[0]
                        previous = next(
                            item
                            for item in prior[memory, control]["fields"]
                            if (item["call_index"], item["field"]) == (i, k)
                        )
                        x, y = (g, b) if winner == memory else (b, g)
                        if (
                            x["db"] != previous["memory_swap"]["after_hash"]
                            or y["db"] != previous["control_swap"]["after_hash"]
                        ):
                            raise ContractError(
                                "Single-field replay differs from published native DB"
                            )
                    probe["argument_swaps"].append(
                        dict(fields=details, winner_after=g, loser_after=b)
                    )
        i = omitted_index(good, bad, flags)
        if i is not None:
            missing = good[i]
            probe["insertion"] = dict(
                index=i,
                tool=missing.name,
                arguments=missing.arguments,
                removed_from_winner=run_edit(winner, good[:i] + good[i + 1 :]),
                inserted_into_loser=[
                    dict(
                        position=j,
                        outcome=run_edit(loser, bad[:j] + [missing] + bad[j:]),
                    )
                    for j in range(len(bad) + 1)
                ],
            )
    return dict(
        study=STUDY,
        input_sha256=HASHES,
        changed_outcome="database_match_only",
        scope="All scored discordances against both controls. Swap one or two different argument fields when the attempted operations and keys align and at most two fields differ. For one uniquely omitted successful mutation, try every insertion boundary. Across all second-pass episodes also exchange successful address and item changes on the same order.",
        order_swaps=ordering,
        discordant=discordant,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", type=Path, default=ROOT)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    if args.output.exists():
        p.error("Choose a new analysis output")
    with (
        patch(
            "socket.socket.connect", side_effect=AssertionError("Offline replay")
        ) as conn,
        patch(
            "socket.socket.connect_ex", side_effect=AssertionError("Offline replay")
        ) as conn_ex,
    ):
        result = replay(args.workspace.resolve())
        if conn.called or conn_ex.called:
            raise ContractError("Unexpected network attempt")
    write_json(args.output, result)
