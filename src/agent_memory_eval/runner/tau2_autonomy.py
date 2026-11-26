"""Compile and run a small blocked comparison with a single frozen memory bank."""

from copy import deepcopy
from dataclasses import asdict
import json
import random
import sys
import time

from jsonschema import Draft202012Validator

from agent_memory_eval.adapters.tau2 import prepare_native
from agent_memory_eval.contracts.connection import validate_profile
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, digest, new_id, read_json
from agent_memory_eval.contracts.validation import ROOT, SCHEMA_ROOT
from agent_memory_eval.models.openrouter import SpendGuard
from agent_memory_eval.memory.imports import import_source, load_source
from agent_memory_eval.runner.tau2_memory import Tau2MemoryRunner, checked_asset


ARMS = ("no_memory", "empty_bank", "memory_bank")


def compile_pilot(profile, *, source_texts=None):
    """Local readiness checks only. A pilot does not promote the research draft."""
    schema_file = {"1.0":"autonomy-pilot.schema.json","1.1":"autonomy-pilot-v1.1.schema.json"}.get(profile.get("profile_version"))
    if schema_file is None:
        raise ContractError("Unsupported autonomy pilot version")
    schema = read_json((SCHEMA_ROOT / schema_file).read_bytes())
    Draft202012Validator(schema).validate(profile)
    refs = dict(connection=profile["connection_profile_ref"], **profile["instructions"])
    texts = {key:checked_asset(ref) for key,ref in refs.items()} if source_texts is None else source_texts
    if set(texts) != set(refs) or any(digest(texts[key].encode()) != ref["sha256"] for key,ref in refs.items()):
        raise ContractError("Pilot source assets do not match their declared hashes")
    connection = read_json(texts["connection"].encode())
    validate_profile(connection)
    if connection["profile_version"] != "1.1":
        raise ContractError("The pilot requires the Luna/DeepSeek 1017 profile")
    caps = profile["guards"]
    if caps["max_model_requests"] < connection["guards"]["max_model_requests"]:
        raise ContractError("The total request cap cannot be below one run's cap")
    settings = profile["memory_limits"]
    policies = {
        "collection": asdict(MemoryPolicy(True,"post_task",False,read_mode="none",**settings)),
        "no_memory": asdict(MemoryPolicy(False,"none",False,read_mode="none",**settings)),
        "empty_bank": asdict(MemoryPolicy(True,"none",False,**settings)),
        "memory_bank": asdict(MemoryPolicy(True,"none",False,**settings)),
    }
    rng = random.Random(profile["schedule_seed"])
    first = list(ARMS)
    rng.shuffle(first)
    orders = [first[offset:]+first[:offset] for offset in range(len(ARMS))]
    rng.shuffle(orders)
    return dict(profile=deepcopy(profile), source_texts=deepcopy(texts), connection=connection,
                policies=policies, orders=orders, worker_message_rendering="provider_original",
                source_mode="frozen_external" if "frozen_source" in profile else "new_collection")


def prepare_pilot(profile, checkout):
    plan = compile_pilot(profile)
    external = load_source(profile["frozen_source"]) if plan["source_mode"] == "frozen_external" else None
    native = prepare_native(plan["connection"],checkout)
    if any(tool.name.startswith("memory_") for tool in native[0].agent.tools):
        raise ContractError("Native tool namespace overlaps external memory")
    return plan,native,external


def make_schedule(plan):
    schedule = []
    if plan["source_mode"] == "new_collection":
        schedule.append(dict(run_id=new_id("r"),stage="collection",condition="collection",block_id=None,position=None))
    for block,order in enumerate(plan["orders"],1):
        rows = [dict(run_id=new_id("r"),stage="probe",condition=arm,block_id=block,position=position)
                for position,arm in enumerate(order,1)]
        baseline = next(row["run_id"] for row in rows if row["condition"] == "no_memory")
        schedule.extend(dict(row,baseline_run_id=baseline) for row in rows)
    return schedule


def run_pilot(journal, profile, prepared, *, live, opener=None):
    plan,native,external = prepared
    if (plan["source_mode"] == "frozen_external") != (external is not None):
        raise ContractError("Frozen-source preparation is missing or unexpected")
    if compile_pilot(profile) != plan:
        raise ContractError("Prepared pilot sources or settings changed")
    schedule = make_schedule(plan)
    state = deepcopy(journal.state)
    state["autonomy_pilot"] = dict(requested=profile,orders=plan["orders"],policies=plan["policies"],
        source_mode=plan["source_mode"],
        connection_profile=plan["connection"],
        source_refs={name:journal.blobs.put(text) for name,text in plan["source_texts"].items()},
        native_resolved=native[1].model_dump(mode="json"),model_catalog_entries=native[2],
        response_cache_enabled=False,
        native_system_ref=journal.blobs.put(native[0].agent.system_prompt),
        business_tools_ref=journal.blobs.put([tool.openai_schema for tool in native[0].agent.tools]),
        implementation_ref=journal.blobs.put(dict(python=sys.version.split()[0],
            sources={str(path.relative_to(ROOT)):digest(path.read_bytes())
                     for path in sorted((ROOT/"src/agent_memory_eval").rglob("*.py"))})),
        worker_message_rendering="provider_original",source_snapshot=None,empty_snapshot=None,
        source_eligibility="not_run",runtime_status="prepared_only" if not live else "running")
    state["schedule"],state["skipped_runs"] = schedule,{}
    journal.append([],state=state)  # Allocation is fixed before any model output.
    if not live:
        return dict(intent="autonomous_memory_pilot",status="prepared_only",benchmark_or_model_calls=0,
                    source_runs=int(plan["source_mode"] == "new_collection"),
                    probe_runs=sum(row["stage"] == "probe" for row in schedule),orders=plan["orders"])
    limits = profile["guards"]
    guard = SpendGuard(max_calls=limits["max_model_requests"],max_usd=limits["max_reported_cost_usd"],max_seconds=limits["max_seconds"])
    runner = Tau2MemoryRunner(journal,plan["connection"],native,guard,opener=opener)
    first,archive = None,None
    if external is not None:
        archive = import_source(runner.memory,profile["frozen_source"],MemoryPolicy(**plan["policies"]["memory_bank"]))
        source_snapshot,eligible,reason = archive["local_snapshot_id"],True,"eligible"
    else:
        first = runner.episode(run_id=schedule[0]["run_id"],stage="collection",
            policy=MemoryPolicy(**plan["policies"]["collection"]),condition_id="collection",mode="autonomous_memory_pilot",
            solve_instruction=plan["source_texts"]["collection_solve"],post_instruction=plan["source_texts"]["post_task_write"])
        source_snapshot = first["snapshot_id"]
        heads = journal.state["snapshots"][source_snapshot]["heads"]
        eligible = first["status"] == "completed" and bool(heads)
        reason = "eligible" if eligible else "source_empty" if first["status"] == "completed" else "source_incomplete"
    empty = runner.memory.create_bank(new_id("control"),MemoryPolicy(**plan["policies"]["empty_bank"]))
    empty_snapshot = runner.memory.freeze(empty)
    runner.memory.seal(empty)
    state = deepcopy(journal.state)
    state["autonomy_pilot"].update(source_snapshot=source_snapshot,empty_snapshot=empty_snapshot,
                                    source_eligibility=reason,source_archive=archive)
    journal.append([],state=state)
    stop_reason = None
    for row in (row for row in schedule if row["stage"] == "probe"):
        if not eligible:
            stop_reason = reason
        elif guard.unknown_cost or any(event["type"] == "model.request_finished" and event["payload"]["cost_status"] == "unknown"
                                      for event in journal.events.values()):
            stop_reason = "unknown_cost"
        elif guard.calls >= guard.max_calls:
            stop_reason = "call_limit"
        elif time.monotonic() >= guard.deadline:
            stop_reason = "time_limit"
        elif guard.reported_usd >= guard.max_usd:
            stop_reason = "spend_limit"
        if stop_reason:
            state = deepcopy(journal.state)
            state["skipped_runs"][row["run_id"]] = stop_reason
            journal.append([],state=state)
            continue
        arm = row["condition"]
        parent = {"no_memory":None,"empty_bank":empty_snapshot,"memory_bank":source_snapshot}[arm]
        result = runner.episode(run_id=row["run_id"],stage="probe",policy=MemoryPolicy(**plan["policies"][arm]),
            condition_id=arm,mode="autonomous_memory_pilot",block_id=row["block_id"],parent=parent,
            sources=[first["run_id"]] if arm == "memory_bank" and first else [],
            solve_instruction="" if arm == "no_memory" else plan["source_texts"]["probe_memory"],post_instruction="")
        if result["error_code"] in {"SPEND_LIMIT","CALL_LIMIT","TIME_LIMIT","UNKNOWN_COST_STOP"}:
            stop_reason = result["error_code"].lower()
    state = deepcopy(journal.state)
    state["autonomy_pilot"].update(runtime_status="finished",http_calls=guard.calls)
    journal.append([],state=state)
    journal.export_views()
    from agent_memory_eval.analysis.tau2_autonomy import summarize_pilot
    return summarize_pilot(journal.root)
