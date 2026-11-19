"""Swap one recorded business argument at a time across discordant full-retail memory/control pairs."""

import argparse
from collections import Counter
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.journal import safe_path

if __package__:
    from .analyze_recheck_rounds import MUTATIONS
    from .run_full_retail import prepare, read
else:
    from analyze_recheck_rounds import MUTATIONS
    from run_full_retail import prepare, read


def state_hashes(env):
    return env.get_db_hash(), env.get_user_db_hash()


def aligned_fields(left, right):
    if [(c.name, c.requestor) for c in left] != [(c.name, c.requestor) for c in right]:
        return "different_operations", []
    if any(
        a.arguments.keys() != b.arguments.keys()
        for a, b in zip(left, right, strict=True)
    ):
        return "different_argument_keys", []
    diffs = [
        (i, k)
        for i, (a, b) in enumerate(zip(left, right, strict=True))
        for k in sorted(a.arguments)
        if canonical(a.arguments[k]) != canonical(b.arguments[k])
    ]
    return ("aligned" if diffs else "same_operations_and_arguments"), diffs


def replay(workspace, detail):
    cfg, _, _, _, tasks = prepare(workspace)
    data = read(detail)
    from tau2.data_model.simulation import SimulationRun
    from tau2.multilingual.runtime import language_environment_kwargs
    from tau2.registry import registry
    from tau2.runner.presets import reload_recorded_call

    cells = {x["run"]: x for x in data["cells"]}
    make = registry.get_env_constructor("retail")
    if data["study"] != cfg["name"]:
        raise ContractError("Wrong study for field replay")

    @lru_cache(maxsize=None)
    def trace(run):
        cell = cells[run]
        file = safe_path(workspace, cell["report"])
        raw = file.read_bytes()
        if digest(raw) != cell["report_sha256"]:
            raise ContractError("Trajectory hash differs from audited study")
        sim = SimulationRun.model_validate_json(raw)
        task = tasks[cell["task"]]
        if task["initial_state"] is not None:
            raise ContractError("Unexpected retail initial state")
        opts = (
            language_environment_kwargs(
                reload_recorded_call(sim.language_context, "retail")
            )
            or {}
        )
        expected, strict, observed = (make(**opts) for _ in range(3))
        strict.set_state(
            initialization_data=None,
            initialization_actions=None,
            message_history=sim.messages,
            strict=True,
        )
        for action in task["evaluation_criteria"]["actions"] or []:
            expected.make_tool_call(
                action["name"],
                requestor=action.get("requestor", "assistant"),
                **action["arguments"],
            )
        replies = {m.id: m for m in sim.messages if m.role == "tool"}
        calls = [
            c
            for m in sim.messages
            for c in getattr(m, "tool_calls", None) or []
            if c.name in MUTATIONS
            and c.id in replies
            and replies[c.id].execution_status != "rejected"
        ]
        errors = [observed.get_response(c).error for c in calls]
        before = state_hashes(observed)
        target = state_hashes(expected)
        if (
            errors != [replies[c.id].error for c in calls]
            or before != state_hashes(strict)
            or (before == target) != cell["grade"]["db_match"]
        ):
            raise ContractError(
                "Original mutation-only replay differs from the strictly graded trajectory"
            )
        return calls, opts, target, before, errors

    def change(run, i, key, alternative):
        calls, opts, target, before, errors = trace(run)
        env = make(**opts)
        edited = list(calls)
        edited[i] = calls[i].model_copy(
            update={"arguments": dict(calls[i].arguments, **{key: alternative})}
        )
        edited_errors = [env.get_response(c).error for c in edited]
        after = state_hashes(env)
        return dict(
            run=run,
            old=calls[i].arguments[key],
            new=alternative,
            before_match=before == target,
            after_match=after == target,
            before_hash=before,
            after_hash=after,
            expected_hash=target,
            tool_errors_before=errors,
            tool_errors_after=edited_errors,
        )

    outcomes = []
    for pair in data["pairs"]:
        if pair["control"] != "no_memory" or pair["state"] not in {"gain", "loss"}:
            continue
        m, b = pair["memory_run"], pair["control_run"]
        a, other = trace(m)[0], trace(b)[0]
        kind, diffs = aligned_fields(a, other)
        evidence = dict(
            task=pair["task"],
            form=pair["arm"],
            outcome=pair["state"],
            memory=m,
            control=b,
            memory_db=cells[m]["grade"]["db_match"],
            control_db=cells[b]["grade"]["db_match"],
            memory_operations=[c.name for c in a],
            control_operations=[c.name for c in other],
            status=kind,
            fields=[],
        )
        for i, key in diffs:
            evidence["fields"].append(
                dict(
                    call_index=i,
                    operation=a[i].name,
                    field=key,
                    memory_swap=change(m, i, key, other[i].arguments[key]),
                    control_swap=change(b, i, key, a[i].arguments[key]),
                )
            )
        outcomes.append(evidence)
    return dict(
        study=cfg["name"],
        scope="Exploratory all discordant memory versus shared pure no-memory pairs. Align accepted business mutations by sequence and requestor, then exchange every differing top-level argument independently in both directions. Customers, memory choices and NL scores are not regenerated.",
        input_sha256=digest(detail.read_bytes()),
        status_counts=dict(Counter(r["status"] for r in outcomes)),
        pairs=outcomes,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("detail", type=Path)
    p.add_argument("--workspace", type=Path, default=ROOT)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        p.error("Use a new output")
    with (
        patch(
            "socket.socket.connect", side_effect=AssertionError("Offline DB replay")
        ) as conn,
        patch(
            "socket.socket.connect_ex", side_effect=AssertionError("Offline DB replay")
        ) as conn_ex,
    ):
        value = replay(a.workspace.resolve(), a.detail)
        assert not conn.call_count and not conn_ex.call_count
    write_json(a.output, value)
    print(value["status_counts"])
