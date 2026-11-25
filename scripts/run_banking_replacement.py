"""Check the native DB scorer and run a registered paired banking case."""

import argparse
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.recording.assets import asset_text
from scripts import run_banking_policy_cycle as cycle

ROOT = cycle.ROOT
PROFILE = "protocols/pilots/tau-banking-replacement-v1.json"
PREFLIGHT = "artifacts/pilots/20261125-banking-replacement-preflight.json"
APPROVAL = "artifacts/pilots/20261125-banking-replacement-authorization.json"


def prepare(workspace):
    spec = cycle.read(ROOT / PROFILE)
    _, base, conn, _, _, cohort = cycle.bank.prepare(workspace)
    require(spec["banking_profile"]["path"] == cycle.bank.PROFILE, "Wrong native task protocol")
    asset_text(ROOT, spec["banking_profile"])
    strict = read_json(asset_text(ROOT, spec["required_writer"]).encode())
    require(
        strict["common_instruction_ref"] == spec["base_instruction"]
        and spec["task"] not in spec["previous_cases"],
        "Wrong original writer or reused task",
    )
    old = asset_text(ROOT, spec["base_instruction"]).rstrip("\r\n")
    new = asset_text(ROOT, spec["candidate_instruction"]).rstrip("\r\n")
    require(new.startswith(old + "\n"), "Different base writer")
    from tau2.domains.banking_knowledge.environment import get_tasks

    (task,) = [t for t in get_tasks() if t.id == spec["task"]]
    require(
        task.evaluation_criteria.reward_basis == ["DB"] and not task.evaluation_criteria.nl_assertions,
        "This case requires deterministic native DB scoring",
    )
    cfg = cycle.bank.required_design(base, strict)
    modules = cycle.bank.matched.modules_for(workspace)
    case, native = cycle.bank.native_case(cfg, conn, modules, task, spec["case_type"])
    case["reader_modes"] = [spec["reader"]]
    endpoints = read_json(asset_text(ROOT, cfg["endpoints"]).encode())["models"]
    roles = ("worker", "user_simulator")
    cfg.update(
        writer_feedback_level=spec["feedback"],
        max_concurrent=2,
        record_unfunded_shards=True,
        expected_endpoint_terms={r: cfg["endpoint_terms"][r] for r in roles},
        expected_model_revisions={r: endpoints[r]["canonical_slug"] for r in roles},
    )
    return spec, cfg, conn, case, native, cohort["corpus_sha256"]


def order_fields(order, expected):
    """Only material requested fields, independent of tool JSON formatting and order IDs."""
    return {k: order.get(k) == v for k, v in expected.items()}


def scorer_check(task):
    from tau2.data_model.message import AssistantMessage, ToolCall
    from tau2.domains.banking_knowledge.environment import get_environment
    from tau2.evaluator.evaluator_env import EnvironmentEvaluator

    kwargs = {"retrieval_variant": "grep_only", "task": task}
    verification, unlock, place = task.evaluation_criteria.actions
    require(
        [a.name for a in (verification, unlock, place)]
        == ["log_verification", "unlock_discoverable_agent_tool", "call_discoverable_agent_tool"],
        "Unexpected reference workflow",
    )
    expected = read_json(place.arguments["arguments"].encode())
    env = get_environment(**kwargs)
    cards = env.tools.db.credit_card_accounts.data
    other = next(
        k
        for k in sorted(cards)
        if k != expected["credit_card_account_id"] and cards[k]["user_id"] == expected["user_id"]
    )
    wrong_card = {"credit_card_account_id": other}
    variants = {
        "reference": [{}],
        "no_order": [],
        "wrong_card": [wrong_card],
        "home_instead_of_work": [{"shipping_address": verification.arguments["address"]}],
        "standard_shipping": [{"expedited_shipping": False}],
        "wrong_reason": [{"reason": "lost"}],
        "additional_wrong_card": [{}, wrong_card],
    }
    results = {}
    for label, orders in variants.items():
        env = get_environment(**kwargs)
        initial = task.initial_state
        env.set_state(
            initialization_data=initial.initialization_data,
            initialization_actions=initial.initialization_actions,
            message_history=[],
        )
        db = env.tools.db
        original_orders = set(db.credit_card_orders.data)
        statuses = {k: v.get("status") for k, v in db.credit_card_accounts.data.items()}
        calls = [(verification.name, verification.arguments), (unlock.name, unlock.arguments)]
        for change in orders:
            calls.append(
                (place.name, dict(place.arguments, arguments=canonical({**expected, **change}).decode()))
            )
        messages = []
        for index, (name, args) in enumerate(calls):
            call = ToolCall(
                id=f"offline-{index}", name=name, arguments=deepcopy(args), requestor="assistant"
            )
            response = env.get_response(call)
            require(not response.error, f"Native tool error in {label}: {name}")
            messages.extend([AssistantMessage(role="assistant", tool_calls=[call]), response])
        new_orders = [v for k, v in db.credit_card_orders.data.items() if k not in original_orders]
        closed = sorted(
            k
            for k, v in db.credit_card_accounts.data.items()
            if statuses[k] != "CLOSED" and v.get("status") == "CLOSED"
        )
        grade = EnvironmentEvaluator.calculate_reward(get_environment, task, messages, env_kwargs=kwargs)
        results[label] = {
            "db_match": grade.db_check.db_match,
            "new_orders": len(new_orders),
            "fields": [order_fields(o, expected) for o in new_orders],
            "target_closed": expected["credit_card_account_id"] in closed,
            "other_cards_closed": len(set(closed) - {expected["credit_card_account_id"]}),
        }
        require(
            results[label]["db_match"] is (label == "reference") and len(new_orders) == len(orders),
            f"Native DB scorer or accepted order count did not discriminate {label}",
        )
    return results


def preflight(workspace):
    spec, cfg, conn, case, native, corpus = prepare(workspace)
    from tau2.data_model.tasks import Task

    confirm = cycle.bank.native_case(
        cfg, conn, cycle.bank.matched.modules_for(workspace), Task.model_validate(native), spec["case_type"]
    )[0]
    require(all(case[k] == v for k, v in confirm.items()), "Native start is not reproducible")
    schedules = {}
    for role in spec["validation"]["writer_policies"]:
        cell = dict(case, task=case["task"] + "__" + role)
        schedules[role] = {s: cycle.bank.matched.jobs_for(cell, s) for s in ("write", "solve")}
    for stage in ("write", "solve"):
        require(
            sum(len(c[stage]) for c in schedules.values()) == spec["validation"][stage],
            "Unexpected schedule",
        )
    return {
        "protocol_sha256": digest((ROOT / PROFILE).read_bytes()),
        "case": case,
        "corpus_sha256": corpus,
        "schedules": schedules,
        "source_schedule": cycle.bank.matched.jobs_for(case, "source"),
        "scorer_checks": scorer_check(Task.model_validate(native)),
        "additional_model_requests": 0,
    }


def run(workspace, phase):
    spec, cfg, conn, case, native, _ = prepare(workspace)
    check = preflight(workspace)
    require(check == cycle.read(ROOT / PREFLIGHT), "Published preflight differs")
    approval = cycle.read(ROOT / APPROVAL)
    require(
        approval["preflight_sha256"] == digest(canonical(check))
        and approval["protocol_sha256"] == check["protocol_sha256"]
        and approval["maximum_usd"] == spec["additional_limit_usd"]
        and approval["assigned"] == {"source": 1, "write": 10, "solve": 10, "second_pass_no_memory": 0}
        and approval["user_authorization"],
        "No matching new case allocation",
    )
    cfg["total_usd_limit"] = approval["maximum_usd"]
    common = {
        "protocol_sha256": check["protocol_sha256"],
        "preflight_sha256": approval["preflight_sha256"],
        "approval": approval,
    }
    return cycle.run_paired_validation(
        workspace, phase, spec, spec, Decimal(0), cfg, conn, [case], {case["task"]: native}, common
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "sources", "validation"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if args.phase == "preflight":
        require(args.output is not None, "Specify where to publish the preflight")
        cycle.save(args.output, preflight(workspace))
    else:
        done = run(workspace, args.phase)
        print(done["total_covered_usd"], flush=True)
