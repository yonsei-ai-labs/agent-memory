"""Read-only, actor-aware evidence from the native banking environment and its official report."""

from collections import defaultdict, deque
from functools import cache
import json
from unittest.mock import patch

from agent_memory_eval.adapters.tau_banking_opening import _environment
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest


AUDIT_TABLES = {"agent_discoverable_tools", "user_discoverable_tools", "user_discoverable_tool_calls",
                "verification_history", "task_config"}
DEBIT_DISPUTE = "file_debit_card_transaction_dispute_6281"


def decoded(arguments):
    result = dict(arguments)
    value = result.get("arguments")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return result
        if isinstance(parsed, dict):
            result["arguments"] = parsed
    return result


def argument_match(target, call):
    if (call.requestor, call.name) != (target.requestor, target.name):
        return False
    expected, actual = decoded(target.arguments), decoded(call.arguments)
    if target.compare_args is None:
        return canonical(actual) == canonical(expected)
    return all(key in expected and key in actual and canonical(expected[key]) == canonical(actual[key])
               for key in target.compare_args)


def snapshot(env):
    # model_dump detaches nested values, including rows modified in place by the next call.
    return {(table, key): value for table, wrapper in env.tools.db.model_dump().items()
            for key, value in wrapper["data"].items()}


def differences(before, after):
    return {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}


def receipt(message):
    if message is None:
        return "missing"
    if message.execution_status == "rejected":
        return "rejected_before_execution"
    if message.error or str(message.content).strip().casefold().startswith(("error:", "failed to ")):
        return "reported_error"
    return "returned"


def public_call(call, index, reply):
    args = decoded(call.arguments)
    return dict(message_index=index, call_id=call.id, actor=call.requestor, function=call.name,
                tool=args.get("agent_tool_name") or args.get("discoverable_tool_name") or call.name,
                arguments_sha256=digest(canonical(args)), receipt=receipt(reply),
                reply_sha256=digest(canonical(reply.content)) if reply is not None else None)


def bind_call_requests(calls, log):
    """Keep a request pointer only when the speaker, call ID, name and decoded arguments agree."""
    origins = defaultdict(deque)
    for req in log.requests:
        finish = log.finishes[req["request_id"]]
        if finish["status"] != "response" or req["actor_role"] not in {"worker", "user_simulator"}:
            continue
        actor = "assistant" if req["actor_role"] == "worker" else "user"
        for call in log.reply(req)["message"].get("tool_calls") or []:
            fn = call["function"]
            try:
                arguments = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
            except ValueError:
                continue
            if isinstance(arguments, dict):
                key = (actor, call["id"], fn["name"], digest(canonical(decoded(arguments))))
                origins[key].append(dict(id=req["request_id"], sha256=req["request_ref"]["sha256"],
                    manifest_sha256=req["input_manifest_ref"]["sha256"], response_sha256=finish["response_ref"]["sha256"]))
    for c in calls:
        key = c["actor"], c["call_id"], c["function"], c["arguments_sha256"]
        c["request"] = origins[key].popleft() if origins.get(key) else None


@cache
def message_parser():
    from pydantic import TypeAdapter
    from tau2.data_model.message import Message
    return TypeAdapter(list[Message])


def recorded_calls(messages):
    """Match the native call and return order, including an unfinished final tool turn."""
    issued = []
    prefix = []
    index = 0
    while index < len(messages):
        message = messages[index]
        require(message.role != "tool", "Orphan native tool return")
        calls = getattr(message, "tool_calls", None) or []
        replies = []
        for offset, call in enumerate(calls, 1):
            reply = messages[index + offset] if index + offset < len(messages) else None
            if reply is not None:
                require(reply.role == "tool" and reply.id == call.id, "Native call and receipt disagree")
                replies.append(reply)
            require(call.requestor == message.role, "Native call attributed to a different speaker")
            issued.append((index, call, reply))
        if len(replies) < len(calls):
            if replies:
                prefix.extend([message.model_copy(update={"tool_calls": calls[:len(replies)]}), *replies])
            break
        prefix.extend([message, *replies])
        index += len(replies) + 1
    return issued, prefix


class BankingBusiness:
    """Compare actual row transitions with reference transitions from the same registered start."""

    def __init__(self, task, case):
        from tau2.data_model.message import ToolCall
        self.task = task
        env = _environment(task)
        hashes = case.get("initialized_databases") or {"agent": case["initial_db"], "user": case["initial_db"]}
        require((env.get_db_hash(), env.get_user_db_hash()) == (hashes["agent"], hashes["user"]),
                "Business evidence has a different initial state")
        self.initial = snapshot(env)
        self.targets = task.evaluation_criteria.actions or []
        self.reference_errors = []
        self.debit_targets = {}
        for i, action in enumerate(self.targets):
            call = ToolCall(id=f"reference-{i}", requestor=action.requestor, name=action.name, arguments=action.arguments)
            reply = env.get_response(call)
            if receipt(reply) != "returned":
                self.reference_errors.append(public_call(call, i, reply))
            if call.name == "call_discoverable_agent_tool" and call.arguments.get("agent_tool_name") == DEBIT_DISPUTE:
                args = decoded(call.arguments)["arguments"]
                self.debit_targets[args["transaction_id"]] = args
        self.reference = snapshot(env)
        self.reference_hash = env.get_db_hash(), env.get_user_db_hash()
        self.expected = differences(self.initial, self.reference)

    def observe(self, messages, final_hash, reward=None):
        env = _environment(self.task)
        issued, prefix = recorded_calls(message_parser().validate_python(messages))
        require(reward is None or all(reply is not None for _, _, reply in issued), "Missing native graded receipt")
        # set_state is the official replay; it skips pre-dispatch rejections and pure reads.
        # Failed executed calls can still write an audit or business row.
        last = self.initial
        mutations = defaultdict(deque)
        native_response = env.get_response

        def track(call):
            nonlocal last
            reply = native_response(call)
            after = snapshot(env)
            changed = [(k, "created" if k not in last else "deleted" if k not in after else "updated")
                       for k in sorted(differences(last, after))]
            mutations[(call.requestor, call.id, digest(canonical(decoded(call.arguments))))].append(changed)
            last = after
            return reply

        with patch.object(env, "get_response", track):
            env.set_state(initialization_data=None, initialization_actions=None, message_history=prefix, strict=True)
        after = snapshot(env)
        require(after == last and env.get_db_hash() == final_hash, "Business replay differs from sealed final DB")
        if reward and reward.get("db_check") is not None:
            db_match = (env.get_db_hash(), env.get_user_db_hash()) == self.reference_hash
            require(db_match == reward["db_check"]["db_match"], "DB comparison differs from native grader")
        evidence, gifts, creators, debit_comparisons = [], {}, {}, []
        for index, call, reply in issued:
            row = public_call(call, index, reply)
            key = (call.requestor, call.id, row["arguments_sha256"])
            changes = mutations[key].popleft() if mutations.get(key) else []
            row["changed_rows"] = [dict(table=t, key_sha256=digest(k.encode()), operation=op) for (t, k), op in changes]
            args = decoded(call.arguments)
            if call.name == "give_discoverable_user_tool" and row["receipt"] == "returned":
                gifts[row["tool"]] = (call.id, args.get("arguments"))
            if call.requestor == "user" and call.name == "call_discoverable_user_tool":
                if row["tool"] in gifts:
                    gift, suggested = gifts[row["tool"]]
                    row["grant_call"] = gift
                    row["matches_suggested_arguments"] = None if suggested is None else suggested == args.get("arguments")
            for changed_key, operation in changes:
                if operation == "created":
                    creators[changed_key] = call.requestor
                elif operation == "deleted":
                    creators.pop(changed_key, None)
            if call.requestor == "assistant" and call.name == "call_discoverable_agent_tool" and row["tool"] == DEBIT_DISPUTE:
                actual = args.get("arguments")
                target = self.debit_targets.get(actual.get("transaction_id")) if isinstance(actual, dict) else None
                debit_comparisons.append(dict(call_id=call.id, is_target=target is not None, receipt=row["receipt"],
                    different_reference_fields=sorted(k for k, v in target.items()
                        if k not in actual or actual[k] != v) if target else None))
            evidence.append(row)
        require(not any(mutations.values()), "Replayed changes have no recorded call")
        changed = differences(self.initial, after)
        tables = {}
        for table in sorted({t for t, _ in self.expected | changed}):
            expected = {k for k in self.expected if k[0] == table}
            observed = {k for k in changed if k[0] == table}
            tables[table] = dict(kind="audit" if table in AUDIT_TABLES else "business", reference_changed=len(expected),
                actual_changed=len(observed), target_keys_changed=len(expected & observed),
                target_values_matched=sum(after.get(k) == self.reference.get(k) for k in expected),
                additional_changed=len(observed - expected))
        calls = [c for _, c, _ in issued]
        native = [any(a.compare_with_tool_call(c) for c in calls) for a in self.targets]
        official = reward.get("action_checks") if reward else None
        if official is not None:
            require(native == [a["action_match"] for a in official], "Native action checklist differs")
        expected_matches = {a.action_id: [c.id for c in calls if argument_match(a, c)] for a in self.targets}
        customer_extra = sum(creators.get(k) == "user" for k in after.keys() - self.initial.keys() - self.reference.keys())
        result = dict(scope="graded" if reward is not None else "observed_prefix", verified_db_sha256=final_hash,
                      calls=evidence, tables=tables, customer_extra_final_rows=customer_extra,
                      reference_errors=self.reference_errors, reference_actions=len(self.targets),
                      official_action_matches=sum(native) if official is not None else None,
                      normalized_actor_matches=sum(bool(v) for v in expected_matches.values()), matched_calls=expected_matches)
        if self.debit_targets:
            opened = {after[k]["transaction_id"] for k in after.keys() - self.initial.keys()
                      if k[0] == "debit_card_disputes" and after[k].get("status") == "OPEN"}
            result["debit_disputes"] = dict(targets=len(self.debit_targets), target_opened=len(opened & self.debit_targets.keys()),
                other_opened=len(opened - self.debit_targets.keys()), calls=debit_comparisons)
        return result
