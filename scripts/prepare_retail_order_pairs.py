"""Reserve unseen retail customers and execute the native reference paths of paired task variants."""

import argparse
from collections import Counter
from pathlib import Path

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.assets import asset_text
from scripts.analyze_full_retail import require
from scripts.audit_retail_order_choice import PROFILE, publish
from scripts.replay_full_retail_fields import reference_state
from scripts.run_full_retail import modules_for, owner_ids


def hash_value(value):
    return digest(canonical(value))


def official_customers(tasks, db, fallback):
    owners, used_fallback = set(), set()
    for task in tasks:
        found = owner_ids(task, db)
        if not found:
            uid = fallback[task["id"]]
            info = task["user_scenario"]["instructions"]["known_info"].casefold()
            candidates = []
            for name, user in db["users"].items():
                full_name = (user["name"]["first_name"] + " " + user["name"]["last_name"]).casefold()
                if name.casefold() in info or (full_name in info and user["address"]["zip"] in info):
                    candidates.append(name)
            require(candidates == [uid], "Could not uniquely verify scenario-only owner")
            found = {digest(uid.encode())}
            used_fallback.add(task["id"])
        require(len(found) == 1, "Official task has ambiguous ownership")
        owners.update(found)
    require(used_fallback == set(fallback), "Unnecessary fallback identity")
    return owners


def public_prefix(opening, user, profile):
    def tool_turn(name, arguments, result):
        call_id = "call_" + name
        return [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": call_id, "type": "function", "function": {
                    "name": name, "arguments": canonical(arguments).decode()}}]},
            {"role": "tool", "tool_call_id": call_id, "content": result},
        ]

    return [
        {"role": "user", "content": opening},
        {"role": "assistant", "content": "What is the email address on your account?"},
        {"role": "user", "content": user["email"]},
        *tool_turn("find_user_id_by_email", {"email": user["email"]}, user["user_id"]),
        *tool_turn("get_user_details", {"user_id": user["user_id"]}, canonical(profile).decode()),
    ]


def native_task(task_id, opening, user, order, *, previous_visit):
    from tau2.data_model.tasks import Task

    def action(name, **arguments):
        return dict(action_id=name, name=name, arguments=arguments)

    oid = order["order_id"]
    actions = [action("find_user_id_by_email", email=user["email"]),
               action("get_user_details", user_id=user["user_id"]),
               action("get_order_details", order_id=oid)]
    if previous_visit:
        instructions = "Ask only for information. Do not request or approve a change."
        communicate = sorted({i["name"] for i in order["items"]})
        reward = ["DB", "COMMUNICATE"]
    else:
        instructions = (
            "Give the order ID if the assistant asks which order you mean. "
            "Confirm only after the assistant presents the correct order and cancellation details. "
            "Correct a wrong order and never approve cancellation of another order."
        )
        actions.append(action("cancel_pending_order", order_id=oid, reason="no longer needed"))
        communicate, reward = [], ["DB"]
    task = Task.model_validate(dict(
        id=task_id,
        user_scenario=dict(instructions=dict(domain="retail", reason_for_call=opening,
            task_instructions="Start with the supplied reason for calling, verbatim. " + instructions,
            known_info=f"Your email is {user['email']}. The order you mean is {oid}.",
            unknown_info="")),
        evaluation_criteria=dict(actions=actions, communicate_info=communicate, reward_basis=reward),
    ))
    return task.model_dump(mode="json", exclude_none=True)


def current_cases(cfg, user, orders, profile):
    """All six cases are allocated before any source writer or target solver is observed."""
    customer = digest(user["user_id"].encode())
    tasks, prefixes, index = {}, {}, []
    order_ids = tuple(orders)
    for history, other in (order_ids, order_ids[::-1]):
        first_id = "derived-" + hash_value([customer, "source", history])[:24]
        prior = cfg["prior_request"].format(order_id=history)
        tasks[first_id] = native_task(first_id, prior, user, orders[history], previous_visit=True)
        for mode, target in (("previous_only", history), ("explicit_same", history), ("explicit_other", other)):
            opening = cfg["current_requests"]["previous_only"] if mode == "previous_only" else (
                cfg["current_requests"]["current_id"].format(order_id=target))
            task_id = "derived-" + hash_value([customer, history, mode])[:24]
            tasks[task_id] = native_task(task_id, opening, user, orders[target], previous_visit=False)
            prefix = public_prefix(opening, user, profile)
            prefix_sha = hash_value(prefix)
            prefixes[prefix_sha] = prefix
            index.append(dict(history=history, mode=mode, target=target, source=first_id,
                              task=task_id, public_prefix_sha256=prefix_sha))
    implicit = [r for r in index if r["mode"] == "previous_only"]
    require(implicit[0]["public_prefix_sha256"] == implicit[1]["public_prefix_sha256"]
            and implicit[0]["target"] != implicit[1]["target"], "Past-only histories are distinguishable")
    for oid in orders:
        stated = [r for r in index if r["mode"] != "previous_only" and r["target"] == oid]
        require(len(stated) == 2 and stated[0]["public_prefix_sha256"] == stated[1]["public_prefix_sha256"]
                and stated[0]["history"] != stated[1]["history"], "Current request differs with history")
    require(len(prefixes) == 3 and len(tasks) == 8, "Invalid paired case inventory")
    return tasks, prefixes, index


def prepare(root, cfg):
    from tau2.domains.retail.data_model import RetailDB
    from tau2.domains.retail.environment import get_environment

    db = read_json(asset_text(root, cfg["inputs"]["db"]).encode())
    native = get_environment(db=RetailDB.model_validate(db))
    initial = native.get_db_hash()
    tasks = read_json(asset_text(root, cfg["inputs"]["tasks"]).encode())
    rules = cfg["reserve"]
    official = official_customers(tasks, db, rules["unlinked_scenarios"])
    catalog, specs, public = [], {}, {}
    for uid, user in sorted(db["users"].items()):
        customer = digest(uid.encode())
        if customer in official:
            continue
        pending = [o for o in user["orders"] if db["orders"][o]["status"] == "pending"]
        if len(pending) < 2:
            continue
        picked = sorted(pending, key=lambda o: hash_value([rules["salt"], customer, o]))[:2]
        require(native.make_tool_call("find_user_id_by_email", email=user["email"]) == uid,
                "Native authentication failed")
        profile = native.make_tool_call("get_user_details", user_id=uid).model_dump(mode="json")
        require(profile["orders"] == user["orders"], "Unexpected order list")
        order_data = {}
        for oid in picked:
            order = native.make_tool_call("get_order_details", order_id=oid)
            require(order.user_id == uid and order.status == "pending" and order.items, "Ineligible order")
            order_data[oid] = order.model_dump(mode="json")
        cases, prefixes, index = current_cases(rules, user, order_data, profile)
        end_states = {}
        for oid in picked:
            src = next(r["source"] for r in index if r["history"] == oid)
            references = [cases[r["task"]]["evaluation_criteria"]["actions"] for r in index if r["target"] == oid]
            require(len(references) == 3 and all(actions == references[0] for actions in references),
                    "Current cases do not share the same reference for a target order")
            fresh = get_environment(db=native.tools.db, policy=native.policy)
            state, errors = reference_state(fresh, cases[src]["evaluation_criteria"]["actions"])
            require(not errors and state[0] == initial, "A source read changed the DB")
            state, errors = reference_state(fresh, references[0])
            require(not errors and state[0] != initial, "No-op and cancellation were indistinguishable")
            after = fresh.tools.db
            require(after.orders[oid].status == "cancelled" and after.orders[oid].cancel_reason == "no longer needed"
                    and all(v == native.tools.db.orders[k] for k, v in after.orders.items() if k != oid)
                    and all(v == native.tools.db.users[k] for k, v in after.users.items() if k != uid)
                    and after.products == native.tools.db.products, "Unexpected cancellation side effects")
            end_states[oid] = state[0]
        require(len(set(end_states.values())) == 2, "Wrong-order cancellation would pass")
        require(not (specs.keys() & cases.keys() or public.keys() & prefixes.keys()), "Customer overlap")
        specs.update(cases)
        public.update(prefixes)
        catalog.append(dict(customer=customer, pending_candidates=len(pending), order_pair=picked,
                            expected_db=end_states, cases=index))
    require(native.get_db_hash() == initial, "Shared initial state was mutated")
    catalog.sort(key=lambda r: hash_value([rules["salt"], r["customer"]]))
    summary = dict(official_tasks=len(tasks), official_customers=len(official), db_customers=len(db["users"]),
                   reserved_customers=len(catalog), source_tasks=2*len(catalog), target_cases=6*len(catalog),
                   verified_distinct_cancellations=2*len(catalog), public_prefixes=len(public),
                   pending_order_counts=dict(sorted(Counter(r["pending_candidates"] for r in catalog).items())))
    return dict(summary=summary, initial_db=initial, native_tasks_sha256=hash_value(specs),
                public_prefixes_sha256=hash_value(public), reserved=catalog), specs, public


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--materialize", type=Path, help="New folder for private native tasks and public prefixes")
    args = parser.parse_args()
    cfg = read_json((ROOT / PROFILE).read_bytes())
    modules_for(ROOT)
    result, specs, public = prepare(ROOT, cfg)
    if args.materialize:
        args.materialize.mkdir(exist_ok=False)
        publish(args.materialize / "native-tasks-analyst-and-simulator.json", specs)
        publish(args.materialize / "public-prefixes.json", public)
    publish(args.output, dict(protocol_sha256=digest((ROOT / PROFILE).read_bytes()),
                              model_requests_sent=0, **result))
    print(result["summary"])
