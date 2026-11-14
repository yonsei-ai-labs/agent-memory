"""Read-only native memory connection audit, including returned versus included text."""

from collections import Counter, defaultdict
from decimal import Decimal
import json
from pathlib import Path

from agent_memory_eval.contracts.connection import forward_seed, provider_route, validate_profile
from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.contracts.validation import validate_manifest, validate_public_return
from agent_memory_eval.recording.journal import BlobStore, read_experiment


def audit_memory_check(root):
    root = Path(root)
    scan, state, experiment = read_experiment(root)
    if scan["tail_bytes"] or scan["incomplete_runs"] or scan["pending_requests"]:
        raise ContractError("Memory connection evidence is incomplete")
    check = state["memory_connection_check"]
    if check["requested"]["intent"] != "memory_connection_check" or check["requested"]["mode"] != "guided_check":
        raise ContractError("Expected a guided memory connection profile")
    config, blobs = check["connection_profile"], BlobStore(root)
    validate_profile(config)
    profile_text = blobs.get(check["connection_profile_source_ref"])
    if digest(profile_text.encode()) != check["requested"]["connection_profile_ref"]["sha256"] or json.loads(profile_text) != config:
        raise ContractError("Saved connection profile differs from the approved source")
    for name,ref in check["instruction_source_refs"].items():
        text = blobs.get(ref)
        if digest(text.encode()) != check["requested"]["instructions"][name]["sha256"] or text != check["instructions"][name]:
            raise ContractError("Saved instruction differs from the approved source")
    expected, actual, requests = {}, {}, {}
    counts, stores, returns, outcomes, ends, native_counts = defaultdict(Counter), Counter(), defaultdict(Counter), {}, {}, {}
    cost, unknown, tokens, responses = Decimal(0), False, 0, 0
    business_calls, successful_reads = Counter(), defaultdict(set)
    for event in scan["events"].values():
        if event["record_kind"] != "observed":
            raise ContractError("Native memory evidence must be observed")
        payload, kind, run = event["payload"], event["type"], event["run_id"]
        if kind == "model.request_started":
            role = payload["actor_role"]
            if role not in {"worker", "writer", "user_simulator"}:
                raise ContractError("Unexpected model recipient")
            model_role = "worker" if role == "writer" else role
            wire = blobs.get(payload["request_ref"])
            if wire["model"] != config[model_role]["model_id"] or wire["provider"] != provider_route(config, model_role):
                raise ContractError("Model route differs from the profile")
            for key, value in config[model_role]["parameters"].items():
                if wire.get(key) != value:
                    raise ContractError("Model parameter differs from the profile")
            if not forward_seed(config, model_role) and "seed" in wire:
                raise ContractError("Unsupported native seed was forwarded")
            tools = [tool["function"]["name"] for tool in wire.get("tools", [])]
            if role == "user_simulator" and any(name.startswith("memory_") for name in tools):
                raise ContractError("Memory tools leaked to the user simulator")
            if role == "writer" and (event["phase"] != "post_task_write" or any(not name.startswith("memory_") for name in tools)):
                raise ContractError("Writer received business tools or the wrong phase")
            manifest = blobs.get(payload["input_manifest_ref"])
            origin = state["provenance"][payload["request_id"]]
            catalog = blobs.get(origin["sources_ref"])
            spans = validate_manifest(root, manifest, event, catalog, scan["events"], origin["approved_templates"])
            if role == "user_simulator" and spans:
                raise ContractError("Internal memory was forwarded to the user simulator")
            for span in spans:
                source = catalog[span["source_id"]]
                key = "returned_event_id" if source["kind"] == "memory_return" else "response_event_id"
                expected[span["span_id"]] = (run, dict(request_id=payload["request_id"],
                    manifest_span_id=span["span_id"], source=dict(kind=source["kind"], **{key:source["producer_event_id"]}),
                    request_ref=payload["request_ref"], **{field:span[field] for field in
                    ("item_id","version_id","field","pointer","char_start","char_end","rendered_sha256")}))
            requests[payload["request_id"]] = model_role
            counts[run][role] += 1
        elif kind == "model.request_finished":
            model_role = requests[payload["request_id"]]
            unknown |= payload["cost_status"] == "unknown"
            if payload["status"] == "response":
                response = blobs.get(payload["response_ref"])
                model = check["model_catalog_entries"][model_role]
                if response.get("model") not in {model["id"],model.get("canonical_slug")} or response.get("provider") != model["selected_endpoint"]["provider_name"]:
                    raise ContractError("Returned model/provider differs from the declared role")
                if payload["usage"] != response.get("usage"):
                    raise ContractError("Usage differs from the original response")
                if payload["cost_status"] == "observed":
                    if payload["cost_usd"] != response["usage"].get("cost"):
                        raise ContractError("Cost differs from the original response")
                    cost += Decimal(str(payload["cost_usd"]))
                tokens += (response.get("usage") or {}).get("total_tokens",0)
                responses += 1
        elif kind == "memory.input_included":
            key = payload["manifest_span_id"]
            if key in actual:
                raise ContractError("Duplicate memory input event")
            actual[key] = (run, payload)
        elif kind in {"memory.stored", "memory.returned"}:
            start = scan["starts"][run]
            if payload["bank_id"] != start["payload"]["bank_id"]:
                raise ContractError("Memory operation crossed banks")
            if kind == "memory.stored":
                stores[run] += 1
            else:
                wire = blobs.get(payload["result_ref"])
                validate_public_return(wire,payload)
                bank = state["banks"][payload["bank_id"]]
                if not bank["policy"]["same_episode_read"] and any(bank["start_heads"].get(item["item_id"]) != item["version_id"] for item in payload["items"]):
                    raise ContractError("Memory read escaped the fixed starting view")
                returns[run][payload["operation"]] += 1
                if payload["status"] == "ok" and payload["items"]:
                    successful_reads[run].add(payload["operation"])
        elif kind == "business.transition":
            if payload.get("origin") == "tau2_memory_dispatch" and payload["memory_only"]:
                if (payload["native_step_before"],payload["native_errors_before"]) != (payload["native_step_after"],payload["native_errors_after"]):
                    raise ContractError("A memory-only round changed native counters")
            if payload.get("origin") == "tau2_native_final":
                native_counts[run] = {key:payload[key] for key in ("native_steps","native_tool_errors")}
            if "action" in payload:
                business_calls[run] += 1
        elif kind == "outcome.scored":
            if run in outcomes:
                raise ContractError("Duplicate native outcome")
            outcomes[run] = payload
        elif kind == "run.ended":
            ends[run] = payload
    if actual != expected:
        raise ContractError("Memory inclusion events differ from request manifests")
    summary = json.loads((root/"memory-summary.json").read_text())
    if summary["intent"] != "memory_connection_check" or summary["mode"] != "guided_check" or summary["policy_effect_measured"] is not False:
        raise ContractError("Summary mislabels the diagnostic scope")
    if abs(Decimal(str(summary["reported_cost_usd"]))-cost) > Decimal("0.000000001") or summary["unknown_cost"] != unknown:
        raise ContractError("Summary cost differs from observed records")
    scheduled = {row["run_id"] for row in state["schedule"]}
    if set(scan["starts"]) | set(state.get("skipped_runs",{})) != scheduled:
        raise ContractError("Scheduled runs are missing from execution and skip records")
    for phase in ("collection","probe"):
        episode = summary[phase]
        if episode is None:
            if phase != "probe" or len(state.get("skipped_runs",{})) != 1:
                raise ContractError("Missing episode has no explicit skip record")
            if summary["probe_skipped_reason"] not in state["skipped_runs"].values():
                raise ContractError("Probe skip reason differs from the recorded reason")
            continue
        run = episode["run_id"]
        if state["assignments"][run]["stage"] != phase:
            raise ContractError("Summary changed the scheduled stage")
        outcome = outcomes[run]
        for key in ("grading_status","official_score","official_success"):
            if episode[key] != outcome[key]:
                raise ContractError("Summary outcome differs from the frozen native outcome")
        if episode["report_ref"] != outcome["official_artifact_ref"]:
            raise ContractError("Summary report differs from the outcome artifact")
        if episode["grading_status"] == "graded":
            report = blobs.get(episode["report_ref"])
            if report["reward_info"]["reward"] != episode["official_score"]:
                raise ContractError("Native grading report disagrees with the score")
            rejected = {m["id"] for m in report["messages"] if m["role"] == "tool" and m["execution_status"] == "rejected"}
            if any(call["id"] not in rejected and call["name"].startswith("memory_")
                   for message in report["messages"] for call in message.get("tool_calls") or []):
                raise ContractError("Memory calls leaked into the executed native trajectory")
        bank = state["banks"][episode["bank_id"]]
        if bank["owner_run"] != run or not bank["sealed"] or ends[run]["end_snapshot_id"] != episode["snapshot_id"]:
            raise ContractError("Bank ownership, sealing or end snapshot disagrees")
        if dict(counts[run]) != episode["requests_by_role"] or stores[run] != episode["memory_stores"] or dict(returns[run]) != episode["memory_returns"]:
            raise ContractError("Summary counts differ from the journal")
        if episode["memory_input_spans"] != sum(owner == run for owner,_ in actual.values()):
            raise ContractError("Summary memory exposure count differs from the journal")
        completed = outcome["grading_status"] == "graded" and ends[run]["termination_reason"] != "post_task_error"
        if episode["status"] != ("completed" if completed else "error"):
            raise ContractError("Summary completion differs from the recorded termination")
        if any(episode[key] != value for key,value in native_counts[run].items()):
            raise ContractError("Native counters differ from the frozen state")
    if summary["probe"] is not None:
        first, second = summary["collection"], summary["probe"]
        first_snapshot = state["snapshots"][first["snapshot_id"]]
        bank = state["banks"][second["bank_id"]]
        if bank["parent_snapshot"] != first["snapshot_id"] or bank["start_heads"] != first_snapshot["heads"] or first["bank_id"] == second["bank_id"]:
            raise ContractError("Probe did not fork the preserved collection snapshot")
        if scan["starts"][first["run_id"]]["session_id"] == scan["starts"][second["run_id"]]["session_id"]:
            raise ContractError("Collection and probe reused a session")
    second = summary["probe"]
    roundtrip = bool(stores[summary["collection"]["run_id"]] and second and second["status"] == "completed"
        and {"list","get"} <= successful_reads[second["run_id"]]
        and any(owner == second["run_id"] for owner,_ in actual.values()))
    if summary["memory_roundtrip"] != roundtrip or summary["status"] != ("completed" if roundtrip else "error"):
        raise ContractError("Summary roundtrip differs from memory stores, reads and exposure")
    return dict(intent="memory_connection_check", mode="guided_check", status="audit_passed",
        experiment_id=experiment, run_count=len(scan["starts"]), requests=sum(sum(c.values()) for c in counts.values()),
        responses=responses, reported_cost_usd_decimal=str(cost), unknown_cost=unknown, reported_tokens=tokens,
        memory_stores=sum(stores.values()), memory_return_calls=sum(sum(c.values()) for c in returns.values()),
        retrieved_input_spans=sum(p[1]["source"]["kind"] == "memory_return" for p in actual.values()),
        self_authored_input_spans=sum(p[1]["source"]["kind"] == "self_authored" for p in actual.values()),
        business_calls=dict(business_calls), memory_roundtrip=summary["memory_roundtrip"], policy_effect_measured=False,
        log_sha256=digest((root/"log.jsonl").read_bytes()), summary_sha256=digest((root/"memory-summary.json").read_bytes()))
