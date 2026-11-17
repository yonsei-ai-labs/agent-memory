"""Shared read-only request provenance and exposure audit across experiment types."""

from collections import Counter
from decimal import Decimal
import math
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.contracts.validation import validate_manifest
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from agent_memory_eval.recording.provenance import exposure_payload


def audit_requests(root, *, evidence=None):
    root = Path(root)
    scan, state, _ = evidence or read_experiment(root)
    if scan["tail_bytes"] or scan["incomplete_runs"] or scan["pending_requests"]:
        raise ContractError("Request evidence is incomplete")
    blobs, expected, actual, roles = BlobStore(root), {}, {}, Counter()
    for row in scan["records"]:
        requests = [e for e in row["events"] if e["type"] == "model.request_started"]
        if not requests:
            continue
        # Request-local state also works when a later runner drops old provenance.
        origins = blobs.get(row["state_ref"])["provenance"]
        for event in requests:
            payload = event["payload"]
            roles[payload["actor_role"]] += 1
            manifest = blobs.get(payload["input_manifest_ref"])
            origin = origins[payload["request_id"]]
            sources = blobs.get(origin["sources_ref"])
            spans = validate_manifest(root, manifest, event, sources, scan["events"], origin["approved_templates"])
            for span in spans:
                expected[span["span_id"]] = (event["run_id"], exposure_payload(manifest, span, sources[span["source_id"]]))
    for event in scan["events"].values():
        if event["type"] == "memory.input_included":
            key = event["payload"]["manifest_span_id"]
            if key in actual:
                raise ContractError("Duplicate memory exposure")
            actual[key] = (event["run_id"], event["payload"])
    if expected != actual:
        raise ContractError("Memory exposure differs from request provenance")
    statuses, cost, unknown, deliveries = Counter(), Decimal(0), 0, {}
    for event in scan["events"].values():
        if event["type"] != "model.request_finished":
            continue
        payload = event["payload"]
        deliveries[payload["request_id"]] = payload
        statuses[payload["status"]] += 1
        unknown += payload["cost_status"] == "unknown"
        raw = blobs.get(payload["response_ref"]) if payload["response_ref"] else None
        usage = raw.get("usage") if isinstance(raw, dict) else None
        reported = usage.get("cost") if isinstance(usage, dict) else None
        known = type(reported) in (int, float) and math.isfinite(reported) and reported >= 0
        if payload["status"] == "response":
            if (not isinstance(raw, dict) or raw["choices"][0]["message"]["role"] != "assistant"
                    or payload["usage"] != usage):
                raise ContractError("Response or usage differs from the original provider body")
            if known != (payload["cost_status"] == "observed"):
                raise ContractError("Response cost availability differs from the raw usage")
            if not known and event["record_kind"] == "observed" and payload["cost_status"] != "unknown":
                raise ContractError("An observed response with no cost must retain unknown cost")
        if payload["cost_status"] == "observed":
            if not known or payload["cost_usd"] != reported or payload["usage"] != usage:
                raise ContractError("Recorded cost differs from original provider usage")
            cost += Decimal(str(reported))
        elif payload["cost_usd"] is not None:
            raise ContractError("Unobserved cost cannot carry a reported amount")
    by_delivery = Counter(deliveries[payload["request_id"]]["delivery_status"] for _, payload in actual.values())
    normal_spans = sum(deliveries[payload["request_id"]]["status"] == "response" for _, payload in actual.values())
    configurations = dict(state.get("assignments", {}))
    configurations.update(state.get("memory_writes", {}))
    configurations.update(state.get("snapshot_replays", {}))
    for run, config in configurations.items():
        if "execution_limits" not in config:
            continue
        if config.get("execution_contract") != "per-role-limits/v1":
            raise ContractError("Unknown execution limit contract")
        limits = ExecutionLimits(**config["execution_limits"])
        counts = Counter()
        for event in scan["events"].values():
            if event["run_id"] != run or event["type"] != "model.request_started":
                continue
            payload = event["payload"]
            actor = payload["actor_role"]
            counts[actor] += 1
            if counts[actor] > limits.cap(actor):
                raise ContractError("Requests exceeded the declared role limit")
            size = len(canonical(blobs.get(payload["request_ref"])))
            if size > limits.max_input_bytes:
                finish = deliveries[payload["request_id"]]
                if (finish["status"] != "error" or finish["delivery_status"] != "not_submitted"
                        or finish["response_ref"] is not None or finish["cost_status"] != "not_applicable"):
                    raise ContractError("Oversized input was submitted or misclassified")
    return dict(status="passed", requests=sum(roles.values()), requests_by_role=dict(roles),
                memory_input_spans=len(actual), responses_by_status=dict(statuses),
                observed_cost_usd_decimal=str(cost), unknown_cost_requests=unknown,
                memory_spans_by_delivery=dict(by_delivery), normal_response_memory_spans=normal_spans)
