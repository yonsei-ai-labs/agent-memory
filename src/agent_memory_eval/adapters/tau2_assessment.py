"""Offline assessment of recorded native actions and of the state reached before termination."""

from agent_memory_eval.contracts.core import ContractError


def single_action_attempts(report, *, expected_action=None):
    """Score the declared actor's attempts in transcript order for a single native ACTION grade."""
    reward = report["reward_info"]
    checks = reward.get("action_checks") or []
    if reward.get("reward_basis") != ["ACTION"] or len(checks) != 1:
        return None

    from tau2.data_model.message import ToolCall
    from tau2.data_model.tasks import Action

    check, = checks
    action = Action.model_validate(check["action"])
    if expected_action is not None and action != Action.model_validate(expected_action):
        raise ContractError("Native action check differs from the registered task")

    attempts = []
    any_actor_matched = False
    for turn, message in enumerate(report["messages"]):
        actor = message["role"]
        if actor not in ("assistant", "user"):
            continue
        for index, call in enumerate(message.get("tool_calls") or []):
            if call["name"] != action.name:
                continue
            matched = action.compare_with_tool_call(ToolCall.model_validate(call))
            # Native ACTION considers both actors. The diagnostic follows the declared actor.
            any_actor_matched |= matched
            if actor == action.requestor:
                attempts.append(dict(turn=turn, call_index=index, matched=matched))
    if check["action_match"] is not any_actor_matched or (reward["reward"] == 1) != any_actor_matched:
        raise ContractError("Native single-action grade disagrees with its recorded tool calls")
    first_correct = next((n for n, a in enumerate(attempts, 1) if a["matched"]), None)
    return dict(action_id=action.action_id, name=action.name, actor=action.requestor,
                official_match=any_actor_matched, first_correct_attempt=first_correct, attempts=attempts)


def action_attempt_observation(folder, row, expected_action):
    """Bind an allocated consultation to its recorded grade and the calls in the sealed transcript."""
    from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
    from agent_memory_eval.recording.journal import safe_path

    result = row.get("result") or {}
    success = result.get("official_success")
    graded = result.get("grading_status") == "graded"
    require(type(success) is bool if graded else success is None, "Allocation grade and status disagree")
    record = dict(task=row["task_id"], slot=row["index"], arm=row["arm"], run=row.get("run_id"),
                  status=result.get("grading_status", row["status"]), official_success=success, action=None)
    if not graded:
        return record
    log = RecordedDecisions(safe_path(folder, row["directory"]), record["run"], result["log_sha256"])
    scored, = log.of_type("outcome.scored")
    ref = scored["payload"]["official_artifact_ref"]
    report = log.blobs.get(ref)
    allocated_ref = result.get("report_ref") or result.get("native_report_ref")
    require(ref == allocated_ref and report["task_id"] == row["task_id"] and
            success is scored["payload"]["official_success"] and success == (report["reward_info"]["reward"] == 1),
            "The native report and recorded grade differ")
    action = single_action_attempts(report, expected_action=expected_action)
    require(action is not None or report["reward_info"].get("reward_basis") is None,
            "Native report has a different action assessment")
    return dict(record, report_sha256=ref["sha256"], action=action)


def action_attempt_counts(records):
    """Count first and later hits over assessed consultations, keeping ungraded allocations explicit."""
    records = list(records)
    actions = [r["action"] for r in records if r["action"] is not None]
    hits = [a["first_correct_attempt"] for a in actions]
    return dict(assigned=len(records), graded=sum(r["official_success"] is not None for r in records),
                official_success=sum(r["official_success"] is True for r in records),
                assessed_consultations=len(actions), first_correct=hits.count(1),
                later_correct=sum(hit is not None and hit > 1 for hit in hits), no_correct_attempt=hits.count(None),
                no_attempt=sum(not a["attempts"] for a in actions))


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
