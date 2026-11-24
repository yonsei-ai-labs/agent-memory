"""Offline reached-state diagnostics without inventing a native successful termination."""


def assess_reached_state(orchestrator, config):
    from tau2.data_model.semantic_communication import CommunicateScoring
    from tau2.evaluator.evaluator_env import EnvironmentEvaluator
    from tau2.evaluator.evaluator_communicate import CommunicateEvaluator
    from tau2.registry import registry

    if config["benchmark"]["communicate_scorer"] != "substring":
        return dict(status="not_assessed", reason="requires_model_free_substring_scoring")
    before = (orchestrator.environment.get_db_hash(), orchestrator.environment.get_user_db_hash(),
              orchestrator.step_count, orchestrator.num_errors)
    trajectory = orchestrator.get_trajectory()
    env_kwargs = {}
    context = orchestrator.call_context
    if config["benchmark"]["domain"] == "banking_knowledge":
        from agent_memory_eval.runner.tau2_baseline import native_config
        from tau2.runner.build import _build_env_kwargs
        if "DB" not in orchestrator.task.evaluation_criteria.reward_basis:
            return dict(status="not_assessed", reason="task_has_no_db_reward")
        env_kwargs = _build_env_kwargs(native_config(config), orchestrator.task, language_context=context.language)
    elif context is not None:
        from tau2.multilingual.runtime import language_environment_kwargs
        from tau2.runner.presets import reload_recorded_call
        loaded = reload_recorded_call(context.language, orchestrator.environment.get_domain_name(), canonical_tasks=False)
        env_kwargs = language_environment_kwargs(loaded)
    constructor = registry.get_env_constructor(orchestrator.environment.get_domain_name())
    copies = []
    def fresh(**kwargs):
        environment = constructor(**kwargs)
        copies.append(environment)
        return environment
    try:
        db = EnvironmentEvaluator.calculate_reward(fresh, orchestrator.task, trajectory,
            solo_mode=orchestrator.solo_mode, env_kwargs=env_kwargs, strict_replay=True)
        if copies and (copies[0].get_db_hash(), copies[0].get_user_db_hash()) != before[:2]:
            return dict(status="not_assessed", reason="replayed_state_differs_from_reached_state")
        communicate = CommunicateEvaluator.calculate_reward(orchestrator.task, trajectory,
            scoring=CommunicateScoring(scorer="substring"))
        return dict(status="assessed", scope="reached-state-diagnostic/v1", official_success=None,
            db_component=db.model_dump(mode="json"), communicate_component=communicate.model_dump(mode="json"),
            reached_db_hash=before[0], reached_user_db_hash=before[1],
            native_steps=before[2], native_tool_errors=before[3], model_calls=0)
    except Exception as error:
        return dict(status="not_assessed", reason=type(error).__name__)
    finally:
        after = (orchestrator.environment.get_db_hash(), orchestrator.environment.get_user_db_hash(),
                 orchestrator.step_count, orchestrator.num_errors)
        if after != before:
            raise RuntimeError("Reached-state assessment changed the live environment")
