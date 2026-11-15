"""Offline DB-only no-op/reference replay for the fixed screening train tasks.

This is a posthoc grading diagnostic, not a model trial or a full official score.
All model generators and completion are blocked while replaying local tools.
"""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.adapters.tau2 import native_modules
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.runner.tau2_baseline import build_baseline_orchestrator, preflight_baseline, resolved_connection


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "protocols/pilots/tau2-baseline-screening-v1.json"


def check_grading():
    profile = read_json(PROFILE.read_bytes())
    plan, checkout = preflight_baseline(profile, ROOT / "data/tau2-bench", require_ready=True)
    agent_module, user_module, llm_utils = native_modules(checkout)
    from loguru import logger
    logger.disable("tau2")
    from tau2.evaluator.evaluator_env import EnvironmentEvaluator
    from tau2.registry import registry
    from tau2.runner.build import _build_env_kwargs
    rows = []
    with ExitStack() as stack:
        for module, name in ((agent_module, "generate"), (user_module, "generate"), (llm_utils, "completion")):
            stack.enter_context(patch.object(module, name, side_effect=AssertionError("Model calls are blocked in the grading diagnostic")))
        for task_id in plan["task_ids"]:
            allocation = dict(task_id=task_id, native_seed=profile["native_seeds"][0])
            config = resolved_connection(plan, allocation)
            orch, native = build_baseline_orchestrator(config, plan["tasks"][task_id])
            task = orch.task
            env_kwargs = _build_env_kwargs(native, task, language_context=orch.call_context.language)
            constructor = registry.get_env_constructor(config["benchmark"]["domain"])
            initial = task.initial_state
            history = initial.message_history or [] if initial is not None else []
            replay = constructor(**env_kwargs)
            replay.set_state(initialization_data=initial.initialization_data if initial else None,
                initialization_actions=initial.initialization_actions if initial else None, message_history=history)
            failures = []
            for action in task.evaluation_criteria.actions or []:
                try:
                    replay.make_tool_call(tool_name=action.name, requestor=action.requestor, **action.arguments)
                except Exception as error:
                    failures.append(dict(action_id=action.action_id, tool_name=action.name, error_type=type(error).__name__))
            reward = EnvironmentEvaluator.calculate_reward(constructor, task, full_trajectory=history, env_kwargs=env_kwargs)
            rows.append(dict(task_id=task_id, no_followup_db_reward=reward.db_check.db_reward,
                reference_actions=len(task.evaluation_criteria.actions or []), reference_execution_errors=failures,
                reward_basis=[str(getattr(v, "value", v)) for v in task.evaluation_criteria.reward_basis]))
    for name, sha in plan["asset_hashes"].items():
        assert digest((checkout / name).read_bytes()) == sha, "Source asset changed during local replay"
    return dict(format="agent-memory-tau2-train-grading-diagnostic/v1", visibility="analysis_only_never_worker_or_writer_input",
        scope="posthoc_DB_only_no_followup_and_reference_call_replay_not_model_performance",
        profile_ref=dict(path=str(PROFILE.relative_to(ROOT)), sha256=digest(PROFILE.read_bytes())),
        method_ref=dict(path=str(Path(__file__).resolve().relative_to(ROOT)), sha256=digest(Path(__file__).read_bytes())),
        benchmark_asset_hashes=plan["asset_hashes"], tasks=rows, source_assets_unchanged=True,
        model_calls=0, network_calls=0,
        limitations="DB-only. Reference tool execution does not prove policy compliance, user confirmation or full official success.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-new", type=Path, help="Create one analysis artifact; refuse existing output")
    parser.add_argument("--verify", type=Path, help="Recompute and verify without writing")
    args = parser.parse_args()
    assert not (args.write_new and args.verify)
    if args.write_new:
        assert not args.write_new.exists()
    result = check_grading()
    if args.write_new:
        with args.write_new.open("xb") as stream:
            stream.write(canonical(result) + b"\n")
    if args.verify:
        assert canonical(result) == canonical(read_json(args.verify.read_bytes()))
    print(json.dumps(dict(status="grading_diagnostic_passed", tasks=len(result["tasks"]),
        no_followup_db_pass=sum(r["no_followup_db_reward"] == 1 for r in result["tasks"]),
        reference_execution_errors=sum(len(r["reference_execution_errors"]) for r in result["tasks"]), model_calls=0), indent=2))


if __name__ == "__main__":
    main()
