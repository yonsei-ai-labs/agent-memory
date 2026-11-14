"""Audit allocated frozen-bank delivery runs; only descriptive outcomes are reported."""

from collections import Counter
from decimal import Decimal
from itertools import combinations
from pathlib import Path

from agent_memory_eval.contracts.connection import forward_seed, provider_route
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.contracts.validation import validate_manifest, validate_public_return
from agent_memory_eval.memory.imports import audit_import
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from agent_memory_eval.recording.provenance import exposure_payload
from agent_memory_eval.runner.tau2_delivery import compile_delivery


def summarize_delivery(root):
    root = Path(root)
    scan, state, experiment = read_experiment(root)
    if scan["tail_bytes"] or scan["incomplete_runs"] or scan["pending_requests"]:
        raise ContractError("Delivery evidence contains unfinished records")
    blobs = BlobStore(root)
    bound = state["delivery_comparison"]
    texts = {k: blobs.get(ref) for k, ref in bound["source_refs"].items()}
    plan = compile_delivery(bound["requested"], source_texts=texts)
    initial = blobs.get(scan["records"][0]["state_ref"])
    fixed = ("requested", "blocks", "policies", "source_refs", "benchmark_assets", "native_controls", "response_cache_enabled", "implementation_ref")
    if any(initial["delivery_comparison"][k] != bound[k] for k in fixed) or initial["schedule"] != state["schedule"]:
        raise ContractError("Delivery allocation or controls changed after execution began")
    if (bound["runtime_status"] != "finished" or bound["response_cache_enabled"] is not False
            or bound["blocks"] != plan["blocks"] or bound["policies"] != plan["policies"]):
        raise ContractError("Delivery bindings differ from the fixed plan")
    schedule = state["schedule"]
    expected = [dict(stage="probe", block_id=i, position=p, task_id=b["task_id"], source_group_id=b["source_group_id"],
                     repetition=b["repetition"], condition=c)
                for i, b in enumerate(plan["blocks"], 1) for p, c in enumerate(b["conditions"], 1)]
    if [{k: v for k, v in r.items() if k != "run_id"} for r in schedule] != expected:
        raise ContractError("Delivery schedule differs from the declared blocks")
    rows = {r["run_id"]: r for r in schedule}
    skipped = state["skipped_runs"]
    if len(rows) != len(schedule) or set(scan["starts"]) & set(skipped) or set(scan["starts"]) | set(skipped) != set(rows):
        raise ContractError("An allocated delivery run is missing, duplicated or unexpected")
    original = audit_import(root, bound["requested"]["frozen_source"], bound["source_archive"])
    snapshot = state["snapshots"][bound["source_snapshot"]]
    if (bound["source_snapshot"] != bound["source_archive"]["local_snapshot_id"] or snapshot["heads"] != original["heads"]
            or snapshot["order"] != original["order"] or state["snapshots"][bound["empty_snapshot"]]["heads"]):
        raise ContractError("The fixed memory source or empty control changed")
    results = {run: dict(row, started=False, status="skipped", grading_status="not_available", official_success=None,
        official_score=None, list_calls=0, get_calls=0, title_input_spans=0, body_input_spans=0, preload_prepared=False,
        actions=[], errors=[], policy_denials=[], requests_by_role=Counter(), reported_tokens=0, cost=Decimal(0), unknown_cost=False)
        for run, row in rows.items()}
    requests, expected_exposures, actual_exposures, outcomes, ends, finals = {}, {}, {}, {}, {}, {}
    http_calls = 0
    for event in scan["events"].values():
        if event["record_kind"] != "observed":
            raise ContractError("Delivery records must be observed")
        run, kind, p = event["run_id"], event["type"], event["payload"]
        if run is None:
            continue
        if run not in results:
            raise ContractError("Unallocated delivery event")
        r, row = results[run], rows[run]
        condition, task = row["condition"], row["task_id"]
        config, policy = plan["connections"][task], plan["policies"][condition]
        native = bound["native_controls"][task]
        if kind == "run.started":
            r["started"] = True
            assignment = state["assignments"][run]
            if (assignment["task_key"] != task or assignment["policy"] != policy or assignment["block_id"] != row["block_id"]
                    or event["condition_id"] != condition):
                raise ContractError("Delivery run task, condition or policy differs")
        elif kind == "memory.preloaded":
            if condition != "full_preload":
                raise ContractError("Unexpected preload in a non-preload condition")
            r["preload_prepared"] = True
        elif kind == "memory.stored":
            raise ContractError("A read-only delivery evaluation wrote memory")
        elif kind == "memory.returned":
            validate_public_return(blobs.get(p["result_ref"]), p)
            if condition == "no_memory" or (p["operation"] not in {"list", "get"} and p["status"] != "error") or p["bank_id"] != scan["starts"][run]["payload"]["bank_id"]:
                raise ContractError("Unexpected or foreign memory operation")
            bank = state["banks"][p["bank_id"]]
            if any(bank["start_heads"].get(item["item_id"]) != item["version_id"] for item in p["items"]):
                raise ContractError("Query escaped the fixed starting memory")
            if p["operation"] in {"list", "get"}:
                r[p["operation"] + "_calls"] += 1
        elif kind == "model.request_started":
            actor = p["actor_role"]
            if actor not in {"worker", "user_simulator"}:
                raise ContractError("Unexpected delivery actor")
            wire = blobs.get(p["request_ref"])
            if wire["model"] != config[actor]["model_id"] or wire["provider"] != provider_route(config, actor):
                raise ContractError("Delivery model or provider changed")
            if any(wire.get(k) != v for k, v in config[actor]["parameters"].items()) or (not forward_seed(config, actor) and "seed" in wire):
                raise ContractError("Delivery model parameters changed")
            origin = state["provenance"][p["request_id"]]
            catalog, manifest = blobs.get(origin["sources_ref"]), blobs.get(p["input_manifest_ref"])
            spans = validate_manifest(root, manifest, event, catalog, scan["events"], origin["approved_templates"])
            if actor == "worker":
                system = blobs.get(native["native_system_ref"])
                if condition != "no_memory":
                    system += "\n<memory_policy>\n" + texts["memory_instruction"] + "\n" + canonical(policy).decode() + "\n</memory_policy>"
                tools = blobs.get(native["business_tools_ref"]) + memory_tools(MemoryPolicy(**policy).tools("solve"))
                if wire["messages"][0] != dict(role="system", content=system) or wire.get("tools", []) != tools:
                    raise ContractError("Delivery instructions or tool schemas changed")
                snapshot_bindings = [b for b in manifest["bindings"] if catalog[b["source_id"]]["kind"] == "memory_snapshot"]
                if condition == "full_preload":
                    if len(snapshot_bindings) != 1 or snapshot_bindings[0]["pointer"] != "/messages/1":
                        raise ContractError("Full snapshot was not bound at its declared input position")
                    if sum(catalog[s["source_id"]]["kind"] == "memory_snapshot" for s in spans) != 2 * len(snapshot["heads"]):
                        raise ContractError("Full snapshot input coverage is incomplete")
                elif snapshot_bindings:
                    raise ContractError("Automatic snapshot input leaked into another condition")
            elif spans or any(t["function"]["name"].startswith("memory_") for t in wire.get("tools", [])):
                raise ContractError("Direct memory input reached the simulator")
            for span in spans:
                expected_exposures[span["span_id"]] = (run, exposure_payload(manifest, span, catalog[span["source_id"]]))
            requests[p["request_id"]] = (run, actor)
            r["requests_by_role"][actor] += 1
        elif kind == "memory.input_included":
            if p["manifest_span_id"] in actual_exposures:
                raise ContractError("Duplicate delivery exposure")
            actual_exposures[p["manifest_span_id"]] = (run, p)
            if p["field"] in {"title", "body"}:
                r[p["field"] + "_input_spans"] += 1
        elif kind == "model.request_finished":
            _, actor = requests[p["request_id"]]
            http_calls += p["delivery_status"] != "not_submitted"
            r["unknown_cost"] |= p["cost_status"] == "unknown"
            response = blobs.get(p["response_ref"]) if p["response_ref"] else None
            if p["cost_status"] == "observed":
                if p["usage"] != response.get("usage") or p["cost_usd"] != response["usage"].get("cost"):
                    raise ContractError("Delivery usage or cost differs from provider evidence")
                r["cost"] += Decimal(str(p["cost_usd"]))
            if p["status"] == "response":
                model = native["model_catalog_entries"][actor]
                if response.get("model") not in {model["id"], model.get("canonical_slug")} or response.get("provider") != model["selected_endpoint"]["provider_name"]:
                    raise ContractError("Delivery response model or provider changed")
                r["reported_tokens"] += (response.get("usage") or {}).get("total_tokens", 0)
        elif kind == "business.transition":
            if p.get("origin") == "tau2_native_final":
                finals[run] = p
            if "action" in p:
                r["actions"].append(dict(name=p["action"], arguments=p["arguments"]))
            if p.get("memory_only") and (p["native_step_before"], p["native_errors_before"]) != (p["native_step_after"], p["native_errors_after"]):
                raise ContractError("A memory round changed native steps or errors")
        elif kind == "outcome.scored":
            if run in outcomes:
                raise ContractError("Duplicate delivery outcome")
            outcomes[run] = p
        elif kind == "run.ended":
            ends[run] = p
        elif kind == "runtime.error":
            r["errors"].append(p["code"])
        elif kind == "policy.denied":
            r["policy_denials"].append(p["code"])
    if actual_exposures != expected_exposures or http_calls != bound["http_calls"]:
        raise ContractError("Delivery exposure or HTTP totals differ")
    for run, r in results.items():
        if not r["started"]:
            r["skip_reason"] = skipped[run]
        else:
            r.update({k: outcomes[run][k] for k in ("grading_status", "official_success", "official_score")})
            r.update(status="completed" if r["grading_status"] == "graded" else "error",
                session_id=scan["starts"][run]["session_id"], termination_reason=ends[run]["termination_reason"],
                **{k: finals[run][k] for k in ("final_db_hash", "native_steps", "native_tool_errors")})
            start, end = scan["starts"][run]["payload"], ends[run]
            if r["condition"] == "no_memory":
                if start["bank_id"] or start["start_snapshot_id"] or end["end_snapshot_id"] or start["source_run_ids"]:
                    raise ContractError("No-memory evaluation inherited memory")
            else:
                bank = state["banks"][start["bank_id"]]
                parent = bound["empty_snapshot"] if r["condition"] == "empty_bank" else bound["source_snapshot"]
                heads = state["snapshots"][parent]["heads"]
                if (not bank["sealed"] or bank["owner_run"] != run or bank["parent_snapshot"] != parent or start["start_snapshot_id"] != parent
                        or bank["policy"] != plan["policies"][r["condition"]] or bank["heads"] != heads or bank["start_heads"] != heads
                        or state["snapshots"][end["end_snapshot_id"]]["heads"] != heads):
                    raise ContractError("Delivery bank ownership, versions or isolation failed")
            if r["grading_status"] == "graded":
                report = blobs.get(outcomes[run]["official_artifact_ref"])
                if report["reward_info"]["reward"] != r["official_score"] or r["official_success"] != (r["official_score"] == 1.0):
                    raise ContractError("Native delivery outcome disagrees with its report")
        r["requests_by_role"] = dict(r["requests_by_role"])
        r["reported_cost_usd_decimal"] = str(r.pop("cost"))
    aggregates = []
    for task in plan["profile"]["tasks"]:
        for condition in plan["profile"]["conditions"]:
            selected = [r for r in results.values() if r["task_id"] == task["id"] and r["condition"] == condition]
            graded = [r for r in selected if r["grading_status"] == "graded"]
            aggregates.append(dict(task_id=task["id"], source_group_id=task["source_group_id"], condition=condition,
                scheduled=len(selected), started=sum(r["started"] for r in selected), graded=len(graded),
                successes=sum(r["official_success"] is True for r in graded),
                success_rate=sum(r["official_success"] is True for r in graded) / len(graded) if graded else None,
                runs_with_title_input=sum(r["title_input_spans"] > 0 for r in selected),
                runs_with_body_input=sum(r["body_input_spans"] > 0 for r in selected),
                reported_cost_usd_decimal=str(sum((Decimal(r["reported_cost_usd_decimal"]) for r in selected), Decimal(0)))))
    primary = []
    for block_id, block in enumerate(plan["blocks"], 1):
        selected = {r["condition"]: r for r in results.values() if r["block_id"] == block_id}
        comparison = plan["profile"]["primary_comparison"]
        left, right = selected[comparison["baseline"]], selected[comparison["treated"]]
        primary.append(dict(block_id=block_id, task_id=block["task_id"], source_group_id=block["source_group_id"], repetition=block["repetition"],
            baseline_run_id=left["run_id"], treated_run_id=right["run_id"],
            official_success_difference=int(right["official_success"]) - int(left["official_success"])
                if left["official_success"] is not None and right["official_success"] is not None else None))
    baseline_repeats = []
    if "no_memory" in plan["profile"]["conditions"]:
        for task in plan["profile"]["tasks"]:
            baseline = [r for r in results.values() if r["task_id"] == task["id"] and r["condition"] == "no_memory"
                        and r["official_success"] is not None]
            pairs = list(combinations(baseline, 2))
            baseline_repeats.append(dict(task_id=task["id"], source_group_id=task["source_group_id"], graded_runs=len(baseline),
                pair_count=len(pairs), outcome_disagreements=sum(a["official_success"] != b["official_success"] for a, b in pairs)))
    return dict(format="agent-memory-tau2-delivery-summary/v1", experiment_id=experiment,
        status="completed" if all(r["status"] == "completed" for r in results.values()) else "incomplete",
        inference="descriptive_fixed_bank_tasks_and_repeats_share_sources_no_independent_pairs_claim",
        runs=[results[r["run_id"]] for r in schedule], task_conditions=aggregates, primary_comparisons=primary,
        baseline_repeat_variation=baseline_repeats,
        http_calls=http_calls, reported_tokens=sum(r["reported_tokens"] for r in results.values()),
        reported_cost_usd_decimal=str(sum((Decimal(r["reported_cost_usd_decimal"]) for r in results.values()), Decimal(0))),
        unknown_cost=any(r["unknown_cost"] for r in results.values()),
        source_generation_reported_cost_usd_decimal=bound["source_archive"]["source_generation_reported_cost_usd_decimal"],
        source_generation_unknown_cost=bound["source_archive"]["source_generation_unknown_cost"],
        log_sha256=digest((root / "log.jsonl").read_bytes()))


def audit_delivery(root):
    root = Path(root)
    summary = summarize_delivery(root)
    saved = read_json((root / "delivery-summary.json").read_bytes())
    if saved != summary:
        raise ContractError("Saved delivery summary differs from original evidence")
    return dict(summary, audit_status="audit_passed", summary_sha256=digest((root / "delivery-summary.json").read_bytes()))
