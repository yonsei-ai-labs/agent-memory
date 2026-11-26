"""Tie an account-opening attempt to its public return and the new native account row."""

from copy import deepcopy
import json
import re

from agent_memory_eval.analysis.paired_trials import target_effects
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest

OPEN = "open_bank_account_4821"
DISPATCH = "call_discoverable_agent_tool"
FIELDS = ("user_id", "account_type", "account_class")
OPENING_OUTCOMES = ("opened", "eligibility_rejected", "request_rejected", "opening_failed")


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def account_opening_population(tasks):
    return {task.id for task in tasks if task.evaluation_criteria
            and task.evaluation_criteria.reward_basis == ["DB"]
            and any(a.requestor == "assistant" and a.name == DISPATCH and a.arguments.get("agent_tool_name") == OPEN
                    for a in (task.evaluation_criteria.actions or []))}


def initial_record_audit(domain, native, task_ids):
    """Some raw tasks repeat default DB rows using fields the pinned native loader ignores."""
    base = json.loads((domain / "db.json").read_bytes())
    evidence = {}
    for tid in task_ids:
        raw = json.loads((domain / "tasks" / (tid + ".json")).read_bytes())
        initial = (raw.get("initial_state") or {}).get("initialization_data") or {}
        if not initial:
            continue
        state = native[tid].initial_state.initialization_data
        require(state.agent_data is None and state.user_data is None, "Native initialization changed")
        matched = {table: sum(base[table]["data"].get(key) == row for key, row in records.items())
                   for table, records in initial.items()}
        require(all(matched[t] == len(rows) for t, rows in initial.items()),
                "Native loader discarded a non-default initial DB record")
        evidence[tid] = dict(identical_default_rows=matched, raw_sha256=digest(canonical(initial)))
    return evidence


def target_accounts(task):
    result = []
    for action in task.evaluation_criteria.actions:
        if action.requestor == "assistant" and action.name == DISPATCH and action.arguments.get("agent_tool_name") == OPEN:
            args = _object(action.arguments["arguments"])
            require(all(isinstance(args.get(k), str) and args[k] for k in FIELDS), "Invalid reference opening")
            result.append({k: args[k] for k in FIELDS})
    require(result and len({canonical(t) for t in result}) == len(result), "Expected distinct account openings")
    return result


def _result(content, args, error):
    if not isinstance(content, str):
        return "unobserved", None
    if error or content.startswith("Error:"):
        if content == "Error: Account eligibility requirements not met.":
            return "eligibility_rejected", None
        return "request_rejected", None
    if content.startswith("Failed to open account:"):
        return "opening_failed", None
    if content.splitlines()[:1] == ["Bank account opened successfully!"]:
        fields = dict(re.findall(r"^  - ([^:\n]+): (.+)$", content, flags=re.MULTILINE))
        if fields.get("Status") == "OPEN" and fields.get("Account ID") and all(
            fields.get(label) == args.get(key) for key, label in zip(FIELDS, ("User ID", "Account Type", "Account Class"))
        ):
            return "opened", fields["Account ID"]
    return "unrecognized", None


def opening_receipts(messages):
    """Accept native messages or the writer's public projection. Only assistant-issued openings count."""
    rows, by_id = [], {}
    for index, msg in enumerate(messages):
        if msg["role"] == "assistant":
            for call in msg.get("tool_calls") or []:
                fn = call.get("function", call)
                outer = _object(fn.get("arguments"))
                if fn.get("name") != DISPATCH or outer.get("agent_tool_name") != OPEN:
                    continue
                key = call["id"]
                require(key and key not in by_id, "Ambiguous account-opening call ID")
                args = _object(outer.get("arguments"))
                row = dict(call_id=key, call_index=index, arguments={k: args.get(k) for k in FIELDS},
                           outcome="unobserved", account_id=None, receipt_index=None,
                           receipt=None, receipt_sha256=None)
                by_id[key] = row
                rows.append(row)
        elif msg["role"] == "tool" and msg.get("requestor") in {None, "assistant"}:
            row = by_id.get(msg.get("tool_call_id", msg.get("id")))
            if row is not None:
                require(row["receipt_index"] is None, "Multiple public receipts for the same opening")
                content = msg.get("content")
                outcome, account_id = _result(content, row["arguments"], msg.get("error"))
                row.update(outcome=outcome, account_id=account_id, receipt_index=index, receipt=content,
                           receipt_sha256=digest(content.encode()) if isinstance(content, str) else None)
    return rows


def account_effects(targets, receipts):
    attempted = [(canonical(r["arguments"]), r["outcome"] == "opened" if r["outcome"] in OPENING_OUTCOMES else None)
                 for r in receipts]
    effects = target_effects([canonical(t) for t in targets], attempted)
    confirmed_ids = [r["account_id"] for r in receipts if r["outcome"] == "opened"]
    return dict(required_targets=effects["expected"], distinct_targets_opened=effects["accepted_target"],
                all_target_accounts_opened=effects["targets_without_confirmed_effect"] == 0,
                other_accounts_opened=effects["accepted_other"],
                repeated_confirmations=len(confirmed_ids) - len(set(confirmed_ids)),
                refused_or_invalid=effects["rejected"], unresolved=effects["unconfirmed"],
                opening_attempts=effects["attempted"])


def _environment(task):
    from tau2.domains.banking_knowledge.environment import get_environment

    env = get_environment(task=task, retrieval_variant="grep_only")
    start = task.initial_state
    require(not start or not start.message_history, "This account audit requires an empty initial conversation")
    env.set_state(initialization_data=start.initialization_data if start else None,
                  initialization_actions=start.initialization_actions if start else None, message_history=[])
    return env


def verify_account_rows(before_ids, accounts, receipts):
    new_ids = accounts.keys() - before_ids
    confirmed = {r["account_id"]: r["arguments"] for r in receipts if r["outcome"] == "opened"}
    require(new_ids == confirmed.keys() and all(
        all(accounts[key].get(k) == value for k, value in args.items()) for key, args in confirmed.items()
    ), "Confirmed openings and actual new account records differ")
    return len(new_ids)


def grader_controls(task):
    """Run public reference actions and counterfactuals with an isolated DB per variant."""
    from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage
    from tau2.domains.banking_knowledge.environment import get_environment
    from tau2.evaluator.evaluator_env import EnvironmentEvaluator

    targets = target_accounts(task)
    result = {}
    for variant in ("reference", "omit_openings", "invalid_account_type", "ineligible_savings", "wrong_product"):
        env = _environment(task)
        before_ids = set(env.tools.db.accounts.data)
        messages = []
        if variant == "ineligible_savings":
            with_checking = {a["user_id"] for a in env.tools.db.accounts.data.values()
                             if a.get("account_type", a.get("class")) in {"checking", "personal_checking"}
                             and a.get("status") in {"OPEN", "ACTIVE"}}
            ineligible = min(env.tools.db.users.data.keys() - with_checking)
        for i, action in enumerate(task.evaluation_criteria.actions):
            args = deepcopy(action.arguments)
            if action.name == DISPATCH and args.get("agent_tool_name") == OPEN:
                if variant == "omit_openings":
                    continue
                params = _object(args["arguments"])
                if variant == "invalid_account_type":
                    args["arguments"] = json.dumps(dict(params, account_type="invalid_type"))
                elif variant == "ineligible_savings":
                    args["arguments"] = json.dumps(dict(params, user_id=ineligible, account_type="savings"))
                elif variant == "wrong_product":
                    args["arguments"] = json.dumps(dict(params, account_class="Unrequested control product"))
            call = ToolCall(id=f"control-{i}", name=action.name, arguments=args, requestor=action.requestor)
            actor = AssistantMessage if call.requestor == "assistant" else UserMessage
            message = actor(role=call.requestor, tool_calls=[call])
            messages.extend([message, env.get_response(call)])
        public = [m.model_dump(mode="json") for m in messages]
        receipts = opening_receipts(public)
        new_rows = verify_account_rows(before_ids, env.tools.db.accounts.data, receipts)
        replay = observed_accounts(task, public, env.get_db_hash())
        require(replay["receipts"] == receipts and replay["new_account_rows"] == new_rows,
                "Native replay and controlled effects differ")
        grade = EnvironmentEvaluator.calculate_reward(get_environment, task, messages,
                                                     env_kwargs=dict(task=task, retrieval_variant="grep_only"))
        result[variant] = dict(official_db_success=grade.db_check.db_match, new_account_rows=new_rows,
                               effects=account_effects(targets, receipts), receipts=receipts)
    require(result["reference"]["official_db_success"] and result["omit_openings"]["effects"]["opening_attempts"] == 0
            and result["invalid_account_type"]["effects"]["refused_or_invalid"] == len(targets)
            and result["invalid_account_type"]["new_account_rows"] == 0
            and all(r["outcome"] == "eligibility_rejected" for r in result["ineligible_savings"]["receipts"])
            and result["ineligible_savings"]["new_account_rows"] == 0
            and result["wrong_product"]["effects"]["distinct_targets_opened"] == 0
            and result["wrong_product"]["effects"]["other_accounts_opened"] > 0, "Native account controls changed")
    return result


def observed_accounts(task, messages, final_db_hash):
    """Replay recorded public business calls through the native DB and verify the sealed final state."""
    from pydantic import TypeAdapter
    from tau2.data_model.message import Message

    env = _environment(task)
    before = set(env.tools.db.accounts.data)
    start = task.initial_state
    env.set_state(initialization_data=start.initialization_data if start else None,
                  initialization_actions=start.initialization_actions if start else None,
                  message_history=TypeAdapter(list[Message]).validate_python(messages), strict=True)
    require(env.get_db_hash() == final_db_hash, "Replayed DB differs from the archived native final hash")
    receipts = opening_receipts(messages)
    return dict(receipts=receipts, new_account_rows=verify_account_rows(before, env.tools.db.accounts.data, receipts),
                final_db_hash=final_db_hash, effects=account_effects(target_accounts(task), receipts))
