"""Run the authorized fixed-state retail comparison using isolated v1.2 journals."""

import argparse
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.memory.context import memory_system_suffix, request_with_past_memory
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.models.openrouter import request_reservation_usd
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.runner import fixed_decision
from agent_memory_eval.runner.fixed_decision import prices
from scripts.analyze_full_retail import require
from scripts.analyze_retail_full_factors import verified
from scripts.audit_retail_order_choice import archived_banks, classify_reply, publish

PROFILE = "protocols/pilots/tau2-retail-order-choice-replay-v1.json"
ARMS = ("preload", "empty_preload", "query", "empty_query", "no_memory")
QUERY = {"query", "empty_query"}


def treatment(base, arm, bank, instructions):
    require(arm in ARMS, "Unknown delivery arm")
    mode = "on_demand" if arm in QUERY else "none" if arm == "no_memory" else "preload"
    policy = MemoryPolicy(mode != "none", "none", False, page_size=4, read_mode=mode,
                          max_items=4, max_payload_bytes=65536, max_return_bytes=131072)
    items = bank if arm in {"query", "preload"} else []
    if mode == "preload":
        wire = request_with_past_memory(base, policy, items, instruction=instructions["preload"],
                                        notice_version="domain-auth-v2")
    elif mode == "on_demand":
        wire = deepcopy(base)
        wire["messages"][0]["content"] += memory_system_suffix(policy, instructions["query"])
        wire["tools"].extend(memory_tools(policy.tools("solve")))
    else:
        wire = base
    return policy, items, wire


def prepare(root, cfg):
    inputs = read_json(asset_text(root, cfg["diagnostic"]).encode())["inputs"]
    selection = read_json(asset_text(root, cfg["selection"]).encode())
    plan = read_json(asset_text(root, inputs["transfer"]).encode())
    banks = archived_banks(root, plan["donors"])
    state_rows = {r["task"]: r for r in selection["decisions"] if "target_order" in r}
    source_pairs = sorted({(r["task"], r["source"]) for r in selection["notes"]})
    require(len(source_pairs) == cfg["blocks"] and list(ARMS) == cfg["arms"], "Allocation changed")
    instructions = {k: asset_text(root, cfg[k + "_instruction"]) for k in ("query", "preload")}
    terms = read_json(asset_text(root, cfg["endpoint_registration"]).encode())["endpoint_terms"]["worker"]
    cells, slots = {}, []
    for task, source in source_pairs:
        row = state_rows[task]
        origin = safe_path(root, row["journal"]).parent / "blobs/sha256" / row["wire_sha256"]
        base = read_json(verified(origin, row["wire_sha256"]))
        bank = [item for arm in cfg["forms"] for item in banks[source, arm]]
        require(len({i["id"] for i in bank}) == len(bank) <= 4, "Invalid source pool")
        evidence = plan["donors"][source]["banks"]
        bank_runs = [evidence[arm]["run_id"] for arm in cfg["forms"]]
        for arm in ARMS:
            policy, items, wire = treatment(base, arm, bank, instructions)
            initial = digest(canonical(wire))
            require(len(canonical(wire)) <= cfg["max_input_bytes"], "Initial input exceeds the study limit")
            cells[task, source, arm] = (base, policy, items, wire, [row["run"], *bank_runs])
            for repeat in range(1, cfg["repetitions"] + 1):
                key = [cfg["schedule_salt"], task, source, arm, repeat]
                slots.append(dict(task=task, source=source, customer=row["customer"], repeat=repeat,
                                  arm=arm, run="r_" + digest(canonical(key))[:32],
                                  initial_sha256=initial, items_sha256=digest(canonical(items))))
        require(cells[task, source, "query"][3] == cells[task, source, "empty_query"][3]
                and cells[task, source, "preload"][2] == cells[task, source, "query"][2],
                "Delivery arms are not matched")
    slots.sort(key=lambda row: row["run"])
    for number, row in enumerate(slots, 1):
        row["slot"] = number
    capacity = sum(cfg["query_worker_limit"] if r["arm"] in QUERY else 1 for r in slots)
    require(len(slots) == cfg["max_decisions"] == 140 and capacity == cfg["max_model_requests"] == 308
            and Decimal(str(cfg["max_usd"])) == Decimal("0.50"), "Approved scope changed")
    return slots, cells, terms


def execute(journal, slot, cell, client, limits, identity):
    return fixed_decision.execute(journal, slot, cell, client, limits, identity, result_origin="order_choice.result")


def receipt(folder, slot, model, target, *, full_audit=False):
    return fixed_decision.receipt(folder, slot, model, lambda _, choice: classify_reply(choice, target),
                                  result_origin="order_choice.result", full_audit=full_audit)


def live(root, cfg, plan, cells, terms):
    preflight = read_json(asset_text(root, cfg["preflight_ref"]).encode())
    require(preflight["slots"] == plan and preflight["possible_requests"] == cfg["max_model_requests"]
            and preflight["approved_usd"] == cfg["max_usd"], "Live batch differs from offline registration")
    targets = {r["task"]: r["target_order"] for r in
               read_json(asset_text(root, cfg["selection"]).encode())["decisions"] if "target_order" in r}
    return fixed_decision.run_slots(root, cfg, plan, lambda s: cells[s["task"], s["source"], s["arm"]], terms,
        lambda s, choice: classify_reply(choice, targets[s["task"]]), result_origin="order_choice.result",
        journal_prefix="order_choice")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "live"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cfg = read_json((ROOT / PROFILE).read_bytes())
    plan, cells, terms = prepare(ROOT, cfg)
    if args.mode == "preflight":
        model = {"pricing": prices(terms)}
        result = dict(slots=plan, possible_requests=cfg["max_model_requests"], approved_usd=cfg["max_usd"],
                      initial_max_reservation_usd=str(max(request_reservation_usd(c[3], model) for c in cells.values())),
                      unique_initial_wires=len({r["initial_sha256"] for r in plan}))
    else:
        result = live(ROOT, cfg, plan, cells, terms)
    if args.output:
        publish(args.output, result)
    print({k: v for k, v in result.items() if k not in {"slots", "journals"}})
