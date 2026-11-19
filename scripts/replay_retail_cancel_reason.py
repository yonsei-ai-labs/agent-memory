"""Exploratory offline sensitivity of the native retail DB to the selected cancellation reason."""

import argparse
from collections import Counter
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.adapters.tau2 import native_modules, verify_checkout
from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.journal import safe_path

if __package__:
    from .analyze_recheck_rounds import MUTATIONS
    from .review_fact_recheck_baggage import state_hashes
    from .run_recheck_rounds import prepare, read
else:
    from analyze_recheck_rounds import MUTATIONS
    from review_fact_recheck_baggage import state_hashes
    from run_recheck_rounds import prepare, read

OTHER_REASON = {"no longer needed": "ordered by mistake", "ordered by mistake": "no longer needed"}


def replay(workspace, paths):
    cfg, _, _, _, _, tasks = prepare(workspace)
    native_modules(verify_checkout(workspace / "data/tau2-bench"))
    from loguru import logger
    from tau2.data_model.language import LanguageTaskInfo
    from tau2.data_model.simulation import SimulationRun
    from tau2.multilingual.runtime import language_environment_kwargs
    from tau2.registry import registry
    from tau2.runner.presets import reload_recorded_call

    logger.disable("tau2")
    constructor = registry.get_env_constructor("retail")

    @lru_cache(maxsize=None)
    def expected(task, language):
        options = (
            language_environment_kwargs(
                reload_recorded_call(LanguageTaskInfo.model_validate_json(language), "retail")
            )
            or {}
        )
        env = constructor(**options)
        if tasks[task]["initial_state"] is not None:
            raise ContractError("Undeclared initialization in cancellation task")
        for a in tasks[task]["evaluation_criteria"]["actions"]:
            env.make_tool_call(a["name"], requestor=a.get("requestor", "assistant"), **a["arguments"])
        return state_hashes(env), options

    rows = []
    for path in paths:
        result = read(path)
        if result["study"] != cfg["name"]:
            raise ContractError("Expected a registered retail round")
        for e in result["cells"]:
            if e["family"] != "cancellation" or not e["stage"].startswith("solve"):
                continue
            row = dict(
                round=e["round"], task=e["task"], stage=e["stage"], arm=e["arm"], run=e["run"], status="ungraded"
            )
            rows.append(row)
            if e["official"] is None:
                continue
            data = safe_path(workspace, e["report"]).read_bytes()
            if digest(data) != e["grade"]["report_sha256"]:
                raise ContractError("Different trajectory than the strict official replay")
            sim = SimulationRun.model_validate(read_json(data))
            matches = [c for m in sim.messages for c in getattr(m, "tool_calls", None) or [] if c.name in MUTATIONS]
            replies = {m.id: m for m in sim.messages if m.role == "tool"}
            attempts = [c for c in matches if c.id in replies and replies[c.id].execution_status != "rejected"]
            cancels = [c for c in attempts if c.name == "cancel_pending_order" and not replies[c.id].error]
            row.update(
                status="no_single_completed_cancellation", before=e["grade"]["db_match"], report_sha256=digest(data)
            )
            if len(cancels) != 1:
                continue
            call = cancels[0]
            previous = call.arguments["reason"]
            alternative = OTHER_REASON[previous]
            # LanguageContext is a mutable Pydantic model. Cache by its immutable serialized value.
            lang = sim.language_context
            expected_hash, opts = expected(e["task"], lang.model_dump_json())
            observed, edited, strict = (constructor(**opts) for _ in range(3))
            strict.set_state(
                initialization_data=None, initialization_actions=None, message_history=sim.messages, strict=True
            )
            for c in attempts:
                modified = (
                    c.model_copy(update={"arguments": dict(c.arguments, reason=alternative)})
                    if c.id == call.id
                    else c
                )
                a, b = observed.get_response(c), edited.get_response(modified)
                if a.error != replies[c.id].error or b.error != a.error:
                    raise ContractError("A replay changed a tool's success status")
            before, after = state_hashes(observed), state_hashes(edited)
            if before != state_hashes(strict) or (before == expected_hash) != row["before"]:
                raise ContractError("Rebuilt original DB differs from the strictly regraded trajectory")
            row.update(
                status="replayed",
                old=previous,
                new=alternative,
                after=after == expected_hash,
                expected_hash=expected_hash,
                observed_hash=before,
                edited_hash=after,
            )
    return dict(
        study=cfg["name"],
        scope="Exploratory one-field DB replay, selected after observing a development disagreement; no counterfactual customer utterances or model calls.",
        input_sha256=[digest(p.read_bytes()) for p in paths],
        rows=rows,
        status_counts=dict(Counter(r["status"] for r in rows)),
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("round_results", type=Path, nargs="+")
    p.add_argument("--workspace", type=Path, default=ROOT)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        p.error("Use a new output")
    with (
        patch("socket.socket.connect", side_effect=AssertionError("Offline DB replay")) as connect,
        patch("socket.socket.connect_ex", side_effect=AssertionError("Offline DB replay")) as connect_ex,
    ):
        result = replay(a.workspace.resolve(), a.round_results)
        if connect.call_count or connect_ex.call_count:
            raise ContractError("Network attempted during offline DB replay")
    write_json(a.output, result)
    print(result["status_counts"])
