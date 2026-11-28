"""Analyst-only evidence for the target order or reservation in a sealed transfer episode."""

from collections import Counter, defaultdict, deque
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.adapters.retail_grader import checked_answer, judge_wire
from agent_memory_eval.adapters.tau2 import PIN
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest, read_json


ENTITIES = {"airline": ("reservations", "reservation_id"), "retail": ("orders", "order_id")}
ITEM_CHANGES = {"modify_pending_order_items", "exchange_delivered_order_items"}


def unmeasured_state(domain, reason, *, cohort=False):
    if domain is not None and domain not in ENTITIES and not (cohort and domain == "banking_knowledge"):
        return dict(status="not_applicable", reason="no_order_or_reservation_comparison")
    return dict(status="unavailable", reason=reason)


def _call_receipts(calls, messages, id_field, *, bank=False):
    """Bind wire calls to native messages by actor, ID, name and arguments, including incomplete runs."""
    from agent_memory_eval.adapters.banking_business import decoded

    def key(call_id, name, arguments):
        return call_id, name, canonical(decoded(arguments) if bank else arguments)

    native, returns = defaultdict(deque), defaultdict(list)
    for index, msg in enumerate(messages):
        if msg["role"] == "assistant":
            for offset, call in enumerate(msg.get("tool_calls") or [], 1):
                if call.get("requestor", "assistant") == "assistant":
                    native[key(call["id"], call["name"], call["arguments"])].append((index, index + offset, call["arguments"]))
        elif msg["role"] == "tool" and msg.get("requestor", "assistant") == "assistant":
            returns[msg["id"]].append((index, msg))
    for call in calls:
        call["native_call_index"] = None
        call["receipt"] = dict(status="native_call_unavailable")
        recorded = native[key(call["call_id"], call["name"], call["arguments"])]
        if not call["arguments_valid"] or not recorded:
            continue
        call["native_call_index"], next_index, native_args = recorded.popleft()
        if bank and native_args != call["arguments"]:
            call["native_arguments"] = native_args
        matches = returns[call["call_id"]]
        adjacent = [r for r in matches if r[0] == next_index]
        # A historical partial transcript can have an inverted timestamp. A unique ID still binds the return.
        candidates = adjacent or matches
        if len(candidates) != 1:
            call["receipt"] = dict(status="ambiguous_native_return" if matches else "missing_native_return")
            continue
        index, reply = candidates[0]
        matches.remove(candidates[0])
        status = "returned"
        if reply.get("execution_status") == "rejected":
            status = "rejected_before_execution"
        elif reply.get("error") or (bank and str(reply.get("content")).strip().casefold().startswith(("error:", "failed to "))):
            status = "reported_error"
        receipt = dict(status=status, native_reply_index=index, reply_sha256=digest(canonical(reply)),
                       execution_status=reply.get("execution_status"), error=reply.get("error"))
        if status != "returned":
            receipt["error_message"] = reply.get("content")
        elif id_field:
            try:
                content = read_json(reply["content"])
            except (TypeError, ValueError):
                content = None
            entity = content.get(id_field) if isinstance(content, dict) else None
            requested = call["arguments"].get(id_field)
            receipt.update(returned_entity_id=entity, returned_requested_entity=(
                entity == requested if entity is not None and requested is not None else None))
        call["receipt"] = receipt


def _replacement_pairs(args):
    old, new = args.get("item_ids"), args.get("new_item_ids")
    if not (isinstance(old, list) and isinstance(new, list) and len(old) == len(new)
            and all(isinstance(item, str) for item in (*old, *new))):
        return None
    return Counter(zip(old, new))


def registered_arguments_match(operation, observed, required):
    """Check preregistered fields. An ordered old/new pair must remain together if items are reordered."""
    if operation == "call_discoverable_agent_tool":
        from agent_memory_eval.adapters.banking_business import decoded
        observed, required = decoded(observed), decoded(required)
    keys = required.keys()
    if operation in ITEM_CHANGES and {"item_ids", "new_item_ids"} <= keys:
        pairs = _replacement_pairs(required)
        if pairs is None or _replacement_pairs(observed) != pairs:
            return False
        keys = keys - {"item_ids", "new_item_ids"}
    return all(k in observed and canonical(observed[k]) == canonical(required[k]) for k in keys)


def compare_reference_call(call, action):
    """Compare every supplied and expected argument; the registered subset score is kept separately."""
    same_tool = action.name == call["name"]
    method = "all_arguments_with_paired_item_multiset" if action.name in ITEM_CHANGES else "all_arguments_exact"
    result = dict(action_id=action.action_id, method=method, operation_matches=same_tool, arguments_match=None,
                  exact_arguments_match=None, different_arguments=None, matches=False)
    if same_tool and call["arguments_valid"]:
        observed, wanted = call["arguments"], action.arguments
        different = {k for k in observed.keys() | wanted.keys()
                     if k not in observed or k not in wanted or canonical(observed[k]) != canonical(wanted[k])}
        exact = not different
        if action.name in ITEM_CHANGES:
            actual, expected = _replacement_pairs(observed), _replacement_pairs(wanted)
            if actual is not None and expected is not None:
                # Only a joint permutation preserves the old/new pairs. Keep duplicates and any extra arguments.
                different -= {"item_ids", "new_item_ids"}
                if actual != expected:
                    different.add("item_replacements")
                    result["replacement_differences"] = {
                        label: [dict(item_id=old, new_item_id=new, count=n) for (old, new), n in sorted(delta.items())]
                        for label, delta in (("missing", expected - actual), ("extra", actual - expected))
                    }
        result.update(arguments_match=not different, exact_arguments_match=exact,
                      different_arguments=sorted(different), matches=not different)
    return result


@lru_cache(maxsize=4)
def _native_context(workspace, native_bytes):
    """Reuse the pinned task, resolved language and untouched initial data across rows for that target."""
    from agent_memory_eval.study.adapters.tau_banking import modules_for
    modules_for(workspace)
    from tau2.data_model.simulation import TextRunConfig
    from tau2.multilingual.runtime import language_environment_kwargs, pin_language_artifacts, resolve_task
    from tau2.registry import registry
    from tau2.runner.helpers import select_run_tasks

    config = TextRunConfig.model_validate_json(native_bytes)
    loaded = pin_language_artifacts(config, update=False)
    task, = select_run_tasks(config, loaded)
    language = resolve_task(loaded, task, config.seed or 0)
    if config.domain == "banking_knowledge":
        from tau2.runner.build import _build_env_kwargs
        require(config.retrieval_config == "grep_only", "Expected the registered offline banking retrieval variant")
        options = _build_env_kwargs(config, task, language_context=language)
    else:
        options = language_environment_kwargs(loaded)
    initial = registry.get_env_constructor(config.domain)(**options)
    return config, task, language, options, (initial.get_db_hash(), initial.get_user_db_hash())


def _reference_entities(task, rule, domain):
    table, id_field = ENTITIES[domain]
    actions = task.evaluation_criteria.actions or []
    registered_id = rule.get("arguments", {}).get(id_field)
    ids = sorted({a.arguments[id_field] for a in actions if isinstance(a.arguments.get(id_field), str)})
    if registered_id is not None:
        ids = [registered_id]
    references = [a for a in actions if a.requestor == "assistant" and a.name in rule.get("tools", ())
                  and (registered_id is None or a.arguments.get(id_field) == registered_id)]
    return table, ids, references


def _entity_fields(table, key, observed, reference):
    def fields(env):
        entity = getattr(env.tools.db, table).get(key)
        return entity.model_dump(mode="json") if entity is not None else None
    actual, expected = fields(observed), fields(reference)
    differences = {}
    for field in sorted((actual or {}).keys() | (expected or {}).keys()):
        values = {label: row[field] for label, row in (("observed", actual), ("reference", expected))
                  if row is not None and field in row}
        if len(values) < 2 or canonical(values["observed"]) != canonical(values["reference"]):
            differences[field] = values
    return dict(table=table, id=key, observed_present=actual is not None, reference_present=expected is not None,
                different_fields=list(differences), field_differences=differences)


def _replay(log, report, native, control, context, *, inspect_environment=None):
    from tau2.data_model.simulation import SimulationRun
    from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
    from tau2.evaluator.evaluator_env import EnvironmentEvaluator

    config, task, language, options, _ = context
    sim = SimulationRun.model_validate(report)
    require(sim.task_id == task.id and sim.language_context == language and sim.seed == config.seed
            and sim.mode == "half_duplex" and config.communicate_scoring.scorer == "substring",
            "Transfer report differs from its pinned text task, seed, language or substring scorer")
    before_ref = native.get("before_grade_ref")
    if before_ref:
        require(log.blobs.get(before_ref) == dict(report, reward_info=None),
                "Native trajectory changed between the pre-grade and final receipts")
    requests = [r for r in log.requests if r["actor_role"] == "grader"]
    recorded = []

    def answer(*, model, messages, call_name, **kwargs):
        require(len(requests) == 1 and not recorded, "Missing or extra archived assertion-judge request")
        req, = requests
        assignment = log.state["assignments"][log.run]["native_grade_limits"]
        cfg, endpoint = (log.blobs.get(assignment[k]) for k in ("config_ref", "model_ref"))
        require(cfg == control["grader"] and judge_wire(cfg, model, messages, call_name, kwargs) == log.blobs.get(req["request_ref"]),
                "Reconstructed assertion-judge request differs from the sealed input")
        end = log.finishes[req["request_id"]]
        require(end["status"] == "response", "Missing archived assertion-judge response")
        recorded.append(req["request_id"])
        return checked_answer(log.blobs.get(end["response_ref"]), task.evaluation_criteria.nl_assertions, cfg, endpoint)

    # Retain the actual environments the pinned scorer uses, including its reference actions and initialization.
    environments = []
    evaluate_environment = EnvironmentEvaluator.calculate_reward

    def retain_environments(environment_constructor, **kwargs):
        def capture(**env_kwargs):
            env = environment_constructor(**env_kwargs)
            if inspect_environment is not None:
                inspect_environment(env, predicted="solo_mode" in env_kwargs)
            environments.append(env)
            return env
        return evaluate_environment(capture, **kwargs)

    with patch.object(EnvironmentEvaluator, "calculate_reward", retain_environments), \
            patch("tau2.evaluator.evaluator_nl_assertions.generate", answer):
        reward = evaluate_simulation(simulation=sim.model_copy(update={"reward_info": None}), task=task,
            evaluation_type=EvaluationType.ALL, solo_mode=False, domain=config.domain, env_kwargs=options,
            strict_replay=True, communicate_scoring=config.communicate_scoring, customer_language=language.provider_language)
    checksum = digest(canonical(reward.model_dump(mode="json")))
    require(checksum == digest(canonical(report["reward_info"])) and len(recorded) == len(requests),
            "Strict transfer replay disagrees with the archived native grade or judge receipts")
    return environments, checksum, recorded


def premature_state(log, report, native, control, context, evidence):
    """Verify the native zero for a capped consultation before any outcome criteria were evaluated."""
    if report["termination_reason"] in {"agent_stop", "user_stop"}:
        return None
    states, checksum, requests = _replay(log, report, native, control, context)
    reward = report["reward_info"]
    require(not states and not requests and reward["reward"] == 0 and reward["reward_basis"] is None
            and reward.get("db_check") is None and reward.get("action_checks") is None,
            "Premature native grade contains an evaluated state or action criterion")
    status = evidence["registered_calls_status"]
    return dict(evidence, status="not_applicable", reason="premature_termination_before_native_state_comparison",
                termination_reason=report["termination_reason"], declared_reward_basis=[
                    basis.value for basis in context[1].evaluation_criteria.reward_basis],
                official_reward_basis=None, official_db_match=None, diagnostic_db_match=None,
                registered_calls_status="no_call_before_termination" if status == "no_call" else status,
                replayed_reward_sha256=checksum, archived_judge_requests=requests,
                recorded_final_db_sha256=native["final_db_hash"])


def state_diagnostics(log, row, transfer, report, calls):
    """Read registered calls from the worker wire and the final DB/grade only from native analyst receipts."""
    result = row["result"]
    transitions = [e["payload"] for e in log.of_type("business.transition")]
    native, = [e for e in transitions if e.get("origin") == "forms.native_result"]
    require(all(native.get(k) == result.get(k) for k in (
        "partial_trajectory_ref", "native_report_ref", "before_grade_ref", "final_db_hash"
    )), "Native state receipt differs from the allocation")
    domain = transfer.get("domain")
    bank = domain == "banking_knowledge" and transfer.get("cohort", False)
    evidence_ref = native.get("native_report_ref") or native.get("partial_trajectory_ref")
    require(report is None or evidence_ref == result["report_ref"], "Different official and trajectory receipts")
    messages = report["messages"] if report is not None else []
    if report is None and evidence_ref:
        raw = log.blobs.get(evidence_ref)
        messages = raw if isinstance(raw, list) else raw["messages"]
    _call_receipts(calls, messages, ENTITIES.get(domain, (None, None))[1], bank=bank)
    evidence = dict(trajectory_sha256=evidence_ref["sha256"] if evidence_ref else None,
                    registered_calls_status="observed" if calls else "no_call" if report else "incomplete_no_call",
                    first_registered_call=calls[0] if calls else None, subsequent_registered_calls=calls[1:])
    workspace = transfer.get("workspace")
    if not bank and domain not in ENTITIES:
        return dict(unmeasured_state(domain, "domain_not_supplied"), **evidence)
    if workspace is None or not (Path(workspace) / "data/tau2-bench").is_dir() or find_spec("tau2") is None:
        return dict(status="unavailable", reason="pinned_native_checkout_or_install_unavailable", **evidence)
    controls = [e for e in transitions if e.get("origin") == "matched_trial.native_control"]
    if not controls and report is None:
        return dict(status="unavailable", reason="no_native_controls", **evidence)
    event, = controls
    control = log.blobs.get(event["control_ref"])
    require(transfer.get("native_sha256", event["control_ref"]["sha256"]) == event["control_ref"]["sha256"],
            "Target native controls differ from the registration")
    bench = control["config"]["benchmark"]
    require(bench["source_revision"] == PIN and bench["domain"] == domain and bench["task_id"] == row["task_id"],
            "Native controls belong to a different benchmark revision, domain or task")
    context = _native_context(Path(workspace).resolve(), canonical(control["native"]))
    config, task, language, _, initial = context
    require(config.domain == domain and task.id == row["task_id"]
            and digest(canonical(task.model_dump(mode="json"))) == control["task_sha256"]
            and initial == (control["initial_db"], control["initial_user_db"]), "Native task or initial state changed")
    evidence.update(benchmark_revision=PIN, native_control_sha256=event["control_ref"]["sha256"],
                    task_sha256=language.task_sha256)
    if bank:
        from agent_memory_eval.analysis.banking_transfer_state import banking_state
        return banking_state(log, report, native, control, context, transfer["rule"], calls, evidence, _replay)
    table, ids, references = _reference_entities(task, transfer["rule"], domain)
    evidence["reference_actions"] = [dict(action_id=a.action_id, name=a.name, arguments=a.arguments) for a in references]
    for call in calls:
        comparisons = [compare_reference_call(call, action) for action in references]
        call.update(reference_comparisons=comparisons,
                    matches_any_reference=any(c["matches"] for c in comparisons) if comparisons else None)
    if report is None:
        return dict(unmeasured_state(domain, "no_official_grade"), **evidence)
    if transfer.get("cohort"):
        premature = premature_state(log, report, native, control, context, evidence)
        if premature is not None:
            return premature
    if "DB" not in (report["reward_info"]["reward_basis"] or []):
        return dict(status="not_applicable", reason="no_native_db_criterion", **evidence)
    states, checksum, judge_requests = _replay(log, report, native, control, context)
    evidence.update(replayed_reward_sha256=checksum, archived_judge_requests=judge_requests)
    if not states:
        return dict(status="not_applicable", reason="native_evaluator_has_no_reference_state", **evidence)
    observed, reference = states
    actual, expected = observed.get_db_hash(), reference.get_db_hash()
    db_match = actual == expected and observed.get_user_db_hash() == reference.get_user_db_hash()
    require(actual == native["final_db_hash"] and db_match is report["reward_info"]["db_check"]["db_match"],
            "Strict transfer state differs from the recorded final DB hash or native DB component")
    return dict(status="verified", official_db_match=db_match, observed_db_sha256=actual, reference_db_sha256=expected,
                target_entities=[_entity_fields(table, key, observed, reference) for key in ids], **evidence)
