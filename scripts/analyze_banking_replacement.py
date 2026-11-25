"""Replay the native order DB and trace the evidence available before the order."""

import argparse
import re
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from scripts import analyze_banking_policy_cycle as scores
from scripts import run_banking_replacement as study

cycle = study.cycle
MERCHANTS = ("Electronics Express Miami", "GamerZone LA")


def expected_order(task):
    return read_json(task.evaluation_criteria.actions[-1].arguments["arguments"].encode())


def case_facts(workspace):
    spec, cfg, conn, case, data, _ = study.prepare(workspace)
    from tau2.data_model.tasks import Task
    from tau2.domains.banking_knowledge.environment import get_environment

    task = Task.model_validate(data)
    target = expected_order(task)
    env = get_environment(retrieval_variant="grep_only", task=task)
    user = target["user_id"]
    db = env.tools.db
    transactions = [r for r in db.credit_card_transaction_history.data.values() if r["user_id"] == user]
    returned = env.use_tool("get_credit_card_transactions_by_user", user_id=user)
    records = {
        key: (int(rank), block)
        for rank, key, block in re.findall(
            r"^(\d+)\. Record ID: (\S+)\n(.*?)(?=^\d+\. Record ID:|\Z)", returned, re.MULTILINE | re.DOTALL
        )
    }
    listed = []
    for merchant in MERCHANTS:
        (entry,) = [r for r in transactions if r["merchant_name"] == merchant]
        rank, block = records[entry["transaction_id"]]
        require(
            all(
                str(entry[k]) in block for k in ("merchant_name", "transaction_date", "transaction_amount")
            ),
            "Target not reachable in the real public transaction lookup",
        )
        listed.append(
            {
                "lookup_position": rank,
                **{k: entry[k] for k in ("merchant_name", "transaction_date", "transaction_amount")},
            }
        )
    bank = cycle.bank
    conf = bank.solve_connection(
        conn,
        bank.matched.design_for(cfg, case, "source"),
        bank.matched.jobs_for(case, "source")[0],
        cfg["episode_cap_usd"],
    )
    orch, _ = bank.build_baseline_orchestrator(conf, data)
    systems = (orch.agent.system_prompt, orch.user.system_prompt)
    obsolete_dates = ("11/04/2024", "11/06/2024")
    require(
        all(day in task.description.notes for day in obsolete_dates)
        and not any(day in prompt for day in obsolete_dates for prompt in systems),
        "The reference-only date discrepancy changed",
    )
    return {
        "task": spec["task"],
        "task_sha256": case["task_sha256"],
        "account_count": sum(c["user_id"] == user for c in db.credit_card_accounts.data.values()),
        "customer_transactions": len(transactions),
        "transactions_on_target_product": sum(
            t["credit_card_type"] == "Silver Rewards Card" for t in transactions
        ),
        "targets_in_native_lookup": listed,
        "returned_order": [
            t["merchant_name"] for t in sorted(listed, key=lambda r: returned.index(r["merchant_name"]))
        ],
        "native_lookup_sha256": digest(returned.encode()),
        "analyst_note_dates": list(obsolete_dates),
        "analyst_note_dates_in_role_prompts": False,
        "target_address_in_worker_initial_system": target["shipping_address"] in orch.agent.system_prompt,
        "source_or_reader_runs": 0,
        "model_requests": 0,
    }


def word_in(value, needle):
    return needle.casefold() in value.casefold()


def argument_object(value):
    if isinstance(value, str):
        try:
            value = read_json(value.encode())
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def audited_db(log, task, source_public, note):
    from pydantic import TypeAdapter
    from tau2.data_model.message import Message, MultiToolMessage, ToolMessage
    from tau2.domains.banking_knowledge.environment import get_environment
    from tau2.evaluator.evaluator_env import EnvironmentEvaluator

    (score,) = log.of_type("outcome.scored")
    score = score["payload"]
    native = log.blobs.get(score["official_artifact_ref"])
    messages = TypeAdapter(list[Message]).validate_python(native["messages"])
    receipts = {
        tool.id: tool
        for msg in messages
        for tool in (msg.tool_messages if isinstance(msg, MultiToolMessage) else [msg])
        if isinstance(tool, ToolMessage) and tool.requestor == "assistant"
    }
    kwargs = {"retrieval_variant": "grep_only", "task": task}
    grade = EnvironmentEvaluator.calculate_reward(get_environment, task, messages, env_kwargs=kwargs)
    require(
        grade.db_check.db_match is score["official_success"]
        and native["reward_info"]["db_check"]["db_match"] is grade.db_check.db_match,
        "Native DB replay disagrees with the sealed grade",
    )
    env = get_environment(**kwargs)
    setup = task.initial_state

    def reset(history):
        env.set_state(
            initialization_data=setup.initialization_data,
            initialization_actions=setup.initialization_actions,
            message_history=history,
        )

    reset([])
    db = env.tools.db
    original_orders = deepcopy(db.credit_card_orders.data)
    initial_status = {k: v.get("status") for k, v in db.credit_card_accounts.data.items()}
    reset(messages)
    db = env.tools.db
    orders = [v for k, v in db.credit_card_orders.data.items() if original_orders.get(k) != v]
    target = expected_order(task)
    closed = [
        k
        for k, v in db.credit_card_accounts.data.items()
        if v.get("status") == "CLOSED" and initial_status.get(k) != "CLOSED"
    ]
    replacement_tool = task.evaluation_criteria.actions[-1].arguments["agent_tool_name"]
    needles = {
        "replacement_tool": replacement_tool,
        "work_address": target["shipping_address"],
        "reason_code": target["reason"],
        "expedited": "expedited",
        "card_account": target["credit_card_account_id"],
        "first_merchant": MERCHANTS[0],
        "second_merchant": MERCHANTS[1],
    }
    source_and_note = {
        name: {"source_public": word_in(source_public, term), "in_stored_note": word_in(note, term)}
        for name, term in needles.items()
    }
    traces = []
    for req in (r for r in log.requests if r["actor_role"] == "worker"):
        if log.finishes[req["request_id"]]["status"] != "response":
            continue
        wire = log.blobs.get(req["request_ref"])
        m = log.reply(req)["message"]
        calls = m.get("tool_calls") or []
        for c in calls:
            if c["function"]["name"] != "call_discoverable_agent_tool":
                continue
            args = argument_object(c["function"]["arguments"])
            if not args or args.get("agent_tool_name") != replacement_tool:
                continue
            spans = log.visible(req, wire)
            require(
                not note or {s["field"] for s in spans} == {"title", "body"}, "Missing note in order input"
            )
            public = wire["messages"][2:] if note else wire["messages"][1:]
            current = cycle.input_evidence([wire["messages"][0], *public], wire.get("tools", []), {"grep"})
            attempted = argument_object(args.get("arguments"))
            receipt = receipts.get(c["id"])
            values = {}
            for name, term in needles.items():
                values[name] = {
                    "source_public": word_in(source_public, term),
                    "in_shown_note": bool(note) and word_in(note, term),
                    "current_origins": sorted({origin for origin, text in current if word_in(text, term)}),
                    "in_order_arguments": (
                        bool(attempted and attempted.get("expedited_shipping") is True)
                        if name == "expedited"
                        else word_in(canonical(args).decode(), term)
                    ),
                }
            traces.append(
                {
                    "request": req["request_id"],
                    "request_sha256": req["request_ref"]["sha256"],
                    "call_id": c["id"],
                    "requested_fields_match": study.order_fields(attempted, target)
                    if attempted is not None
                    else None,
                    "native_receipt": None
                    if receipt is None
                    else {
                        "execution_status": receipt.execution_status,
                        "error": receipt.error,
                        "has_order_id": "Order ID:" in (receipt.content or ""),
                        "content_sha256": digest((receipt.content or "").encode()),
                    },
                    "literal_support": values,
                }
            )
    fields = [study.order_fields(r, target) for r in orders]
    return {
        "official_db_success": grade.db_check.db_match,
        "new_or_changed_orders": len(orders),
        "exact_orders": sum(all(f.values()) for f in fields),
        "order_fields": fields,
        "target_card_closed": target["credit_card_account_id"] in closed,
        "other_cards_closed": len(set(closed) - {target["credit_card_account_id"]}),
        "source_and_stored_note_literal_support": source_and_note,
        "evidence_at_order": traces,
    }


def observed(workspace):
    spec, cfg, _, case, data, _ = study.prepare(workspace)
    from tau2.data_model.tasks import Task

    task = Task.model_validate(data)
    tid = case["task_id"]
    pre = cycle.read(study.ROOT / study.PREFLIGHT)
    approval = cycle.read(study.ROOT / study.APPROVAL)
    proposal = {k: spec[k] for k in ("base_instruction", "candidate_instruction")}
    result = scores.paired_validation_results(
        workspace,
        spec,
        proposal,
        start=Decimal(0),
        task_ids={tid},
        read_sha=cfg["delivery_instruction"]["sha256"],
        ceiling=spec["additional_limit_usd"],
        protocol_sha=pre["protocol_sha256"],
        binding={
            "protocol_sha256": pre["protocol_sha256"],
            "approval": approval,
            "preflight_sha256": digest(canonical(pre)),
        },
    )
    src_dir = workspace / "runs" / spec["source_study"] / ("task-" + tid) / "source"
    source = (
        cycle.bank.matched.source_from(src_dir, tid) if (src_dir / "completion.json").exists() else None
    )
    verified = {}
    for policy, rows in [("source", [result["sources"][tid]]), *result["trials"].items()]:
        folder = (
            src_dir
            if policy == "source"
            else workspace / "runs" / spec["validation_study"] / ("task-" + tid + "__" + policy) / "solve"
        )
        if not (folder / "completion.json").exists():
            continue
        assigned = {r.get("run_id"): r for r in cycle.bank.matched.sealed_rows(folder)[2]}
        for row in rows:
            if row["success"] is None:
                continue
            archived = assigned[row["run"]]
            log = RecordedDecisions(folder / archived["directory"], row["run"], row["log_sha256"])
            note = " ".join(i[k] for i in log.items for k in ("title", "body"))
            verified[row["run"]] = audited_db(log, task, source.public_context, note)
    result["replayed_orders"] = verified
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("case", "results"))
    parser.add_argument("--workspace", type=Path, default=study.ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    compute = case_facts if args.phase == "case" else observed
    cycle.save(args.output, compute(args.workspace.resolve()))
