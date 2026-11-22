"""Rebuild fixed-state exposures, actions, customer-paired estimates and costs from sealed receipts."""

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from statistics import mean

from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.snapshot_replay import replay_response_error
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from agent_memory_eval.recording.provenance import public_assistant_message
from scripts.analyze_full_retail import require
from scripts.analyze_retail_sample_expansion import bootstrap_customer_interval
from scripts.audit_retail_order_choice import classify_reply, note_label, publish
from scripts.run_retail_order_choice_replay import ARMS, PROFILE, QUERY, prepare, prices, receipt


def verify_slot(folder, slot, cell, target, model):
    evidence = read_experiment(folder)
    scan, state, _ = evidence
    blobs = BlobStore(folder)
    row = receipt(folder, slot, model, target)
    if not row["complete"]:
        require(row["behavior"] == "unobserved", "Unsealed decision has a score")
        return row
    audit_requests(folder, evidence=evidence)
    base, policy, items, wire, sources = cell
    events = list(scan["events"].values())
    (start,) = [e["payload"] for e in events if e["type"] == "run.started"]
    require(blobs.get(start["task_ref"]) == base and start["source_run_ids"] == sources
            and start["fresh_session"], "Frozen state or source binding differs")
    if policy.enabled:
        bank = state["banks"][start["bank_id"]]
        require(bank["owner_run"] == slot["run"] and bank["sealed"] and bank["heads"] == bank["start_heads"]
                and snapshot_items(blobs, state, start["start_snapshot_id"]) == items,
                "Wrong or modified source snapshot")
    else:
        require(start["bank_id"] is None and start["start_snapshot_id"] is None, "Pure baseline received a bank")
    expected, finished, pending, derived = deepcopy(wire), False, [], "unobserved"
    for event in events:
        name, payload = event["type"], event["payload"]
        if name == "model.request_started":
            require(not finished and not pending and blobs.get(payload["request_ref"]) == expected,
                    "Recorded model input differs from the prefix, memory or internal tool results")
        elif name == "model.request_finished":
            if payload["status"] != "response":
                finished = True
                continue
            raw = blobs.get(payload["response_ref"])
            if replay_response_error(raw, model["identity"]):
                finished = True
                continue
            choice = raw["choices"][0]
            public = public_assistant_message(choice["message"])
            calls = public.get("tool_calls") or []
            if calls and all(c["function"]["name"].startswith("memory_") for c in calls):
                require(slot["arm"] in QUERY or row["error"] == "UNAVAILABLE_MEMORY_TOOL", "Unexpected memory path")
                expected["messages"].append(public)
                pending = [c["id"] for c in calls]
            else:
                finished, derived = True, classify_reply(choice, target)
        elif name in {"memory.returned", "policy.denied"}:
            ref = payload["result_ref"] if name == "memory.returned" else payload["tool_result_ref"]
            returned = blobs.get(ref)
            require(pending and returned["tool_call_id"] == pending.pop(0), "Tool return has no matching call")
            expected["messages"].append(returned)
        elif name == "business.transition":
            require(payload.get("origin") == "order_choice.result", "A business tool was executed")
    require(derived == row["behavior"], "Terminal decision readout differs from the provider response")
    return row


def contrast(rows, left, right):
    pairs = defaultdict(dict)
    for r in rows:
        if r["arm"] in (left, right):
            key = (r["customer"], r["task"], r["source"], r["repeat"])
            require(r["arm"] not in pairs[key], "Duplicate delivery assignment")
            pairs[key][r["arm"]] = r["behavior"]
    edges, counts = defaultdict(list), Counter()
    for (customer, task, source, _), arms in pairs.items():
        if any(arms.get(a, "unobserved") == "unobserved" for a in (left, right)):
            counts["missing"] += 1
            continue
        a, b = (arms[arm] == "focused_target" for arm in (left, right))
        counts[{(True, True): "both", (True, False): "left_only", (False, True): "right_only",
                (False, False): "neither"}[a, b]] += 1
        edges[customer, task, source].append(int(a) - int(b))
    tasks, customers = defaultdict(list), defaultdict(list)
    for (customer, task, _), repeats in edges.items():
        tasks[customer, task].append(mean(repeats))
    for (customer, _), values in tasks.items():
        customers[customer].append(mean(values))
    values = [mean(customers[k]) for k in sorted(customers)]
    return dict(pair_counts=dict(sorted(counts.items())), customers=len(values), source_target_pairs=len(edges),
                difference=mean(values) if values else None, customer_interval=bootstrap_customer_interval(values))


def analyze(root, cfg):
    plan, cells, terms = prepare(root, cfg)
    dest = root / "runs" / cfg["name"]
    registration = read_json((dest / "registration.json").read_bytes())
    done = read_json((dest / "completion.json").read_bytes())
    require(registration["slots"] == plan and registration["profile"] == cfg
            and registration["reservation_pricing"] == prices(terms)
            and done["registration_sha256"] == digest((dest / "registration.json").read_bytes()), "Wrong registration")
    model = dict(identity=registration["identity"], pricing=prices(terms))
    inventory = read_json(asset_text(root, cfg["selection"]).encode())
    states = {r["task"]: r for r in inventory["decisions"] if "target_order" in r}
    rows = []
    for s in plan:
        key = s["task"], s["source"], s["arm"]
        target = states[s["task"]]["target_order"]
        bank = cells[s["task"], s["source"], "preload"][2]
        group, _ = note_label(bank, states[s["task"]]["profile_orders"], target)
        if str(s["slot"]) in done["journals"]:
            folder = dest / f"slot-{s['slot']:03}"
            require(digest((folder / "log.jsonl").read_bytes()) == done["journals"][str(s["slot"])],
                    "Changed decision journal")
            row = verify_slot(folder, s, cells[key], target, model)
        else:
            require(not (dest / f"slot-{s['slot']:03}/log.jsonl").exists(), "Unreported slot journal")
            row = dict(**s, behavior="unobserved", error="NOT_STARTED", confirmed_usd="0", held_usd="0",
                       submitted_or_uncertain=0, memory_calls=[], confirmed_input_fields=[], generations=[])
        rows.append(dict(row, bank_cue=group))
    sessions = [r["session"] for r in rows if r.get("session")]
    generations = [g for r in rows for g in r["generations"]]
    require(len(set(sessions)) == len(sessions) and len(set(generations)) == len(generations),
            "Session or provider generation reused")
    require(sum(r["submitted_or_uncertain"] for r in rows) == done["submitted"]
            and sum(Decimal(r["confirmed_usd"]) for r in rows) == Decimal(done["confirmed_usd"])
            and sum(Decimal(r["held_usd"]) for r in rows) == Decimal(done["held_usd"]),
            "Receipt accounting differs")
    by_arm = {}
    for arm in ARMS:
        rr = [r for r in rows if r["arm"] == arm]
        by_arm[arm] = dict(decisions=len(rr), behaviors=dict(sorted(Counter(r["behavior"] for r in rr).items())),
            list_calls=sum(r["memory_calls"].count("memory_list") for r in rr),
            get_calls=sum(r["memory_calls"].count("memory_get") for r in rr),
            title_seen=sum("title" in r["confirmed_input_fields"] for r in rr),
            body_seen=sum("body" in r["confirmed_input_fields"] for r in rr),
            submitted=sum(r["submitted_or_uncertain"] for r in rr),
            confirmed_usd=str(sum(Decimal(r["confirmed_usd"]) for r in rr)))
    pairs = [("preload", "empty_preload"), ("query", "empty_query"), ("preload", "no_memory"),
             ("query", "no_memory"), ("preload", "query")]
    return dict(study=cfg["name"], freeze=registration["freeze"], accounting={k:v for k,v in done.items() if k!="journals"},
                by_arm=by_arm, contrasts={a+"_vs_"+b: contrast(rows, a, b) for a, b in pairs},
                cue_counts={g: {a: dict(sorted(Counter(r["behavior"] for r in rows
                    if r["bank_cue"] == g and r["arm"] == a).items())) for a in ARMS}
                    for g in sorted({r["bank_cue"] for r in rows})}, rows=rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = analyze(ROOT, read_json((ROOT / PROFILE).read_bytes()))
    publish(args.output, result)
    print(result["by_arm"])
    print(result["contrasts"]["preload_vs_empty_preload"])
