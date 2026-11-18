"""Replay task 17's four writing-comparison trajectories with one changed baggage argument."""
import argparse
from pathlib import Path

from agent_memory_eval.adapters.tau2 import PIN, native_modules, verify_checkout
from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, safe_path
from review_tau2_baseline_screening import write_tool_names


def state_hashes(env):
    return env.get_db_hash(), env.get_user_db_hash()


def probe(run, checkout):
    native_modules(checkout)
    from tau2.data_model.simulation import SimulationRun
    from tau2.data_model.tasks import Task
    from tau2.multilingual.runtime import language_environment_kwargs
    from tau2.registry import registry
    from tau2.runner.presets import reload_recorded_call

    seal = read_json((run / "completion.json").read_bytes())
    rows = read_json(asset_text(run, dict(path="allocations.json", sha256=seal["allocation_sha256"])).encode())
    ctl = read_json(asset_text(run, dict(path="control.json", sha256=seal["control_sha256"])).encode())
    if ctl["design"]["source"]["benchmark_revision"] != PIN or ctl["profile"]["name"] != "tau2-fact-recheck-01":
        raise ContractError("Expected the sealed writing comparison on the pinned airline benchmark")
    task_file = checkout / "data/tau2/domains/airline/tasks.json"
    task = Task.model_validate(next(t for t in read_json(task_file.read_bytes()) if t["id"] == "17"))
    if task.initial_state is not None or task.evaluation_criteria.communicate_info:
        raise ContractError("The expected initial database or grading requirements changed")
    writes = set(write_tool_names((checkout / "src/tau2/domains/airline/tools.py").read_text()))
    factory = registry.get_env_constructor("airline")
    selected = [r for r in rows if r["phase"] == "solve" and r["task_id"] == "17"
                and r["arm"] in {"fact_original", "fact_recheck"}]
    if len(selected) != 4:
        raise ContractError("Expected both writer conditions for both repetitions")
    cells = []
    for r in selected:
        result = r["result"]
        folder = safe_path(run, r["directory"])
        sim = SimulationRun.model_validate(BlobStore(folder).get(result["report_ref"]))
        if sim.task_id != "17" or digest((folder / "log.jsonl").read_bytes()) != result["log_sha256"]:
            raise ContractError("The native episode or its journal differs from the allocation")
        options = language_environment_kwargs(reload_recorded_call(sim.language_context, "airline")) or {}
        gold = factory(**options)
        for step in task.evaluation_criteria.actions:
            gold.make_tool_call(step.name, requestor=step.requestor, **step.arguments)
        strict, replay, edited = (factory(**options) for _ in range(3))
        strict.set_state(initialization_data=None, initialization_actions=None, message_history=sim.messages, strict=True)
        replies = {m.id: m for m in sim.messages if m.role == "tool"}
        args_changed = []
        for i, m in enumerate(sim.messages):
            for c in getattr(m, "tool_calls", None) or []:
                if c.name not in writes or replies[c.id].execution_status == "rejected":
                    continue
                if replay.get_response(c).error != replies[c.id].error:
                    raise ContractError("A recorded write's error status differs during replay")
                counterfactual = c
                if c.name == "update_reservation_baggages":
                    quantity = c.arguments["nonfree_baggages"]
                    if quantity not in (0, 1) or c.arguments["total_baggages"] != 3:
                        raise ContractError("The observed baggage action differs from the diagnostic premise")
                    counterfactual = c.model_copy(update={"arguments": dict(c.arguments, nonfree_baggages=1 - quantity)})
                    args_changed.append(dict(index=i, tool=c.name, original=c.arguments, edited=counterfactual.arguments))
                if edited.get_response(counterfactual).error:
                    raise ContractError("The edited action path returned a tool error")
        recorded, predicted, alternate, reference = map(state_hashes, (strict, replay, edited, gold))
        if (len(args_changed) != 1 or recorded != predicted or recorded[0] != result["final_db_hash"]
                or (recorded == reference) is not sim.reward_info.db_check.db_match
                or sim.reward_info.db_check.db_match is not result["official_success"]):
            raise ContractError("Baseline action replay does not reproduce the official database outcome")
        cells.append(dict(job=r["index"], arm=r["arm"], repetition=r["repetition"], changed=args_changed[0],
            native_report_sha256=result["report_ref"]["sha256"], official_success=result["official_success"],
            edited_db_match=alternate == reference, original_db_sha256=recorded[0], edited_db_sha256=alternate[0],
            reference_db_sha256=reference[0]))
    return dict(format="agent-memory-fact-recheck-baggage-replay/v1", benchmark_revision=PIN,
        benchmark_tasks_sha256=digest(task_file.read_bytes()), allocation_sha256=seal["allocation_sha256"],
        method_sha256=digest(Path(__file__).read_bytes()), cells=cells,
        scope="Post hoc simulator intervention on one argument. Conversations, stored memories and official grades preserved.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    args = parser.parse_args()
    run, output = args.run.resolve(), args.output.resolve()
    if output.exists() or output.is_relative_to(run):
        raise ContractError("Choose a new analysis output outside the sealed run")
    write_json(output, probe(run, verify_checkout(args.workspace / "data/tau2-bench")))


if __name__ == "__main__":
    main()
