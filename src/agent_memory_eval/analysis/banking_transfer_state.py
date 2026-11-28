"""Offline banking action and transaction evidence for the own/mixed memory cohort."""

from collections import defaultdict, deque
from types import SimpleNamespace

from agent_memory_eval.adapters.banking_business import (
    AUDIT_TABLES, argument_match, bind_call_requests, decoded, differences, message_parser, public_call, receipt,
    recorded_calls, snapshot,
)
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest


def _row_id(key):
    table, identifier = key
    return dict(table=table, id=identifier, kind="audit" if table in AUDIT_TABLES else "business")


def _operation(before, after, key):
    row = dict(_row_id(key), operation="created" if key not in before else "deleted" if key not in after else "updated")
    if "status" in after.get(key, {}):
        row["status_after"] = after[key]["status"]
    return row


def _entity(key, initial, observed, reference):
    actual, expected = observed.get(key, {}), reference.get(key, {})
    fields = sorted(k for k in actual.keys() | expected.keys()
                    if k not in actual or k not in expected or canonical(actual[k]) != canonical(expected[k]))
    row = dict(_row_id(key), initially_present=key in initial, observed_present=key in observed,
                reference_present=key in reference, different_fields=fields,
                field_differences={k: {name: record[k] for name, record in (("observed", actual), ("reference", expected))
                                       if k in record} for k in fields})
    if "status" in actual or "status" in expected:
        row["final_status"] = {name: record["status"] for name, record in (("observed", actual), ("reference", expected))
                               if "status" in record}
    return row


class BankingTransitions:
    """Observe the two disposable environments constructed by the pinned native evaluator."""

    def __init__(self):
        self.initial = None
        self.calls = defaultdict(deque)
        self.reference_calls = []

    def inspect(self, env, *, predicted):
        if predicted:
            native_response = env.get_response

            def observe(call):
                before = snapshot(env)
                result = native_response(call)
                after = snapshot(env)
                key = call.requestor, call.id, digest(canonical(decoded(call.arguments)))
                self.calls[key].append([_operation(before, after, k)
                                        for k in sorted(differences(before, after))])
                return result

            env.get_response = observe
            return

        initialize, run_action = env.set_state, env.make_tool_call

        def observe_reference(tool_name, requestor, **kwargs):
            before = snapshot(env)
            try:
                result = run_action(tool_name, requestor=requestor, **kwargs)
                text = env.to_json_str(result)
                status = receipt(SimpleNamespace(execution_status="executed", error=False, content=text))
                return result
            except Exception as error:
                text, status = str(error), "reported_error"
                raise
            finally:
                after = snapshot(env)
                self.reference_calls.append(dict(name=tool_name, actor=requestor, arguments=kwargs, receipt=status,
                    error_message=text if status == "reported_error" else None,
                    changed_rows=[_operation(before, after, k)
                                  for k in sorted(differences(before, after))]))

        def reference_start(**kwargs):
            initialize(**kwargs)
            self.initial = snapshot(env)
            env.make_tool_call = observe_reference

        env.set_state = reference_start


def _selected_references(actions, rule):
    if rule.get("kind") != "first_matching_tool":
        return []
    # Several banking actions share a wrapper name. Its registered inner tool identifies the target operation.
    arguments = rule.get("arguments", {})
    operation_keys = {"agent_tool_name", "discoverable_tool_name"}.intersection(arguments)
    return [a for a in actions if a.requestor == "assistant" and a.name in rule["tools"]
            and (not rule.get("expected") or a.name == rule["expected"])
            and all(a.arguments.get(k) == arguments[k] for k in operation_keys)]


def _compare(call, action, action_gates):
    from tau2.data_model.message import ToolCall
    same_tool = action.name == call["name"]
    result = dict(action_id=action.action_id, operation_matches=same_tool, native_action_match=None,
                  compare_args=action.compare_args, exact_arguments_match=None, normalized_full_arguments_match=None,
                  normalized_selected_arguments_match=None, different_arguments=None, matches=False,
                  method="native_action_comparator" if action_gates else "all_arguments_with_decoded_banking_json")
    if not same_tool or not call["arguments_valid"]:
        return result
    expected, actual = decoded(action.arguments), decoded(call["arguments"])
    different = sorted(k for k in expected.keys() | actual.keys()
                       if k not in expected or k not in actual or canonical(expected[k]) != canonical(actual[k]))
    native_call = ToolCall(id=call["call_id"], name=call["name"], requestor="assistant",
                           arguments=call.get("native_arguments", call["arguments"]))
    native_match = action.compare_with_tool_call(native_call)
    return dict(result, native_action_match=native_match, exact_arguments_match=canonical(action.arguments) == canonical(call["arguments"]),
                normalized_full_arguments_match=not different, normalized_selected_arguments_match=argument_match(action, native_call),
                different_arguments=different, matches=native_match if action_gates else not different)


def _business_calls(log, messages, tracker, actions):
    trace = []
    parsed = message_parser().validate_python(messages)
    positions = {id(m): index for index, m in enumerate(parsed)}
    issued, _ = recorded_calls(parsed)
    for index, call, reply in issued:
        require(tracker is None or reply is not None, "Banking state comparison has an unanswered business call")
        entry = public_call(call, index, reply)
        entry.update(native_reply_index=positions.get(id(reply)), native_error=reply.error if reply else None,
                     execution_status=reply.execution_status if reply else None, changed_rows=None)
        if reply is not None and entry["receipt"] != "returned":
            entry["error_message"] = reply.content
        if tracker is not None:
            key = call.requestor, call.id, entry["arguments_sha256"]
            entry["changed_rows"] = tracker.calls[key].popleft() if tracker.calls.get(key) else []
        entry["native_matching_action_ids"] = [a.action_id for a in actions if a.compare_with_tool_call(call)]
        entry["normalized_actor_matching_action_ids"] = [a.action_id for a in actions if argument_match(a, call)]
        trace.append(entry)
    require(tracker is None or not any(tracker.calls.values()), "A banking mutation did not belong to a recorded native call")
    bind_call_requests(trace, log)
    return trace


def banking_state(log, report, native, control, context, rule, calls, evidence, replay):
    from agent_memory_eval.analysis.tau_transfer_state import premature_state
    _, task, _, _, _ = context
    basis = list(task.evaluation_criteria.reward_basis)
    actions = task.evaluation_criteria.actions or []
    selected = _selected_references(actions, rule)
    evidence.update(declared_reward_basis=[r.value for r in basis],
                    official_reward_basis=report["reward_info"]["reward_basis"] if report is not None else None, reference_actions=[
        dict(action_id=a.action_id, name=a.name, actor=a.requestor, arguments=a.arguments, compare_args=a.compare_args)
        for a in selected])
    if rule.get("kind") == "official_only":
        evidence["registered_calls_status"] = "not_applicable"
    for call in calls:
        comparisons = [_compare(call, a, "ACTION" in basis) for a in selected]
        call.update(reference_comparisons=comparisons,
                    matches_any_reference=any(c["matches"] for c in comparisons) if comparisons else None)
    if report is None:
        return dict(status="unavailable", reason="no_official_grade", **evidence)
    premature = premature_state(log, report, native, control, context, evidence)
    if premature is not None:
        premature["public_business_calls"] = _business_calls(log, report["messages"], None, actions)
        return premature
    tracker = BankingTransitions()
    states, checksum, judge_requests = replay(log, report, native, control, context, inspect_environment=tracker.inspect)
    require(len(states) == 2 and tracker.initial is not None, "Native banking grader did not replay both environments")
    observed, reference = states
    actual, expected = observed.get_db_hash(), reference.get_db_hash()
    db_match = actual == expected and observed.get_user_db_hash() == reference.get_user_db_hash()
    reward = report["reward_info"]
    require(actual == native["final_db_hash"] and db_match is reward["db_check"]["db_match"],
            "Strict banking state differs from the sealed final DB hash or grader's comparison")
    trace = _business_calls(log, report["messages"], tracker, actions)
    call_rows = {(c["request"]["id"], c["call_id"]): c for c in trace if c["request"]}
    for call in calls:
        row = call_rows.get((call["request"], call["call_id"]))
        if row:
            require(row["receipt"] == call["receipt"]["status"], "Banking tool return differs from the bound receipt")
            call["changed_rows"] = row["changed_rows"]
    initial, final, gold = tracker.initial, snapshot(observed), snapshot(reference)
    targets = differences(initial, gold)
    extra = differences(initial, final) - targets
    native_actions = []
    for action, run, check in zip(actions, tracker.reference_calls, reward.get("action_checks") or [], strict=True):
        require((action.name, action.requestor, action.arguments) == (run["name"], run["actor"], run["arguments"])
                and check["action"] == action.model_dump(mode="json"), "Native banking reference action changed")
        native_actions.append(dict(run, action_id=action.action_id, compare_args=action.compare_args,
                                   native_action_match=check["action_match"]))
    return dict(status="verified", replayed_reward_sha256=checksum, archived_judge_requests=judge_requests,
                official_db_match=db_match if "DB" in basis else None, diagnostic_db_match=db_match,
                observed_db_sha256=actual, reference_db_sha256=expected, native_reference_actions=native_actions,
                public_business_calls=trace, target_entities=[_entity(k, initial, final, gold) for k in sorted(targets)],
                additional_entities=[_entity(k, initial, final, gold) for k in sorted(extra)], **evidence)
