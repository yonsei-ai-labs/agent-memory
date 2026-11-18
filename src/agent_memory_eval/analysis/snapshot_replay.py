"""Reconstruct one-response snapshot runs from local evidence, without the original source."""

from collections import Counter
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest
from agent_memory_eval.contracts.snapshot_replay import SCOPE, replay_response_error, validate_replay_record
from agent_memory_eval.contracts.validation import validate_preload
from agent_memory_eval.memory.preparation import snapshot_replay_input
from agent_memory_eval.recording.experience import ReplayContextReader
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from agent_memory_eval.recording.provenance import public_assistant_message


def audit_snapshot_replays(root, *, evidence=None):
    root = Path(root)
    evidence = evidence or read_experiment(root)
    scan, state, experiment = evidence
    common = audit_requests(root, evidence=evidence)
    context_reader = ReplayContextReader()
    blobs, configs, rows = BlobStore(root), state.get("snapshot_replays", {}), []
    if not configs:
        raise ContractError("No snapshot replay runs are recorded")
    result_events = [e for e in scan["events"].values() if e["type"] == "business.transition"
                     and e["payload"].get("origin") == "snapshot_replay.result"]
    if len(result_events) != len(configs) or {e["run_id"] for e in result_events} != set(configs):
        raise ContractError("Replay configurations and result producers differ")
    start_states, request_origins = {}, {}
    for record in scan["records"]:
        starts = [e for e in record["events"] if e["type"] == "run.started" and e["run_id"] in configs]
        requests = [e for e in record["events"] if e["type"] == "model.request_started" and e["run_id"] in configs]
        if starts or requests:
            at_time = blobs.get(record["state_ref"])
            start_states.update({e["run_id"]:at_time["snapshot_replays"][e["run_id"]] for e in starts})
            request_origins.update({e["payload"]["request_id"]:at_time["provenance"][e["payload"]["request_id"]] for e in requests})
    for run in sorted(configs, key=lambda run: scan["starts"][run]["event_seq"]):
        config = configs[run]
        validate_replay_record(config)
        if start_states.get(run) != config:
            raise ContractError("Replay configuration changed after its run started")
        start = scan["starts"][run]
        events = [e for e in scan["events"].values() if e["run_id"] == run]
        if (start["stage"] != "diagnostic" or not start["condition_id"] or not start["payload"]["fresh_session"]
                or any(e["record_kind"] != config["record_kind"] or e["session_id"] != start["session_id"]
                       or e["condition_id"] != start["condition_id"] for e in events)
                or sum(e["session_id"] == start["session_id"] for e in scan["starts"].values()) != 1):
            raise ContractError("Replay reused a session or changed its identity")
        planned = snapshot_replay_input(blobs, state, config["decision_ref"], config["snapshot_id"],
            policy=MemoryPolicy(**config["policy"]), instruction=config["instruction"],
            max_input_bytes=config["execution_limits"]["max_input_bytes"], context_reader=context_reader)
        source, expected = planned["source"], planned["request"]
        if (digest(canonical(expected)) != config["expected_request_sha256"]
                or source["archive"]["record_kind"] != config["record_kind"]
                or start["payload"]["source_run_ids"] != [source["archive"]["source_run_id"]]
                or start["payload"]["task_ref"] != source["archive"]["task_ref"]
                or start["payload"]["start_snapshot_id"] != config["snapshot_id"]):
            raise ContractError("Replay input or source selection changed")
        if ((config["verified_model"] is not None and config["verified_model"]["id"] != expected["model"])
                or (config["record_kind"] == "illustrative" and expected["model"] != "synthetic-offline")):
            raise ContractError("Replay model differs from its source request")
        source_producers = [e for e in scan["events"].values() if e["type"] == "bank.snapshot_created"
                            and e["payload"]["snapshot_id"] == config["snapshot_id"]]
        if (len(source_producers) != 1 or source_producers[0]["record_kind"] != config["record_kind"]
                or source_producers[0]["event_seq"] >= start["event_seq"]
                or blobs.get(source_producers[0]["payload"]["manifest_ref"]) != state["snapshots"][config["snapshot_id"]]):
            raise ContractError("Replay snapshot lacks a prior producer of the same record kind")
        requests = [e for e in events if e["type"] == "model.request_started"]
        finishes = [e for e in events if e["type"] == "model.request_finished"]
        preloads = [e for e in events if e["type"] == "memory.preloaded"]
        errors = [e["payload"]["code"] for e in events if e["type"] == "runtime.error"]
        result_event = next(e for e in result_events if e["run_id"] == run)
        result = blobs.get(result_event["payload"]["result_ref"])
        validate_replay_record(result)
        if len(requests) > 1 or len(finishes) != len(requests) or len(preloads) > 1 or (requests and not preloads):
            raise ContractError("Replay must have at most one request and one preload")
        if preloads:
            validate_preload(root, preloads[0], scan["events"], state)
        finish, raw, rejection = None, None, None
        if requests:
            request, finish = requests[0], finishes[0]["payload"]
            p = request["payload"]
            manifest = blobs.get(p["input_manifest_ref"])
            origins = request_origins[p["request_id"]]
            sources = blobs.get(origins["sources_ref"])
            binding = [b for b in manifest["bindings"] if b["pointer"] == "/messages/1"]
            if (p["actor_role"] != "worker" or request["phase"] != "solve" or blobs.get(p["request_ref"]) != expected
                    or len(binding) != 1 or binding[0]["source_pointer"] != ""
                    or sources[binding[0]["source_id"]]["kind"] != "memory_snapshot"
                    or sources[binding[0]["source_id"]]["producer_event_id"] != preloads[0]["event_id"]):
                raise ContractError("Replay wire or actual memory provenance differs from preparation")
            raw = blobs.get(finish["response_ref"]) if finish["response_ref"] else None
            if finish["status"] == "response":
                rejection = replay_response_error(raw, config["verified_model"])
                if result["error_code"] != rejection:
                    raise ContractError("Replay completion differs from the raw provider reply")
            elif result["error_code"] is None:
                raise ContractError("Transport failure was reported as a successful reply")
            if finishes[0]["event_seq"] >= result_event["event_seq"]:
                raise ContractError("Replay result precedes its response")
        elif result["error_code"] is None:
            raise ContractError("Replay response has no model request")
        unknown = finish is not None and finish["cost_status"] == "unknown"
        if (errors != ([result["error_code"]] if result["error_code"] is not None else [])
                or result["run_id"] != run or result["request_id"] != (finish["request_id"] if finish else None)
                or result["unknown_cost"] != unknown):
            raise ContractError("Replay result differs from recorded errors, request or cost")
        if result["message_ref"] is not None and (raw is None or rejection is not None
                or blobs.get(result["message_ref"]) != public_assistant_message(raw["choices"][0]["message"])):
            raise ContractError("Replay public reply differs from the original response")
        ends = [e for e in events if e["type"] == "run.ended"]
        outcomes = [e for e in events if e["type"] == "outcome.scored"]
        wanted_outcome = dict(grading_status="not_available", official_success=None, official_score=None, official_artifact_ref=None)
        if (len(ends) != 1 or len(outcomes) != 1 or outcomes[0]["payload"] != wanted_outcome
                or not result_event["event_seq"] < outcomes[0]["event_seq"] < ends[0]["event_seq"]
                or ends[0]["payload"] != dict(termination_reason="single_reply" if not errors else errors[0],
                    grading_status="not_available", end_snapshot_id=result["snapshot_id"])):
            raise ContractError("Replay acquired an official score or inconsistent termination")
        allowed = {"run.started", "memory.preloaded", "model.request_started", "model.request_finished",
                   "memory.input_included", "runtime.error", "outcome.scored", "run.ended"}
        if any(e["type"] not in allowed and e["event_id"] != result_event["event_id"] for e in events):
            raise ContractError("Replay dispatched a tool or an unrecorded business transition")
        bank_id = start["payload"]["bank_id"]
        bank, parent = state["banks"][bank_id], state["snapshots"][config["snapshot_id"]]
        final = state["snapshots"][result["snapshot_id"]]
        final_producers = [e for e in scan["events"].values() if e["type"] == "bank.snapshot_created"
                           and e["payload"]["snapshot_id"] == result["snapshot_id"]]
        if (len(final_producers) != 1 or blobs.get(final_producers[0]["payload"]["manifest_ref"]) != final
                or not start["event_seq"] < final_producers[0]["event_seq"] < result_event["event_seq"]
                or final["parent_snapshot_ids"] != [config["snapshot_id"]] or final["head_revision"] != 0):
            raise ContractError("Replay final snapshot differs from its recorded producer")
        if (result["bank_id"] != bank_id or bank["owner_run"] != run or not bank["sealed"]
                or bank["policy"] != config["policy"] or bank["parent_snapshot"] != config["snapshot_id"]
                or bank["revision"] != 0 or final["bank_id"] != bank_id
                or any(value != parent["heads"] for value in (bank["heads"], bank["start_heads"], final["heads"]))
                or any(value != parent["order"] for value in (bank["order"], bank["start_order"], final["order"]))):
            raise ContractError("Replay modified, shared or failed to seal its snapshot fork")
        spans = [e for e in events if e["type"] == "memory.input_included"]
        rows.append(dict(result, record_kind=config["record_kind"], condition_id=start["condition_id"],
            source_request_sha256=source["archive"]["request_ref"]["sha256"],
            **({"decision_origin": source["decision_origin"], "authored_base_request_sha256": digest(canonical(source["wire"]))}
               if "decision_origin" in source else {}),
            request_sha256=config["expected_request_sha256"], model_requests=len(requests),
            delivery_status=finish["delivery_status"] if finish else "not_submitted",
            memory_input_spans=len(spans), received_memory_spans=len(spans) if finish and finish["delivery_status"] == "response_received" else 0,
            cost_status=finish["cost_status"] if finish else "not_applicable", reported_cost_usd=finish["cost_usd"] if finish else None))
    return dict(format="agent-memory-snapshot-replay-audit/v1", status="passed", experiment_id=experiment,
        replays=rows, model_requests=sum(r["model_requests"] for r in rows),
        statuses=dict(Counter(r["status"] for r in rows)), unknown_cost_requests=sum(r["unknown_cost"] for r in rows),
        observed_cost_usd_decimal=str(sum((Decimal(str(r["reported_cost_usd"])) for r in rows
                                         if r["cost_status"] == "observed"), Decimal(0))),
        request_audit=common, request_audit_scope="whole_journal", run_scope="snapshot_replay_runs_only",
        business_tools_executed=False, official_scores_computed=False,
        scope=SCOPE)
