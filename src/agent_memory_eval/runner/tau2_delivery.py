"""Explicit tasks, conditions and repetitions for frozen-bank delivery comparisons."""

from copy import deepcopy
from dataclasses import asdict
import random
import sys
import time

from jsonschema import Draft202012Validator

from agent_memory_eval.adapters.tau2 import prepare_native, verify_checkout
from agent_memory_eval.contracts.connection import validate_profile
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest, new_id, read_json
from agent_memory_eval.contracts.validation import ROOT, SCHEMA_ROOT
from agent_memory_eval.memory.imports import import_source, load_source
from agent_memory_eval.models.openrouter import SpendGuard
from agent_memory_eval.recording.journal import BlobStore
from agent_memory_eval.runner.tau2_memory import Tau2MemoryRunner, checked_asset


CONDITIONS = ("no_memory", "empty_bank", "on_demand", "full_preload")


def compile_delivery(profile, *, require_ready=True, source_texts=None):
    schema = read_json((SCHEMA_ROOT / "delivery-comparison.schema.json").read_bytes())
    try:
        Draft202012Validator(schema).validate(profile)
    except Exception as error:
        raise ContractError(f"Invalid delivery comparison profile: {error.message if hasattr(error, 'message') else error}") from error
    refs = dict(connection=profile["connection_profile_ref"], memory_instruction=profile["memory_instruction_ref"])
    texts = {k: checked_asset(v) for k, v in refs.items()} if source_texts is None else source_texts
    if set(texts) != set(refs) or any(digest(texts[k].encode()) != v["sha256"] for k, v in refs.items()):
        raise ContractError("Delivery profile assets changed")
    connection = read_json(texts["connection"].encode())
    validate_profile(connection)
    if connection["profile_version"] != "1.1":
        raise ContractError("Delivery comparisons require the current Luna/DeepSeek transport profile")
    missing = [k for k in ("tasks", "repetitions", "schedule_seed", "frozen_source", "guards") if profile[k] is None]
    if profile["memory_limits"]["page_size"] is None:
        missing.append("memory_limits.page_size")
    if "full_preload" in profile["conditions"] and profile["preload_query_tools"] is None:
        missing.append("preload_query_tools")
    if profile["tasks"] is not None and len({t["id"] for t in profile["tasks"]}) != len(profile["tasks"]):
        raise ContractError("Task IDs must be unique")
    if not set(profile["primary_comparison"].values()) <= set(profile["conditions"]) or len(set(profile["primary_comparison"].values())) != 2:
        raise ContractError("Primary comparison must name two selected, distinct conditions")
    ready = profile["status"] == "ready" and not missing
    if (require_ready or profile["status"] == "ready") and not ready:
        raise ContractError("Delivery profile is not ready: " + ", ".join(missing or ["status=draft"]))
    plan = dict(profile=deepcopy(profile), source_texts=deepcopy(texts), connection=connection,
                ready=ready, unresolved_fields=missing)
    if missing:
        return plan
    settings = profile["memory_limits"]
    modes = dict(no_memory="none", empty_bank="on_demand", on_demand="on_demand",
                 full_preload="preload_and_on_demand" if profile["preload_query_tools"] else "preload")
    plan["policies"] = {c: asdict(MemoryPolicy(c != "no_memory", "none", False, read_mode=modes[c], **settings))
                        for c in profile["conditions"]}
    plan["connections"] = {}
    for task in profile["tasks"]:
        config = deepcopy(connection)
        config["benchmark"]["task_id"] = task["id"]
        plan["connections"][task["id"]] = config
    rng = random.Random(profile["schedule_seed"])
    blocks = []
    for task in profile["tasks"]:
        first = list(profile["conditions"])
        rng.shuffle(first)
        for repeat in range(profile["repetitions"]):
            offset = repeat % len(first)
            blocks.append(dict(task_id=task["id"], source_group_id=task["source_group_id"],
                               repetition=repeat + 1, conditions=first[offset:] + first[:offset]))
    rng.shuffle(blocks)
    plan["blocks"] = blocks
    return plan


def preflight_delivery(profile, checkout):
    """No network, model catalog or model calls; validate local sources first."""
    plan = compile_delivery(profile)
    checkout = verify_checkout(checkout)
    bench = plan["connection"]["benchmark"]
    folder = checkout / "data/tau2/domains" / bench["domain"]
    split = read_json((folder / "split_tasks.json").read_bytes())
    tasks = {t["id"]: t for t in read_json((folder / "tasks.json").read_bytes())}
    for task in profile["tasks"]:
        if task["id"] not in split[bench["split"]] or task["id"] not in tasks:
            raise ContractError("A delivery task is outside the declared official split")
    plan["benchmark_assets"] = {str(p.relative_to(checkout)): digest(p.read_bytes())
        for p in (folder / "tasks.json", folder / "split_tasks.json", folder / "policy.md", folder / "db.json")}
    source = load_source(profile["frozen_source"])
    memories = [BlobStore(source["root"]).get(source["state"]["versions"][v]) for v in source["snapshot"]["heads"].values()]
    limits = profile["memory_limits"]
    size = sum(len(canonical({k: m[k] for k in ("title", "body")})) for m in memories)
    if ((limits["max_items"] is not None and len(memories) > limits["max_items"])
            or (limits["max_payload_bytes"] is not None and size > limits["max_payload_bytes"])):
        raise ContractError("Frozen memory exceeds the declared storage limits")
    if "full_preload" in profile["conditions"] and limits["max_return_bytes"] is not None:
        public = [{k: m[k] for k in ("id", "version", "title", "body")} for m in memories]
        if len(canonical(dict(items=public))) > limits["max_return_bytes"]:
            raise ContractError("Full preload exceeds the declared return-byte limit")
    return plan, checkout, source


def prepare_delivery(profile, checkout):
    plan, checkout, _ = preflight_delivery(profile, checkout)
    native = {task: prepare_native(config, checkout) for task, config in plan["connections"].items()}
    if any(tool.name.startswith("memory_") for prepared in native.values() for tool in prepared[0].agent.tools):
        raise ContractError("Native tool namespace overlaps external memory")
    return dict(plan=plan, checkout=checkout, native=native)


def make_schedule(plan):
    schedule = []
    for block_id, block in enumerate(plan["blocks"], 1):
        for position, condition in enumerate(block["conditions"], 1):
            schedule.append(dict(run_id=new_id("r"), stage="probe", block_id=block_id, position=position,
                task_id=block["task_id"], source_group_id=block["source_group_id"],
                repetition=block["repetition"], condition=condition))
    return schedule


def stop_reason(guard, journal):
    if guard.unknown_cost or any(e["type"] == "model.request_finished" and e["payload"]["cost_status"] == "unknown"
                                 for e in journal.events.values()):
        return "unknown_cost"
    if guard.calls >= guard.max_calls:
        return "call_limit"
    if time.monotonic() >= guard.deadline:
        return "time_limit"
    if guard.reported_usd >= guard.max_usd:
        return "spend_limit"
    return None


def run_delivery(journal, profile, prepared, *, opener=None):
    if journal.contract_version != "1.2" or journal.scan["records"]:
        raise ContractError("Delivery execution needs a new v1.2 journal")
    plan, _, _ = preflight_delivery(profile, prepared["checkout"])
    if plan != prepared["plan"]:
        raise ContractError("Prepared delivery plan or assets changed")
    if set(prepared["native"]) != set(plan["connections"]):
        raise ContractError("Prepared task set differs")
    for task, native in prepared["native"].items():
        if native[0].task.id != task or native[1].task_ids != [task]:
            raise ContractError("Prepared native task differs from allocation")
    schedule = make_schedule(plan)
    native_controls = {task: dict(native_resolved=p[1].model_dump(mode="json"), model_catalog_entries=p[2],
        native_system_ref=journal.blobs.put(p[0].agent.system_prompt),
        business_tools_ref=journal.blobs.put([t.openai_schema for t in p[0].agent.tools]))
        for task, p in prepared["native"].items()}
    state = deepcopy(journal.state)
    state["delivery_comparison"] = dict(requested=deepcopy(profile), blocks=plan["blocks"], policies=plan["policies"],
        source_refs={k: journal.blobs.put(v) for k, v in plan["source_texts"].items()},
        benchmark_assets=plan["benchmark_assets"], native_controls=native_controls, response_cache_enabled=False,
        implementation_ref=journal.blobs.put(dict(python=sys.version.split()[0], sources={str(p.relative_to(ROOT)): digest(p.read_bytes())
            for p in sorted((ROOT / "src/agent_memory_eval").rglob("*.py"))})), runtime_status="running")
    state["schedule"], state["skipped_runs"] = schedule, {}
    journal.append([], state=state)
    limits = profile["guards"]
    guard = SpendGuard(max_calls=limits["max_model_requests"], max_usd=limits["max_reported_cost_usd"], max_seconds=limits["max_seconds"])
    runners = {task: Tau2MemoryRunner(journal, plan["connections"][task], native, guard, opener=opener)
               for task, native in prepared["native"].items()}
    memory = next(iter(runners.values())).memory
    read_policy = MemoryPolicy(True, "none", False, read_mode="on_demand", **profile["memory_limits"])
    archive = import_source(memory, profile["frozen_source"], read_policy)
    empty = memory.create_bank(new_id("control"), read_policy)
    empty_snapshot = memory.freeze(empty)
    memory.seal(empty)
    state = deepcopy(journal.state)
    state["delivery_comparison"].update(source_archive=archive, source_snapshot=archive["local_snapshot_id"], empty_snapshot=empty_snapshot)
    journal.append([], state=state)
    stopped = None
    for row in schedule:
        stopped = stopped or stop_reason(guard, journal)
        if stopped:
            state = deepcopy(journal.state)
            state["skipped_runs"][row["run_id"]] = stopped
            journal.append([], state=state)
            continue
        condition = row["condition"]
        parent = None if condition == "no_memory" else empty_snapshot if condition == "empty_bank" else archive["local_snapshot_id"]
        result = runners[row["task_id"]].episode(run_id=row["run_id"], stage="probe",
            policy=MemoryPolicy(**plan["policies"][condition]), condition_id=condition, mode="frozen_bank_delivery",
            block_id=row["block_id"], parent=parent,
            solve_instruction="" if condition == "no_memory" else plan["source_texts"]["memory_instruction"], post_instruction="")
        if result["error_code"] in {"SPEND_LIMIT", "CALL_LIMIT", "TIME_LIMIT", "UNKNOWN_COST_STOP"}:
            stopped = result["error_code"].lower()
    state = deepcopy(journal.state)
    state["delivery_comparison"].update(runtime_status="finished", http_calls=guard.calls)
    journal.append([], state=state)
    journal.export_views()
    from agent_memory_eval.analysis.tau2_delivery import summarize_delivery
    return summarize_delivery(journal.root)
