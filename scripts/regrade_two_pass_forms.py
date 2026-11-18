"""Replay the official native evaluator on archived two-pass trajectories without model access."""
import argparse
from collections import Counter
from contextlib import ExitStack
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.adapters.tau2 import PIN, native_modules, verify_checkout
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import solve_connection, write_json
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, safe_path
from agent_memory_eval.runner.tau2_baseline import build_baseline_orchestrator


def regrade(run, workspace, checkout):
    seal = read_json(safe_path(run, "completion.json").read_bytes())
    control = read_json(asset_text(run, dict(path="control.json", sha256=seal["control_sha256"])).encode())
    rows = read_json(asset_text(run, dict(path="allocations.json", sha256=seal["allocation_sha256"])).encode())
    design = control["design"]
    if PIN != design["source"]["benchmark_revision"]:
        raise ContractError("Unexpected benchmark revision for two-pass regrading")
    connection = read_json(asset_text(workspace, design["model"]["connection_profile_ref"]).encode())
    checkout = verify_checkout(checkout)
    modules = native_modules(checkout)
    from loguru import logger
    from tau2.data_model.simulation import SimulationRun
    from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
    from tau2.multilingual.runtime import language_environment_kwargs
    from tau2.runner.presets import reload_recorded_call
    logger.disable("tau2")
    task_file = checkout / "data/tau2/domains/airline/tasks.json"
    tasks = {t["id"]: t for t in read_json(task_file.read_bytes())}

    @lru_cache(maxsize=None)
    def context(task_id, rep):
        cfg = solve_connection(connection, design, dict(task_id=task_id, repetition=rep), 5)
        orch, native = build_baseline_orchestrator(cfg, tasks[task_id])
        lang = orch.call_context.language
        domain = orch.environment.get_domain_name()
        loaded = reload_recorded_call(lang, domain)
        if (domain != "airline" or native.communicate_scoring.scorer.value != "substring"
                or native.communicate_scoring != orch.call_context.communicate_scoring
                or [s.value for s in orch.task.evaluation_criteria.reward_basis] != ["DB", "COMMUNICATE"]):
            raise ContractError("This read-only replay requires the frozen text airline DB/COMMUNICATE scorer")
        return orch.task, native.communicate_scoring, lang, language_environment_kwargs(loaded) or None

    verified = []
    ungraded = []
    parts = Counter()
    with ExitStack() as stack:
        blocked = RuntimeError("Model or network access during offline regrading")
        guards = [stack.enter_context(patch.object(m, name, side_effect=blocked))
                  for m, name in zip(modules, ("generate", "generate", "completion"))]
        guards += [stack.enter_context(patch(name, side_effect=blocked)) for name in
                   ("socket.socket.connect", "socket.socket.connect_ex", "tau2.evaluator.evaluator_communicate.generate")]
        for row in rows:
            if row["phase"] != "solve":
                continue
            result = row.get("result", {})
            if result.get("grading_status") != "graded":
                if result.get("official_success") is not None or result.get("official_score") is not None:
                    raise ContractError("An ungraded row has an official outcome")
                ungraded.append(dict(index=row["index"], status=row["status"], reason=row.get("reason") or result.get("error_code")))
                continue
            folder = safe_path(run, row["directory"])
            if digest((folder / "log.jsonl").read_bytes()) != result["log_sha256"]:
                raise ContractError("Archived journal differs from the sealed row")
            report = BlobStore(folder).get(result["report_ref"])
            sim = SimulationRun.model_validate(report)
            task, scoring, lang, kwargs = context(row["task_id"], row["repetition"])
            if (sim.task_id != task.id or sim.language_context != lang or sim.termination_reason.value not in {"user_stop", "agent_stop"}):
                raise ContractError("Archived simulation does not match its native task and recorded language")
            # Remove the stored reward before rebuilding the reference and predicted business environments.
            replay = evaluate_simulation(simulation=sim.model_copy(update={"reward_info": None}), task=task,
                evaluation_type=EvaluationType.ALL, solo_mode=False, domain="airline", env_kwargs=kwargs,
                strict_replay=True, communicate_scoring=scoring, customer_language=lang.provider_language)
            reward = replay.model_dump(mode="json")
            if (canonical(reward) != canonical(report["reward_info"]) or replay.reward != result["official_score"]
                    or (replay.reward == 1) is not result["official_success"]):
                raise ContractError(f"Native strict regrade differs at assignment {row['index']}")
            db = replay.db_check.db_match
            comm = all(c.met for c in replay.communicate_checks or [])
            parts[(db, comm)] += 1
            verified.append(dict(index=row["index"], task_id=task.id, repetition=row["repetition"], arm=row["arm"],
                official_score=replay.reward, db_match=db, communication_required=len(replay.communicate_checks or []),
                communication_passed=comm, missing_communication=[c.info for c in replay.communicate_checks or [] if not c.met],
                report_sha256=result["report_ref"]["sha256"], replayed_reward_sha256=digest(canonical(reward))))
        calls = sum(mock.call_count for mock in guards)
        if calls:
            raise ContractError("A model or network operation was attempted during regrading")
    return dict(format="agent-memory-two-pass-native-strict-replay/v1", benchmark_revision=PIN,
        method_sha256=digest(Path(__file__).read_bytes()), benchmark_task_file_sha256=digest(task_file.read_bytes()),
        allocation_sha256=seal["allocation_sha256"], official_results_checked=len(verified),
        successes=sum(r["official_score"] == 1 for r in verified), new_model_or_network_attempts=calls,
        failure_components=dict(db_only=parts[False, True], communication_only=parts[True, False],
            both=parts[False, False], both_passed=parts[True, True]), grades=verified, unavailable=ungraded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.output.exists() or args.output.absolute().is_relative_to(args.run.absolute()):
        raise ContractError("Use a new output outside the evidence root")
    write_json(args.output, regrade(args.run.resolve(), args.workspace.resolve(), args.workspace / "data/tau2-bench"))


if __name__ == "__main__":
    main()
