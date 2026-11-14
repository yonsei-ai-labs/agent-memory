"""Recompute a small autonomy pilot from immutable evidence, without model calls."""

from collections import Counter
from decimal import Decimal
from itertools import combinations
from pathlib import Path

from agent_memory_eval.contracts.connection import forward_seed, provider_route
from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.contracts.validation import validate_manifest, validate_public_return
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.memory.imports import audit_import
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from agent_memory_eval.runner.tau2_autonomy import ARMS, compile_pilot


def summarize_pilot(root):
    root = Path(root)
    scan,state,experiment = read_experiment(root)
    blobs = BlobStore(root)
    if scan["tail_bytes"] or scan["incomplete_runs"] or scan["pending_requests"]:
        raise ContractError("Pilot evidence contains unfinished records")
    bound = state["autonomy_pilot"]
    texts = {name:blobs.get(ref) for name,ref in bound["source_refs"].items()}
    plan = compile_pilot(bound["requested"],source_texts=texts)
    external = plan["source_mode"] == "frozen_external"
    if bound.get("source_mode","new_collection") != plan["source_mode"]:
        raise ContractError("Pilot source mode changed")
    if any(bound[key] != plan[key] for key in ("orders","policies","worker_message_rendering")) or bound["connection_profile"] != plan["connection"]:
        raise ContractError("Pilot bindings differ from the approved plan")
    initial = blobs.get(scan["records"][0]["state_ref"])
    fixed = ("requested","orders","policies","connection_profile","source_refs","native_resolved",
             "model_catalog_entries","native_system_ref","business_tools_ref","implementation_ref",
             "worker_message_rendering","response_cache_enabled")
    if any(initial["autonomy_pilot"][key] != bound[key] for key in fixed) or bound["response_cache_enabled"] is not False:
        raise ContractError("Pilot controls changed after execution began")
    schedule = state["schedule"]
    if initial["schedule"] != schedule or initial["autonomy_pilot"]["requested"] != bound["requested"]:
        raise ContractError("Allocation or settings changed after execution began")
    if bound["runtime_status"] != "finished":
        raise ContractError("The pilot has not finished or explicitly skipped its allocation")
    rows = {row["run_id"]:row for row in schedule}
    collections = [row for row in schedule if row["stage"] == "collection"]
    if len(rows) != (9 if external else 10) or len(collections) != (0 if external else 1):
        raise ContractError("Pilot allocation has an unexpected source or probe count")
    for block,order in enumerate(plan["orders"],1):
        selected = [row for row in schedule if row["block_id"] == block]
        if [row["condition"] for row in selected] != order or [row["position"] for row in selected] != [1,2,3]:
            raise ContractError("Probe order differs from the blocked allocation")
        baseline = next(row["run_id"] for row in selected if row["condition"] == "no_memory")
        if any(row["baseline_run_id"] != baseline or row["stage"] != "probe" for row in selected):
            raise ContractError("Block pairing differs from allocation")
    skipped = state["skipped_runs"]
    if set(scan["starts"]) & set(skipped) or set(scan["starts"]) | set(skipped) != set(rows):
        raise ContractError("An allocated run is missing, duplicated or unexpectedly executed")
    native_system = blobs.get(bound["native_system_ref"])
    business_tools = blobs.get(bound["business_tools_ref"])
    config = plan["connection"]
    expected_spans,actual_spans,requests = {},{},{}
    total_cost = Decimal(0)
    http_calls = 0
    runs = {run:dict(row,started=False,grading_status="not_available",official_success=None,official_score=None,
        memory_stores=0,list_calls=0,get_calls=0,successful_list_calls=0,successful_get_calls=0,
        title_input_spans=0,body_input_spans=0,self_authored_input_spans=0,
        requests_by_role=Counter(),cost=Decimal(0),unknown_cost=False,tokens=0,actions=[],errors=[]) for run,row in rows.items()}
    outcomes,ends,finals = {},{},{}
    for event in scan["events"].values():
        if event["record_kind"] != "observed":
            raise ContractError("Pilot events must be observed")
        p,kind,run = event["payload"],event["type"],event["run_id"]
        if run is None:
            continue
        if run not in runs:
            raise ContractError("An event belongs to an unallocated run")
        result = runs[run]
        arm = rows[run]["condition"]
        if kind == "run.started":
            result["started"] = True
            assignment = state["assignments"][run]
            if assignment["policy"] != plan["policies"][arm] or event["condition_id"] != arm or assignment["block_id"] != rows[run]["block_id"]:
                raise ContractError("Run policy or block differs from allocation")
        elif kind == "model.request_started":
            actor = p["actor_role"]
            if actor not in {"worker","writer","user_simulator"} or (actor == "writer" and arm != "collection"):
                raise ContractError("Unexpected pilot actor")
            role = "worker" if actor == "writer" else actor
            wire = blobs.get(p["request_ref"])
            if wire["model"] != config[role]["model_id"] or wire["provider"] != provider_route(config,role):
                raise ContractError("Pilot model route changed")
            if any(wire.get(key) != value for key,value in config[role]["parameters"].items()):
                raise ContractError("Pilot model parameters changed")
            if not forward_seed(config,role) and "seed" in wire:
                raise ContractError("An unsupported simulator seed was forwarded")
            if role == "worker":
                policy = plan["policies"][arm]
                instruction = texts["collection_solve"] if arm == "collection" else texts["probe_memory"]
                system = native_system if arm == "no_memory" else native_system + "\n<memory_policy>\n" + instruction + "\n" + canonical(policy).decode() + "\n</memory_policy>"
                phase = "post_task_write" if actor == "writer" else "solve"
                names = ["memory_store"] if actor == "writer" else ["memory_list","memory_get"] if arm in {"empty_bank","memory_bank"} else []
                tools = ([] if actor == "writer" else business_tools) + memory_tools(names)
                if wire["messages"][0] != dict(role="system",content=system) or wire.get("tools",[]) != tools or event["phase"] != phase:
                    raise ContractError("Worker instructions, tool schemas or phase changed")
                if actor == "writer" and not any(message == dict(role="user",content=texts["post_task_write"]) for message in wire["messages"]):
                    raise ContractError("Writer did not receive the approved post-task instruction")
            elif any(tool["function"]["name"].startswith("memory_") for tool in wire.get("tools",[])):
                raise ContractError("Memory tools reached the simulator")
            origin = state["provenance"][p["request_id"]]
            catalog = blobs.get(origin["sources_ref"])
            manifest = blobs.get(p["input_manifest_ref"])
            spans = validate_manifest(root,manifest,event,catalog,scan["events"],origin["approved_templates"])
            if actor == "user_simulator" and spans:
                raise ContractError("Memory spans reached the simulator")
            for span in spans:
                source = catalog[span["source_id"]]
                field = "returned_event_id" if source["kind"] == "memory_return" else "response_event_id"
                expected_spans[span["span_id"]] = (run,dict(request_id=p["request_id"],manifest_span_id=span["span_id"],
                    source=dict(kind=source["kind"],**{field:source["producer_event_id"]}),request_ref=p["request_ref"],
                    **{key:span[key] for key in ("item_id","version_id","field","pointer","char_start","char_end","rendered_sha256")}))
            requests[p["request_id"]] = role
            result["requests_by_role"][actor] += 1
        elif kind == "model.request_finished":
            role = requests[p["request_id"]]
            http_calls += p["delivery_status"] != "not_submitted"
            result["unknown_cost"] |= p["cost_status"] == "unknown"
            response = blobs.get(p["response_ref"]) if p["response_ref"] else None
            if p["cost_status"] == "observed":
                if p["usage"] != response.get("usage") or p["cost_usd"] != response["usage"].get("cost"):
                    raise ContractError("Pilot usage/cost differs from provider response")
                result["cost"] += Decimal(str(p["cost_usd"]))
            if p["status"] == "response":
                model = bound["model_catalog_entries"][role]
                if response.get("model") not in {model["id"],model.get("canonical_slug")} or response.get("provider") != model["selected_endpoint"]["provider_name"]:
                    raise ContractError("Returned model/provider changed")
                result["tokens"] += (response.get("usage") or {}).get("total_tokens",0)
        elif kind == "memory.input_included":
            key = p["manifest_span_id"]
            if key in actual_spans:
                raise ContractError("Duplicate pilot memory exposure")
            actual_spans[key] = (run,p)
            if p["source"]["kind"] == "self_authored":
                result["self_authored_input_spans"] += 1
            else:
                result[p["field"]+"_input_spans"] += 1
        elif kind == "memory.stored":
            if arm != "collection" or event["phase"] != "post_task_write":
                raise ContractError("A read-only probe wrote memory")
            result["memory_stores"] += 1
        elif kind == "memory.returned":
            if arm == "no_memory" or p["bank_id"] != scan["starts"][run]["payload"]["bank_id"]:
                raise ContractError("Memory crossed a condition or bank")
            validate_public_return(blobs.get(p["result_ref"]),p)
            if p["operation"] in {"list","get"}:
                result[p["operation"]+"_calls"] += 1
                result["successful_"+p["operation"]+"_calls"] += p["status"] == "ok"
            bank = state["banks"][p["bank_id"]]
            if any(bank["start_heads"].get(item["item_id"]) != item["version_id"] for item in p["items"]):
                raise ContractError("Pilot reads escaped the starting snapshot")
        elif kind == "business.transition":
            if p.get("origin") == "tau2_native_final":
                finals[run] = p
            elif p.get("origin") == "tau2_memory_dispatch" and p["memory_only"]:
                if (p["native_step_before"],p["native_errors_before"]) != (p["native_step_after"],p["native_errors_after"]):
                    raise ContractError("Memory changed native progress/error counters")
            if "action" in p:
                result["actions"].append(dict(name=p["action"],arguments=p["arguments"]))
        elif kind == "runtime.error":
            result["errors"].append(p["code"])
        elif kind == "outcome.scored":
            if run in outcomes:
                raise ContractError("Duplicate pilot outcome")
            outcomes[run] = p
        elif kind == "run.ended":
            ends[run] = p
    if actual_spans != expected_spans or http_calls != bound["http_calls"]:
        raise ContractError("Pilot exposure coverage or HTTP count differs from evidence")
    sessions = [event["session_id"] for event in scan["starts"].values()]
    if len(set(sessions)) != len(sessions):
        raise ContractError("Pilot runs reused a session")
    for run,result in runs.items():
        if not result["started"]:
            result.update(status="skipped",skip_reason=skipped[run])
        else:
            outcome,start,end = outcomes[run],scan["starts"][run]["payload"],ends[run]
            result.update({key:outcome[key] for key in ("grading_status","official_success","official_score")})
            result.update(status="completed" if outcome["grading_status"] == "graded" and end["termination_reason"] != "post_task_error" else "error",
                termination_reason=end["termination_reason"],session_id=scan["starts"][run]["session_id"],
                native_steps=finals[run]["native_steps"],native_tool_errors=finals[run]["native_tool_errors"],
                final_db_hash=finals[run]["final_db_hash"])
            if outcome["grading_status"] == "graded":
                report = blobs.get(outcome["official_artifact_ref"])
                if report["reward_info"]["reward"] != result["official_score"] or result["official_success"] != (result["official_score"] == 1.0):
                    raise ContractError("Native outcome disagrees with report")
                result["native_duration_seconds"] = report["duration"]
                rejected = {m["id"] for m in report["messages"] if m["role"] == "tool" and m["execution_status"] == "rejected"}
                if any(call["name"].startswith("memory_") and call["id"] not in rejected for message in report["messages"] for call in message.get("tool_calls") or []):
                    raise ContractError("Memory entered the executed native trajectory")
            arm = result["condition"]
            if arm == "no_memory":
                if start["bank_id"] is not None or start["start_snapshot_id"] is not None or end["end_snapshot_id"] is not None or start["source_run_ids"]:
                    raise ContractError("No-memory run inherited a bank or source")
            else:
                bank = state["banks"][start["bank_id"]]
                parent = bound["source_snapshot"] if arm == "memory_bank" else bound["empty_snapshot"] if arm == "empty_bank" else None
                heads = state["snapshots"][parent]["heads"] if parent else {}
                if (not bank["sealed"] or bank["owner_run"] != run or bank["parent_snapshot"] != parent
                        or start["start_snapshot_id"] != parent or bank["start_heads"] != heads
                        or state["snapshots"][end["end_snapshot_id"]]["heads"] != bank["heads"]):
                    raise ContractError("Pilot bank ownership or snapshot isolation failed")
                if arm != "collection" and bank["heads"] != heads:
                    raise ContractError("Probe changed its starting bank")
                if arm == "empty_bank" and heads:
                    raise ContractError("Empty-bank control contains memory")
        result["requests_by_role"] = dict(result["requests_by_role"])
        total_cost += result["cost"]
        result["reported_cost_usd_decimal"] = str(result.pop("cost"))
    aggregates = {}
    for arm in ARMS:
        selected = [result for result in runs.values() if result["condition"] == arm]
        graded = [result for result in selected if result["grading_status"] == "graded"]
        successes = sum(result["official_success"] is True for result in graded)
        aggregates[arm] = dict(scheduled=len(selected),started=sum(result["started"] for result in selected),
            graded=len(graded),successes=successes,success_rate=successes/len(graded) if graded else None,
            runs_with_list_calls=sum(result["list_calls"] > 0 for result in selected),
            runs_with_get_calls=sum(result["get_calls"] > 0 for result in selected),
            runs_with_title_input=sum(result["title_input_spans"] > 0 for result in selected),
            runs_with_body_input=sum(result["body_input_spans"] > 0 for result in selected),
            reported_cost_usd_decimal=str(sum((Decimal(r["reported_cost_usd_decimal"]) for r in selected),Decimal(0))),
            unknown_cost_runs=sum(result["unknown_cost"] for result in selected))
    pairs = []
    for block in range(1,4):
        selected = {result["condition"]:result for result in runs.values() if result["block_id"] == block}
        for baseline,treated in (("no_memory","empty_bank"),("no_memory","memory_bank"),("empty_bank","memory_bank")):
            left,right = selected[baseline],selected[treated]
            left_success,right_success = left["official_success"],right["official_success"]
            outcome = "ungraded" if left_success is None or right_success is None else "both_success" if left_success and right_success else "both_failure" if not left_success and not right_success else "failure_to_success" if right_success else "success_to_failure"
            pairs.append(dict(block_id=block,baseline=baseline,treated=treated,baseline_run_id=left["run_id"],treated_run_id=right["run_id"],
                outcome=outcome,actions_changed=left["actions"] != right["actions"] if left["started"] and right["started"] else None))
    baselines = [result for result in runs.values() if result["condition"] == "no_memory" and result["official_success"] is not None]
    baseline_pairs = list(combinations(baselines,2))
    disagreements = sum(left["official_success"] != right["official_success"] for left,right in baseline_pairs)
    source = runs[collections[0]["run_id"]] if collections else None
    if external:
        original = audit_import(root,bound["requested"]["frozen_source"],bound["source_archive"])
        local = state["snapshots"][bound["source_snapshot"]]
        if local["heads"] != original["heads"] or local["order"] != original["order"] or bound["source_snapshot"] != bound["source_archive"]["local_snapshot_id"]:
            raise ContractError("Imported memory versions or order differ from the original snapshot")
        eligibility = "eligible"
    else:
        if ends[source["run_id"]]["end_snapshot_id"] != bound["source_snapshot"]:
            raise ContractError("Pilot source is not the frozen collection snapshot")
        source_heads = state["snapshots"][bound["source_snapshot"]]["heads"]
        eligibility = "eligible" if source["status"] == "completed" and source_heads else "source_empty" if source["status"] == "completed" else "source_incomplete"
    if eligibility != bound["source_eligibility"] or state["snapshots"][bound["empty_snapshot"]]["heads"]:
        raise ContractError("Source eligibility or empty control disagrees with evidence")
    report = dict(intent="autonomous_memory_pilot",scope=bound["requested"]["scope"],
        status="completed" if all(result["status"] == "completed" for result in runs.values()) else "incomplete",
        experiment_id=experiment,orders=plan["orders"],source_eligibility=bound["source_eligibility"],
        collection=source,conditions=aggregates,runs=[runs[row["run_id"]] for row in schedule if row["stage"] == "probe"],
        blocked_comparisons=pairs,baseline_repeat_variation=dict(graded_runs=len(baselines),pair_count=len(baseline_pairs),
            outcome_disagreements=disagreements,disagreement_fraction=disagreements/len(baseline_pairs) if baseline_pairs else None),
        http_calls=http_calls,reported_cost_usd_decimal=str(total_cost),
        unknown_cost=any(result["unknown_cost"] for result in runs.values()),reported_tokens=sum(result["tokens"] for result in runs.values()),
        inference="descriptive_only_single_task_single_bank_three_repetitions_no_significance_claim",
        log_sha256=digest((root/"log.jsonl").read_bytes()))
    if external:
        report.update(source_mode="frozen_external",source_origin=bound["requested"]["frozen_source"],
            source_generation_reported_cost_usd_decimal=bound["source_archive"]["source_generation_reported_cost_usd_decimal"],
            source_generation_unknown_cost=bound["source_archive"]["source_generation_unknown_cost"])
    return report


def audit_pilot(root):
    from agent_memory_eval.contracts.core import read_json
    root = Path(root)
    computed = summarize_pilot(root)
    saved = read_json((root/"pilot-summary.json").read_bytes())
    if saved != computed:
        raise ContractError("Saved pilot summary differs from immutable evidence")
    return dict(computed,audit_status="audit_passed",summary_sha256=digest((root/"pilot-summary.json").read_bytes()))
