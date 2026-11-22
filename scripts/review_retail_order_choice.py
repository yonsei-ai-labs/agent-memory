"""Check the post-hoc order-position and small-sample results from the 140 original model responses."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from fractions import Fraction
from itertools import combinations, product
from math import comb, sqrt
from pathlib import Path
from statistics import mean, stdev

from agent_memory_eval.contracts.core import digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from scripts.analyze_full_retail import require
from scripts.audit_retail_order_choice import publish


def review(root):
    dest = root / "runs/tau2-retail-order-choice-replay-01"
    registered = read_json((dest / "registration.json").read_bytes())
    profile = registered["profile"]
    chosen = read_json(asset_text(root, profile["diagnostic"]).encode())
    db = read_json(asset_text(root, chosen["inputs"]["db"]).encode())
    complete = read_json((dest / "completion.json").read_bytes())
    require(digest((dest / "registration.json").read_bytes()) == complete["registration_sha256"],
            "Registration changed")
    positions, actions, tasks = defaultdict(Counter), defaultdict(Counter), defaultdict(set)
    wire_scores, pairs, later = defaultdict(list), defaultdict(dict), defaultdict(dict)
    total, generations = Decimal(0), set()
    for s in registered["slots"]:
        arm, task = s["arm"], s["task"]
        folder = dest / f"slot-{s['slot']:03}"
        require(digest((folder / "log.jsonl").read_bytes()) == complete["journals"][str(s["slot"])],
                "Slot journal changed")
        scan, _, _ = read_experiment(folder)
        events = list(scan["events"].values())
        blobs = BlobStore(folder)
        (start,) = [e["payload"] for e in events if e["type"] == "model.request_started"]
        (end,) = [e["payload"] for e in events if e["type"] == "model.request_finished"]
        require(start["request_ref"]["sha256"] == s["initial_sha256"] and start["request_id"] == end["request_id"]
                and end["status"] == "response", "Unexpected submitted input or response")
        wire, raw = blobs.get(start["request_ref"]), blobs.get(end["response_ref"])
        require(raw["id"] not in generations, "Duplicated provider response")
        generations.add(raw["id"])
        total += Decimal(str(raw["usage"]["cost"]))
        orders = read_json(wire["messages"][-1]["content"].encode())["orders"]
        products = set(chosen["task_review"][task]["products"])
        (target,) = [o for o in orders if products <= {i["name"] for i in db["orders"][o]["items"]}]
        calls = [c["function"] for c in raw["choices"][0]["message"].get("tool_calls") or []]
        lookup = [read_json(c["arguments"].encode())["order_id"] for c in calls if c["name"] == "get_order_details"]
        focused = int(lookup == [target] and len(calls) == 1)
        group = "first" if target == orders[0] else "later"
        tasks[group].add(task)
        positions[group, arm].update(decisions=1, focused=focused)
        actions[arm].update(order_lookups=bool(lookup), started_at_first=bool(lookup) and lookup[0] == orders[0],
                            memory_calls=sum(c["name"] in {"memory_list", "memory_get"} for c in calls))
        wire_scores[s["initial_sha256"]].append(focused)
        pairs[s["customer"], task, s["source"], s["repeat"]][arm] = focused
        if group == "later" and arm in {"preload", "empty_preload"}:
            later[task].setdefault(arm, []).append(focused)
    require(len(generations) == complete["submitted"] == 140 and total == Decimal(complete["confirmed_usd"]),
            "Provider counts or charges differ")

    edges, by_task, by_customer = defaultdict(list), defaultdict(list), defaultdict(list)
    for (customer, task, source, _), arms in pairs.items():
        edges[customer, task, source].append(arms["preload"] - arms["empty_preload"])
    for (customer, task, _), repeats in edges.items():
        by_task[customer, task].append(mean(repeats))
    for (customer, _), sources in by_task.items():
        by_customer[customer].append(mean(sources))
    effects = sorted(mean(values) for values in by_customer.values())
    n, pos, neg = len(effects), sum(e > 0 for e in effects), sum(e < 0 for e in effects)
    sign_p = min(1, 2 * sum(comb(pos+neg, i) for i in range(min(pos, neg)+1)) / 2**(pos+neg))
    require(n == 7, "The t critical value is specific to these seven customers")
    margin = 2.446911851145 * stdev(effects) / sqrt(n)  # t(0.975, 6)
    comparisons = [a != b for runs in wire_scores.values() for a, b in combinations(runs, 2)]

    # Within each later-target task reassign the four outcomes to two slots per arm.
    # Hold the sum fixed and count the two tails of the observed difference.
    differences, observed = [], 0
    for arms in later.values():
        left, right = arms["preload"], arms["empty_preload"]
        require(len(left) == len(right) == 2, "Post-hoc within-task permutation block changed")
        pooled = left + right
        observed += sum(left) - sum(right)
        differences.append([2*sum(pick)-sum(pooled) for pick in combinations(pooled, 2)])
    draws = [sum(d) for d in product(*differences)]
    exact = Fraction(sum(abs(d) >= abs(observed) for d in draws), len(draws))

    reserved = read_json((root / "artifacts/analyses/20261122-retail-order-pairs.json").read_bytes())
    profiles = {digest(uid.encode()): u["orders"] for uid, u in db["users"].items()}
    future = defaultdict(Counter)
    for customer in reserved["reserved"]:
        orders = profiles[customer["customer"]]
        for case in customer["cases"]:
            future[case["mode"]]["first" if case["target"] == orders[0] else "later"] += 1
    return dict(study=profile["name"], registration_sha256=complete["registration_sha256"],
                new_model_requests=0, verified_generations=len(generations), confirmed_usd=str(total),
                positions={g: dict(tasks=sorted(tasks[g], key=int),
                                  arms={a: positions[g, a] for a in profile["arms"]}) for g in ("first", "later")},
                lookups_by_arm=dict(actions), customer_differences=effects, exact_sign_p=sign_p,
                t_interval_df6=[mean(effects)-margin, mean(effects)+margin],
                bootstrap_zero_probability=((n-pos-neg)/n)**n,
                identical_inputs=dict(distinct=len(wire_scores), all_pairs=len(comparisons),
                                      discordant_on_focused_lookup=sum(comparisons)),
                later_task_permutation=dict(two_sided=str(exact), assignments=len(draws)),
                reserved_target_position=dict(future))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    publish(parser.parse_args().output, review(ROOT))
