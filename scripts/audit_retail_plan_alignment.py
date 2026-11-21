"""Audit the frozen retail treatments and the multi-item order tool using recorded data only."""

import argparse
from collections import Counter, defaultdict
from functools import cache
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.memory.context import snapshot_message
from agent_memory_eval.recording.journal import safe_path
from scripts.analyze_full_retail import require
from scripts.analyze_recheck_rounds import MUTATIONS
from scripts.analyze_retail_full_factors import business_events, verified
from scripts.analyze_retail_transfer_outcome_causes import FROZEN, native_reader
from scripts.audit_retail_transfer_writing import role_calls
from scripts.replay_full_retail_fields import reference_state, state_hashes
from scripts.run_full_retail import modules_for
from scripts.run_recheck_rounds import fixed

FILES = {
    **FROZEN,
    "pools": (
        "artifacts/analyses/20261120-retail-candidate-pools-results.json",
        "1c49fcc3657fe513385f933070981aaf0c6b13db9ccd0179269462af28561d9f",
    ),
    "first": (
        "artifacts/analyses/20261119-retail-full-cells.json",
        "34d877c4d778cee4a39490a640ab11dcd9455ec991df44788fc29f13663907e2",
    ),
}
MULTI = "modify_pending_order_items"


def wire_audit(workspace, results, pools, donors):
    counts = defaultdict(Counter)
    items = {(w["source"], w["arm"]): w["items"] for w in results["writers"]}
    for w in results["writers"]:
        calls = list(role_calls(workspace, w, "writer"))
        require(calls, "Missing writer input")
        for _, wire, _, _ in calls:
            require(
                digest(wire["messages"][1]["content"].encode())
                == donors[w["source"]]["public_context_sha256"]
                and read_json(wire["messages"][2]["content"])
                == {"kind": "evaluation_feedback", "level": "none"}
                and [t["function"]["name"] for t in wire["tools"]] == ["memory_store"]
                and wire["tool_choice"] == "auto",
                "Unexpected writer information or tools",
            )
        counts["writer"]["sessions"] += 1
        counts["writer"]["requests"] += len(calls)
    envelopes = defaultdict(set)
    for study, cells in (("pools", pools["cells"]), ("retest", results["solves"])):
        for cell in cells:
            enabled = cell["arm"] != "no_memory"
            optional = enabled and study == "pools"
            preload = enabled and study == "retest"
            count = counts[study + ("_on_demand" if optional else "_preload" if preload else "_pure")]
            count["assigned"] += 1
            payload = (
                snapshot_message(items[cell["source"], cell["arm"]] if cell.get("source") else [])
                if preload
                else None
            )
            for i, (_, wire, reply, _) in enumerate(role_calls(workspace, cell, "worker")):
                names = {
                    t["function"]["name"]
                    for t in wire["tools"]
                    if t["function"]["name"].startswith("memory_")
                }
                messages = wire["messages"]
                native, marker, _ = messages[0]["content"].partition("\n<memory_policy>\n")
                require(
                    wire["tool_choice"] == "auto"
                    and names == ({"memory_list", "memory_get"} if optional else set())
                    and bool(marker) == enabled
                    and (not preload or messages[1] == payload),
                    "Unexpected solver interface or snapshot",
                )
                count["requests"] += 1
                count["memory_tool_calls"] += sum(
                    c["function"]["name"].startswith("memory_")
                    for c in ((reply or {}).get("message", {}).get("tool_calls") or [])
                )
                if i == 0:
                    count["sessions_with_worker_request"] += 1
                    require(
                        messages[-1]["role"] == "user" and len(messages) == 3 + int(preload),
                        "Unexpected opening",
                    )
                    # Preserve the native greeting, system, model, route, seed and business tools.
                    # Remove exactly the assigned interface and the independently generated first utterance.
                    shared = dict(
                        wire,
                        messages=[dict(messages[0], content=native), messages[-2]],
                        tools=[
                            t for t in wire["tools"] if not t["function"]["name"].startswith("memory_")
                        ],
                    )
                    envelopes[study, cell["task"]].add(digest(canonical(shared)))
    require(all(len(v) == 1 for v in envelopes.values()), "Additional opening differences between arms")
    return {
        "by_condition": counts,
        "matching_opening_templates_by_study": dict(Counter(k[0] for k in envelopes)),
    }


def opportunities(env, targets, tasks, retest, previous):
    found = {}
    for row in targets:
        tid = row["task"]
        actions = [
            a["arguments"]
            for a in tasks[tid]["evaluation_criteria"]["actions"]
            if a["name"] in MUTATIONS
        ]
        order_ids = sorted({a["order_id"] for a in actions if "order_id" in a})
        (owner,) = {env.tools.db.orders[o].user_id for o in order_ids}
        require(digest(owner.encode()) == row["customer"], "Order owner differs from allocation")
        profile = env.make_tool_call("get_user_details", user_id=owner)
        require(
            set(order_ids) <= set(profile.orders), "A required order is absent from the current profile"
        )
        variants = []
        for a in actions:
            if "new_item_ids" not in a:
                continue
            order = env.make_tool_call("get_order_details", order_id=a["order_id"])
            for old, new in zip(a["item_ids"], a["new_item_ids"], strict=True):
                product_id = next(i.product_id for i in order.items if i.item_id == old)
                product = env.make_tool_call("get_product_details", product_id=product_id)
                require(
                    product.variants[new].available, "A required replacement is not currently available"
                )
                variants.append(new)
        entry = {
            "required_orders_on_current_profile": order_ids,
            "available_reference_replacements": variants,
        }
        for name, cells in (("retest", retest), ("previous", previous)):
            entry[name] = {
                arm: [c["official"] for c in cells if c["task"] == tid and c["arm"] == arm]
                for arm in ("no_memory", "empty_preload")
            }
        found[tid] = entry
    return found


def affected(call, args):
    return call == MULTI and len(args["item_ids"]) > 1


def native_calls(report):
    changes, _ = business_events(report["messages"])
    return [
        (w["name"], w["args"], w["success"], report["messages"][w["returned"]]["content"])
        for w in changes
        if w["accepted"]
    ]


def replay(env, actions, *, correct):
    """Play the same order operations; optionally restore per-line catalog values after a successful update."""
    from tau2.data_model.message import ToolCall

    evidence = []
    for name, args, success, historical in actions:
        multi = affected(name, args)
        if multi:
            old = [i.item_id for i in env.tools.db.orders[args["order_id"]].items]
        reply = env.get_response(ToolCall(name=name, requestor="assistant", arguments=args))
        require(bool(reply.error) != success, "Recorded operation status changed during replay")
        if not multi or not success:
            continue
        returned = read_json(reply.content)
        if historical is not None and not correct:
            require(
                returned == read_json(historical), "Multi-item reply differs from the archived return"
            )
        order = env.tools.db.orders[args["order_id"]]
        changed = [i for i, line in enumerate(order.items) if line.item_id != old[i]]
        require(
            Counter(order.items[i].item_id for i in changed) == Counter(args["new_item_ids"]),
            "Ambiguous changed lines",
        )
        differences = []
        for i in changed:
            line = order.items[i]
            variant = env.tools.db.products[line.product_id].variants[line.item_id]
            if line.price == variant.price and line.options == variant.options:
                continue
            differences.append(
                {
                    "item_id": line.item_id,
                    "returned": {"price": line.price, "options": dict(line.options)},
                    "catalog": {"price": variant.price, "options": dict(variant.options)},
                }
            )
            if correct:
                line.price, line.options = variant.price, dict(variant.options)
        evidence.append({"arguments": args, "discrepancies": differences})
    return list(state_hashes(env)), evidence


def benchmark_audit(workspace, data, make):
    history = data["sources"]["database_replay"]
    tasks = {t["id"]: t for t in data["tasks"]}
    groups = {"previous": data["previous"]["cells"], "retest": data["results"]["solves"]}
    bank_sources = data["sources"]["donors"]
    first = {
        c["task"]: c
        for c in data["first"]["cells"]
        if c["stage"] == "source" and c["task"] in bank_sources
    }
    native = native_reader(workspace, {c["run"]: c for group in groups.values() for c in group})
    refs = {}
    for task in history["reference"]:
        require(tasks[task]["initial_state"] is None, "Unexpected task-specific initial state")
        expected_failures = {e["call_index"] for e in history["reference"][task]["errors"]}
        refs[task] = [
            (a["name"], a["arguments"], i not in expected_failures, None)
            for i, a in enumerate(tasks[task]["evaluation_criteria"]["actions"])
        ]
    ref_affected = {t for t, acts in refs.items() if any(affected(n, a) for n, a, _, _ in acts)}
    touched = ref_affected | {
        t
        for t, s in history["observed"].items()
        if any(affected(a["name"], a["arguments"]) for a in s["executed"])
    }
    for cells in groups.values():
        touched.update(
            c["task"]
            for c in cells
            if any(w["success"] and affected(w["name"], w["args"]) for w in c.get("writes", []))
        )

    @cache
    def reference(task, correct):
        result, differences = replay(make(), refs[task], correct=correct)
        if not correct:
            official, errors = reference_state(make(), tasks[task]["evaluation_criteria"]["actions"])
            require(
                result == list(official) == history["reference"][task]["end_db"]
                and errors == history["reference"][task]["errors"],
                f"Official reference changed for task {task}",
            )
        return result, differences

    def evaluate(task, cell, report):
        actions = native_calls(report)
        official_db, findings = replay(make(), actions, correct=False)
        adjusted_db, _ = replay(make(), actions, correct=True)
        original = official_db == reference(task, False)[0]
        adjusted = adjusted_db == reference(task, True)[0]
        require(original is cell["grade"]["db_match"], "Original database grade changed")
        return {
            "task": task,
            "run": cell["run"],
            "arm": cell["arm"],
            "official_success": cell["official"],
            "db_match": original,
            "db_match_with_per_item_catalog_values": adjusted,
            "multi_item_returns": findings,
        }

    rows = defaultdict(list)
    for task in sorted(touched & set(bank_sources), key=int):
        cell = first[task]
        require(cell["run"] == bank_sources[task]["run_id"], "Wrong source session")
        raw = verified(safe_path(workspace, cell["report"]), cell["report_sha256"])
        rows["source"].append(evaluate(task, cell, read_json(raw)))
    for name, cells in groups.items():
        for cell in cells:
            if cell["task"] in touched and cell["official"] is not None:
                rows[name].append(evaluate(cell["task"], cell, native(cell["run"])[0]))
    return {
        "reference": {
            t: {
                "native": reference(t, False)[0],
                "per_item_catalog": reference(t, True)[0],
                "multi_item_returns": reference(t, False)[1],
            }
            for t in sorted(touched, key=int)
        },
        "episodes": rows,
    }


def analyze(workspace):
    from agent_memory_eval.adapters.tau2 import PIN

    data = {name: read_json(verified(workspace / path, sha)) for name, (path, sha) in FILES.items()}
    modules_for(workspace)
    from tau2.registry import registry

    require(data["sources"]["benchmark_revision"] == PIN, "Unexpected benchmark version")
    make = registry.get_env_constructor("retail")
    r = data["results"]
    return {
        "benchmark": PIN,
        "inputs": {path: sha for path, sha in FILES.values()},
        "analysis_model_calls": 0,
        "wire": wire_audit(workspace, r, data["pools"], data["sources"]["donors"]),
        "opportunities": opportunities(
            make(),
            data["sources"]["targets"],
            {t["id"]: t for t in data["tasks"]},
            r["solves"],
            data["previous"]["cells"],
        ),
        "benchmark_audit": benchmark_audit(workspace, data, make),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with patch("socket.socket.connect", side_effect=AssertionError("Offline audit only")):
        result = analyze(args.workspace.resolve())
    fixed(args.output, result)
    print(
        result["wire"]["matching_opening_templates_by_study"],
        list(result["benchmark_audit"]["reference"]),
    )
