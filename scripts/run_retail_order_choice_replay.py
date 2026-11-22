"""Run the authorized fixed-state retail comparison using isolated v1.2 journals."""

import argparse
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
import fcntl
import os
from pathlib import Path
import time
from types import SimpleNamespace

from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, RunContext, canonical, digest, new_id, read_json
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.contracts.snapshot_replay import replay_response_error
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.memory.context import memory_system_suffix, request_with_past_memory
from agent_memory_eval.memory.imports import install_snapshot
from agent_memory_eval.memory.router import ToolRouter
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.models.openrouter import (OpenRouterClient, ProviderError, SpendGuard, model_catalog,
    model_endpoints, request_reservation_usd, resolve_endpoint, resolve_model)
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, Journal, read_experiment, safe_path
from agent_memory_eval.recording.provenance import InputSizeLimit, RecordingGateway, SourceRegistry, public_assistant_message
from agent_memory_eval.reproduction.freeze import implementation_identity
from scripts.analyze_full_retail import require
from scripts.analyze_retail_full_factors import verified
from scripts.audit_retail_order_choice import archived_banks, classify_reply, publish
from scripts.run_recheck_rounds import fixed

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
    base, policy, items, wire, sources = cell
    kind = "illustrative" if getattr(client, "offline", False) else "observed"
    ctx = RunContext(journal.experiment_id, slot["run"], new_id("s"), slot["arm"], "diagnostic", "solve", kind)
    memory = MemoryService(journal, record_kind=kind)
    parent = bank = None
    if policy.enabled:
        heads = {i["id"]: i["version"] for i in items}
        parent = install_snapshot(memory, policy, heads, list(heads), len(heads),
                                  {i["version"]: journal.blobs.put(i) for i in items})
        bank = memory.fork(parent, ctx.run_id, policy)
    state = deepcopy(journal.state)
    state["assignments"][ctx.run_id] = dict(execution_contract="per-role-limits/v1",
        execution_limits=asdict(limits), slot=slot, base_sha256=digest(canonical(base)))
    journal.append([ctx.at("prepare").event("run.started", dict(bank_id=bank, start_snapshot_id=parent,
        fresh_session=True, source_run_ids=sources, task_ref=journal.blobs.put(base)))], state=state)
    registry = SourceRegistry(journal, ctx)
    template = registry.template({k: v for k, v in wire.items() if k != "messages"}, roles=("worker",))
    entries = []
    for index, message in enumerate(wire["messages"]):
        if policy.preloads and index == 1:
            event = memory.preload(ctx, bank)
            entries.append((registry.add("memory_snapshot", event["event_id"]), ""))
        else:
            entries.append((registry.template(message, roles=("worker",)), ""))
    router = ToolRouter(journal, memory, SimpleNamespace(stop_tools=set(), ended=False), bank, policy)
    expected = deepcopy(wire)
    error, terminal = None, None
    try:
        for turn in range(limits.worker_requests):
            def exact(actual):
                require(actual == expected, "Wire differs from the frozen prefix and public continuations")
                return actual

            message, source, rid = RecordingGateway(journal).call(
                ctx, registry, "worker", template, entries, client, max_requests=None,
                execution_limits=limits, transform=exact, project_self_authored=True)
            raw = journal.blobs.get(registry.sources[source]["artifact_ref"])
            error = replay_response_error(raw, identity)
            if error:
                break
            calls = message.get("tool_calls") or []
            if not calls or any(not c["function"]["name"].startswith("memory_") for c in calls):
                terminal = rid
                break
            if slot["arm"] not in QUERY:
                error = "UNAVAILABLE_MEMORY_TOOL"
                break
            if turn + 1 == limits.worker_requests:
                error = "MEMORY_ROUND_LIMIT"
                break
            entries.append((source, ""))
            expected["messages"].append(public_assistant_message(message))
            for returned, origin, event_id in router.dispatch(ctx, rid, calls):
                entries.append((registry.add(origin, event_id), ""))
                expected["messages"].append(returned)
    except (ProviderError, InputSizeLimit) as exc:
        error = exc.code
    if error:
        journal.append([ctx.event("runtime.error", dict(code=error))])
    end = None
    if bank:
        end = memory.freeze(bank)
        memory.seal(bank)
    journal.append([ctx.at("grade").event("business.transition", dict(origin="order_choice.result",
        result_ref=journal.blobs.put(dict(terminal_request=terminal, error=error))))])
    journal.append([ctx.at("grade").event("outcome.scored", dict(grading_status="not_available",
        official_success=None, official_score=None, official_artifact_ref=None))])
    journal.append([ctx.at("seal").event("run.ended", dict(termination_reason=error or "first_decision",
        grading_status="not_available", end_snapshot_id=end))])


def receipt(folder, slot, model, target, *, full_audit=False):
    from agent_memory_eval.analysis.requests import audit_requests

    scan, state, experiment = read_experiment(folder)
    events = list(scan["events"].values())
    blobs = BlobStore(folder)
    runs = set(scan["starts"])
    require(runs <= {slot["run"]} and not scan["tail_bytes"], "Unreconciled slot journal")
    complete = bool(runs) and not scan["incomplete_runs"] and not scan["pending_requests"]
    if full_audit and complete:
        audit_requests(folder, evidence=(scan, state, experiment))
    starts = {e["payload"]["request_id"]: e["payload"] for e in events if e["type"] == "model.request_started"}
    finishes = {e["payload"]["request_id"]: e["payload"] for e in events if e["type"] == "model.request_finished"}
    if starts:
        require(next(iter(starts.values()))["request_ref"]["sha256"] == slot["initial_sha256"],
                "First transmitted request differs from its registered input")
    outcome = {"terminal_request": None, "error": "INTERRUPTED"}
    for e in events:
        if e["type"] == "business.transition" and e["payload"].get("origin") == "order_choice.result":
            outcome = blobs.get(e["payload"]["result_ref"])
    confirmed, held = Decimal(0), Decimal(0)
    unknown, generations, memory_calls, terminal_tools = [], [], [], []
    retry_after = 0
    behavior = "unobserved"
    for rid, start in starts.items():
        finish = finishes.get(rid)
        if not finish or finish["cost_status"] == "unknown":
            reserve = request_reservation_usd(blobs.get(start["request_ref"]), model)
            unknown.append(dict(request_id=rid, maximum_usd=str(reserve)))
            held += reserve
        if not finish:
            continue
        if finish["cost_status"] == "observed":
            confirmed += Decimal(str(finish["cost_usd"]))
        if not finish["response_ref"]:
            continue
        raw = blobs.get(finish["response_ref"])
        if raw.get("id"):
            generations.append(raw["id"])
        if raw.get("error"):
            from agent_memory_eval.experiments.forms_supplement import cooldown_seconds
            from datetime import datetime, timezone
            retry_after = max(retry_after, cooldown_seconds(raw["error"], 0, datetime.now(timezone.utc)))
        if finish["status"] == "response":
            choice = raw["choices"][0]
            names = [c["function"]["name"] for c in choice["message"].get("tool_calls") or []]
            memory_calls.extend(n for n in names if n.startswith("memory_"))
            if rid == outcome["terminal_request"]:
                require(not replay_response_error(raw, model.get("identity")), "Invalid terminal response")
                behavior = classify_reply(choice, target)
                terminal_tools = names
    started = sum(1 for rid in starts if rid not in finishes or finishes[rid]["delivery_status"] != "not_submitted")
    included = [e["payload"] for e in events if e["type"] == "memory.input_included"]
    seen = {p["field"] for p in included if finishes.get(p["request_id"], {}).get("status") == "response"}
    return dict(**slot, behavior=behavior, error=outcome["error"], complete=complete,
        requests=len(starts), submitted_or_uncertain=started, confirmed_usd=str(confirmed), held_usd=str(held),
        unpriced=unknown, generations=generations, memory_calls=memory_calls, terminal_tools=terminal_tools,
        returned_memory_events=sum(e["type"] == "memory.returned" for e in events),
        exposed_fields=sorted({p["field"] for p in included}), confirmed_input_fields=sorted(seen), retry_after=retry_after,
        session=next((e["session_id"] for e in events if e["type"] == "run.started"), None),
        journal_sha256=digest((folder / "log.jsonl").read_bytes()))


def prices(terms):
    listed = [terms["pricing"], *terms["pricing"].get("overrides", [])]
    return {"prompt": max(float(p.get(k, 0)) for p in listed
                          for k in ("prompt", "input_cache_write", "input_cache_read")),
            "completion": max(float(p["completion"]) for p in listed)}


def live(root, cfg, plan, cells, terms):
    require(cfg["status"] == "ready" and os.getenv("OPENROUTER_API_KEY"), "Live profile or credential unavailable")
    preflight = read_json(asset_text(root, cfg["preflight_ref"]).encode())
    require(preflight["slots"] == plan and preflight["possible_requests"] == cfg["max_model_requests"]
            and preflight["approved_usd"] == cfg["max_usd"], "Live batch differs from offline registration")
    frozen = implementation_identity(ROOT)
    first = next(iter(cells.values()))[3]
    params = {k: v for k, v in first.items() if k not in {"model", "provider", "stream", "messages"}}
    model = resolve_model(model_catalog(), first["model"], params)
    model = resolve_endpoint(model, model_endpoints(model["id"]), first["provider"], params)
    endpoint = model["selected_endpoint"]
    require((model["id"], endpoint["tag"], endpoint["provider_name"]) ==
            (terms["model"], terms["provider"], terms["provider_name"])
            and endpoint["pricing"] == terms["pricing"], "Registered worker route or prices changed")
    model["reservation_pricing"] = prices(terms)
    identity = dict(id=model["id"], canonical_slug=model.get("canonical_slug"), provider_name=endpoint["provider_name"])
    model["identity"] = identity
    dest = root / "runs" / cfg["name"]
    dest.mkdir(exist_ok=True)
    with (dest / "controller.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fixed(dest / "registration.json", dict(profile=cfg, freeze=frozen, slots=plan, identity=identity,
                                               reservation_pricing=model["reservation_pricing"]))
        if (dest / "completion.json").exists():
            done = read_json((dest / "completion.json").read_bytes())
            require(done["registration_sha256"] == digest((dest / "registration.json").read_bytes()),
                    "Completion is bound to a different registration")
            return done
        start_path = dest / "start.json"
        if not start_path.exists():
            write_json(start_path, {"unix_time": time.time()})
        deadline = read_json(start_path.read_bytes())["unix_time"] + cfg["max_seconds"]
        targets = {r["task"]: r["target_order"] for r in
                   read_json(asset_text(root, cfg["selection"]).encode())["decisions"] if "target_order" in r}
        rows, spent, held, submitted = [], Decimal(0), Decimal(0), 0
        stop, delay = None, 0
        for slot in plan:
            folder = dest / f"slot-{slot['slot']:03}"
            if not (folder / "log.jsonl").exists():
                if stop:
                    break
                cell = cells[slot["task"], slot["source"], slot["arm"]]
                available = Decimal(str(cfg["max_usd"])) - spent - held
                if available < request_reservation_usd(cell[3], model) or submitted >= cfg["max_model_requests"]:
                    stop = "budget_or_request_limit"
                    break
                if delay:
                    time.sleep(min(delay, max(0, deadline - time.time())))
                remaining_time = deadline - time.time()
                if remaining_time <= 0:
                    stop = "time_limit"
                    break
                guard = SpendGuard(max_calls=cfg["max_model_requests"] - submitted,
                                   max_usd=float(available), max_seconds=remaining_time)
                client = OpenRouterClient(model, guard, route=first["provider"], max_input_bytes=cfg["max_input_bytes"],
                                          timeout_seconds=cfg["timeout_seconds"])
                limits = ExecutionLimits(cfg["query_worker_limit"] if slot["arm"] in QUERY else 1,
                                         0, 0, cfg["max_input_bytes"])
                with Journal(folder, f"order_choice_{slot['slot']}", contract_version="1.2") as journal:
                    execute(journal, slot, cell, client, limits, identity)
            row = receipt(folder, slot, model, targets[slot["task"]])
            rows.append(row)
            spent += Decimal(row["confirmed_usd"])
            held += Decimal(row["held_usd"])
            submitted += row["submitted_or_uncertain"]
            require(spent + held <= Decimal(str(cfg["max_usd"])) and submitted <= cfg["max_model_requests"],
                    "Accounting exceeds its approval")
            error = row["error"] or ""
            delay = max(8, row["retry_after"]) if error in {"HTTP_429", "HTTP_503", "TRANSPORT_UNKNOWN"} else 0
            if error in {"REPLAY_MODEL_MISMATCH", "UNPINNED_ROUTE", "SPEND_LIMIT", "TIME_LIMIT", "CALL_LIMIT",
                         "HTTP_400", "HTTP_401", "HTTP_403", "HTTP_402", "HTTP_404"}:
                stop = error
            write_json(dest / "progress.json", dict(attempted=len(rows), submitted=submitted, confirmed_usd=str(spent),
                                                    held_usd=str(held), last_error=row["error"]))
            if len(rows) % 10 == 0:
                print(f"{len(rows)}/{len(plan)} decisions, {submitted} requests, USD {spent} + {held} held", flush=True)
        completion = dict(attempted=len(rows), unstarted=len(plan)-len(rows), submitted=submitted,
                          confirmed_usd=str(spent), held_usd=str(held), stop_reason=stop,
                          registration_sha256=digest((dest / "registration.json").read_bytes()),
                          journals={str(r["slot"]): r["journal_sha256"] for r in rows})
        fixed(dest / "completion.json", completion)
        return completion


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
