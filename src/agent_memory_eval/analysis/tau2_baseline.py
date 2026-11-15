"""Recompute no-memory screening results from committed evidence, without network or writes."""

from collections import Counter
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.connection import forward_seed, provider_route
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import validate_manifest
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path
from agent_memory_eval.runner.tau2_baseline import allocations, compile_baseline, resolved_connection, select_tasks


def require(condition, message):
    if not condition:
        raise ContractError(message)


def summarize_baseline(root):
    root = Path(root)
    scan, state, experiment = read_experiment(root)
    require(bool(scan["records"]), "Baseline evidence is empty")
    require(not (scan["tail_bytes"] or scan["incomplete_runs"] or scan["pending_requests"]), "Baseline evidence is incomplete")
    require(state.get("baseline_screening", {}).get("runtime_status") == "finished", "Baseline batch has no completion record")
    blobs = BlobStore(root)
    control_ref = state["baseline_screening"]["control_ref"]
    control = blobs.get(control_ref)
    require(control["response_cache_enabled"] is False, "Full response cache was enabled")
    plan = compile_baseline(control["profile"], connection_text=control["connection_text"])
    assets = {}
    for name, asset in control["assets"].items():
        text = blobs.get(asset["text_ref"])
        require(digest(text.encode()) == asset["sha256"], "Saved benchmark source changed")
        assets[name] = text
    folder = "data/tau2/domains/" + plan["connection"]["benchmark"]["domain"] + "/"
    tasks = {t["id"]: t for t in read_json(assets[folder + "tasks.json"].encode())}
    split = read_json(assets[folder + "split_tasks.json"].encode())
    ids = select_tasks(plan["profile"], split, tasks)
    schedule = control["schedule"]
    require([{k: v for k, v in r.items() if k not in {"run_id", "session_id"}} for r in schedule]
            == allocations(plan["profile"], ids), "Baseline allocation differs from the frozen schedule")
    by_run = {r["run_id"]: r for r in schedule}
    require(len(by_run) == len(schedule) == len({r["session_id"] for r in schedule}), "Duplicate baseline run/session allocation")
    origins, state_refs = {}, {}
    for record in scan["records"]:
        head = blobs.get(record["state_ref"])
        require(head["baseline_screening"]["control_ref"] == control_ref, "Batch control changed within the journal")
        require(all(not head[key] for key in ("banks", "versions", "snapshots", "cursors", "return_bytes", "assignments")), "Baseline acquired memory state")
        state_refs[record["state_ref"]["sha256"]] = record["state_ref"]
        for event in record["events"]:
            if event["type"] == "model.request_started":
                request_id = event["payload"]["request_id"]
                require(request_id in head["provenance"], "Request has no registry in its original commit")
                origins[request_id] = head["provenance"][request_id]
    events = list(scan["events"].values())
    for event in events:
        require(event["record_kind"] == "observed" and not event["type"].startswith("memory."), "Unexpected baseline event kind")
        require(event["run_id"] is None or event["run_id"] in by_run, "Unexpected baseline run")
    starts = [e for e in events if e["type"] == "run.started"]
    require([e["run_id"] for e in starts] == [r["run_id"] for r in schedule[:len(starts)]], "Started runs differ from allocation order")
    finish = [e for e in events if e["payload"].get("origin") == "baseline.batch_finished"]
    require(len(finish) == 1 and finish[0] is events[-1], "Missing or misplaced batch completion")
    skipped_events = [e for e in events if e["payload"].get("origin") == "baseline.allocation_skipped"]
    skipped = {e["payload"]["run_id"]: e["payload"] for e in skipped_events}
    require(len(skipped) == len(skipped_events), "Duplicate skipped allocation")
    require(list(skipped) == [r["run_id"] for r in schedule[len(starts):]], "Missing or extra skipped allocation")
    require(not skipped or finish[0]["payload"]["stop_reason"], "Skipped allocations have no batch stop reason")
    require(all(e["reason"] == finish[0]["payload"]["stop_reason"] for e in skipped.values()), "Skipped reason differs from batch stop")
    rows = []
    total_cost, total_tokens, http_calls = Decimal(0), 0, 0
    role_counts, statuses, models, providers = Counter(), Counter(), Counter(), Counter()
    role_usage = {role: dict(http_calls=0, cost=Decimal(0), reported_tokens=0, unknown_cost=False)
                  for role in ("worker", "user_simulator")}
    unknown = False
    for row in schedule:
        run_id = row["run_id"]
        if run_id in skipped:
            entry = skipped[run_id]
            require(entry["allocation_id"] == row["allocation_id"] and bool(entry["reason"]), "Skipped row differs from allocation")
            rows.append(dict(row, status="skipped", skip_reason=entry["reason"], grading_status="not_started",
                official_success=None, official_score=None, requests_by_role={}, http_calls=0,
                reported_cost_usd_decimal="0", reported_tokens=0, unknown_cost=False, bounded=False, usage_by_role={}))
            continue
        run_events = [e for e in events if e["run_id"] == run_id]
        start = run_events[0]
        require(start["type"] == "run.started" and start["session_id"] == row["session_id"] and
            start["condition_id"] == "no_memory" and start["payload"]["fresh_session"] and
            start["payload"]["bank_id"] is None and start["payload"]["start_snapshot_id"] is None and
            start["payload"]["source_run_ids"] == [], "Baseline run inherited state or differs from allocation")
        ends = [e for e in run_events if e["type"] == "run.ended"]
        outcomes = [e["payload"] for e in run_events if e["type"] == "outcome.scored"]
        results = [e["payload"] for e in run_events if e["payload"].get("origin") == "baseline.run_result"]
        controls = [e["payload"] for e in run_events if e["payload"].get("origin") == "baseline.run_control"]
        require(len(ends) == len(outcomes) == len(results) == 1, "Baseline run needs one end, outcome and result")
        end, outcome, result = ends[0]["payload"], outcomes[0], results[0]
        config = resolved_connection(plan, row)
        meta = blobs.get(controls[0]["control_ref"]) if len(controls) == 1 else None
        require(len(controls) <= 1, "Duplicate native run control")
        if meta is not None:
            require(meta["config"] == config and blobs.get(meta["task_ref"])["id"] == row["task_id"], "Native run control differs from allocation")
            native = meta["native_resolved"]
            require(native["seed"] == row["native_seed"] and native["task_ids"] == [row["task_id"]] and
                native["max_steps"] == config["benchmark"]["max_steps"] and native["max_errors"] == config["benchmark"]["max_errors"],
                "Native limits or seed differ from declared settings")
        requests, run_roles, run_cost, run_tokens, run_calls, run_unknown = {}, Counter(), Decimal(0), 0, 0, False
        for event in run_events:
            payload = event["payload"]
            if event["type"] == "model.request_started":
                require(meta is not None, "Request preceded native preparation")
                request_id, actor = payload["request_id"], payload["actor_role"]
                require(actor in {"worker", "user_simulator"}, "Unexpected baseline model role")
                origin = origins[request_id]
                manifest = blobs.get(payload["input_manifest_ref"])
                require(not validate_manifest(root, manifest, event, blobs.get(origin["sources_ref"]),
                    scan["events"], origin["approved_templates"]), "Baseline contains memory input spans")
                wire = blobs.get(payload["request_ref"])
                require(wire["messages"][0] == dict(role="system", content=meta["systems"][actor]) and
                    not any(m["role"] == "system" for m in wire["messages"][1:]), "Native prompt was changed or supplemented")
                parameters = wire_parameters(config, actor)
                parameters.update(model=config[actor]["model_id"], provider=provider_route(config, actor), stream=False)
                tools = meta["tools"][actor]
                if tools:
                    parameters.update(tools=tools, tool_choice="auto")
                require({k: v for k, v in wire.items() if k != "messages"} == parameters, "Baseline wire differs from native settings/tools")
                require(not any(t["function"]["name"].startswith("memory_") for t in tools), "Baseline exposed memory tools")
                requests[request_id] = actor
                run_roles[actor] += 1
            elif event["type"] == "model.request_finished":
                actor = requests[payload["request_id"]]
                statuses[payload["status"]] += 1
                run_calls += payload["delivery_status"] != "not_submitted"
                run_unknown |= payload["cost_status"] == "unknown"
                role_usage[actor]["http_calls"] += payload["delivery_status"] != "not_submitted"
                role_usage[actor]["unknown_cost"] |= payload["cost_status"] == "unknown"
                usage_tokens = (payload.get("usage") or {}).get("total_tokens", 0)
                run_tokens += usage_tokens
                role_usage[actor]["reported_tokens"] += usage_tokens
                if payload["cost_status"] == "observed":
                    run_cost += Decimal(str(payload["cost_usd"]))
                    role_usage[actor]["cost"] += Decimal(str(payload["cost_usd"]))
                if payload["status"] == "response":
                    response = blobs.get(payload["response_ref"])
                    model = control["models"][actor]
                    endpoint = model["selected_endpoint"]
                    require(endpoint["tag"] == provider_route(config, actor)["only"][0] and
                        response.get("provider") == endpoint["provider_name"] and
                        response.get("model") in {model["id"], model.get("canonical_slug")}, "Response identity differs from pinned role")
                    require(payload["usage"] == response.get("usage"), "Recorded usage differs from provider response")
                    if payload["cost_status"] == "observed":
                        require(payload["cost_usd"] == response["usage"].get("cost"), "Recorded cost differs from provider response")
                    models[response["model"]] += 1
                    providers[response["provider"]] += 1
        require(result["http_calls"] == run_calls and result["unknown_cost"] == run_unknown, "Run HTTP/unknown cost differs from request evidence")
        require(abs(Decimal(str(result["reported_cost_usd"])) - run_cost) <= Decimal("0.000000001"), "Run cost differs from provider records")
        require(end["grading_status"] == outcome["grading_status"] and end["end_snapshot_id"] is None, "Run end differs from grading")
        errors = [e["payload"]["code"] for e in run_events if e["type"] == "runtime.error"]
        require(result["error_code"] == (errors[-1] if errors else None), "Run error differs from event evidence")
        if outcome["grading_status"] == "graded":
            report = blobs.get(result["report_ref"])
            require(outcome["official_artifact_ref"] == result["report_ref"] and report["task_id"] == row["task_id"] and
                report["seed"] == row["native_seed"] and report["termination_reason"] == end["termination_reason"] and
                report["reward_info"]["reward"] == outcome["official_score"] and
                outcome["official_success"] == (outcome["official_score"] == 1.0), "Outcome differs from native artifact")
        else:
            require(outcome["official_score"] is None and outcome["official_success"] is None and
                    outcome["official_artifact_ref"] is None, "Ungraded run invented a score")
        bounded = end["termination_reason"] in {"max_steps", "timeout", "too_many_errors", "context_window_exceeded"}
        bounded |= bool(result["error_code"] and ("LIMIT" in result["error_code"] or result["error_code"] == "INPUT_SIZE_LIMIT"))
        rows.append(dict(row, status="completed" if outcome["grading_status"] == "graded" else "error",
            grading_status=outcome["grading_status"], official_success=outcome["official_success"], official_score=outcome["official_score"],
            termination_reason=end["termination_reason"], error_code=result["error_code"],
            native_steps=result["native_steps"], native_tool_errors=result["native_tool_errors"], final_db_hash=result["final_db_hash"],
            report_ref=result["report_ref"], requests_by_role=dict(run_roles), http_calls=run_calls,
            reported_cost_usd_decimal=str(run_cost), reported_tokens=run_tokens, unknown_cost=run_unknown, bounded=bounded))
        total_cost += run_cost
        total_tokens += run_tokens
        http_calls += run_calls
        role_counts.update(run_roles)
        unknown |= run_unknown
    require(finish[0]["payload"]["http_calls"] == http_calls, "Batch HTTP count differs from request evidence")
    counts = dict(planned=len(rows), started=len(starts), graded=sum(r["grading_status"] == "graded" for r in rows),
        successful=sum(r["official_success"] is True for r in rows), skipped=len(skipped),
        ungraded=sum(r["status"] == "error" for r in rows), bounded=sum(r["bounded"] for r in rows))
    return dict(intent="no_memory_screening", status="completed" if counts["graded"] == counts["planned"] else "partial",
        experiment_id=experiment, native_revision=plan["connection"]["benchmark"]["source_revision"], domain=plan["connection"]["benchmark"]["domain"],
        counts=counts, stop_reason=finish[0]["payload"]["stop_reason"], requests_by_role=dict(role_counts), completion_statuses=dict(statuses),
        http_calls=http_calls, reported_cost_usd_decimal=str(total_cost), reported_tokens=total_tokens, unknown_cost=unknown,
        usage_by_role={role: dict(requests=role_counts[role], http_calls=usage["http_calls"], reported_tokens=usage["reported_tokens"],
            reported_cost_usd_decimal=str(usage["cost"]), unknown_cost=usage["unknown_cost"]) for role, usage in role_usage.items()},
        returned_models=dict(models), returned_providers=dict(providers), memory_enabled=False,
        state_storage=dict(unique_committed_state_blobs=len(state_refs),
            bytes=sum(safe_path(root, ref["path"]).stat().st_size for ref in state_refs.values())), runs=rows)


def wire_parameters(config, actor):
    parameters = dict(config[actor]["parameters"])
    if forward_seed(config, actor):
        parameters["seed"] = config["benchmark"]["seed"]
    else:
        parameters.pop("seed", None)
    return parameters


def audit_baseline(root):
    root = Path(root)
    summary = summarize_baseline(root)
    saved = read_json((root / "baseline-summary.json").read_bytes())
    require(canonical(summary) == canonical(saved), "Baseline summary differs from committed evidence")
    return dict(status="audit_passed", **{k: v for k, v in summary.items() if k != "status"},
        log_sha256=digest((root / "log.jsonl").read_bytes()), summary_sha256=digest((root / "baseline-summary.json").read_bytes()))
