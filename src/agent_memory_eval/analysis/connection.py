"""Read-only verification of a no-memory observed connection trace."""

from collections import Counter
from decimal import Decimal
import json
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.contracts.connection import forward_seed, provider_route, validate_profile
from agent_memory_eval.contracts.validation import validate_manifest
from agent_memory_eval.recording.journal import BlobStore, read_experiment


def audit_connection(root):
    root = Path(root)
    scan, state, experiment = read_experiment(root)
    blobs = BlobStore(root)
    config = state["connection_check"]["requested"]
    validate_profile(config)
    if config["intent"] != "connection_check" or config["memory_enabled"] is not False:
        raise ContractError("Expected a no-memory connection profile")
    requests, counts, total, tokens = {}, Counter(), Decimal(0), 0
    models, providers, statuses = Counter(), Counter(), Counter()
    unknown_cost = False
    for event in scan["events"].values():
        if event["record_kind"] != "observed" or event["type"].startswith("memory."):
            raise ContractError("Connection trace has an unexpected record kind or memory event")
        payload = event["payload"]
        if event["type"] == "model.request_started":
            request_id = payload["request_id"]
            manifest = blobs.get(payload["input_manifest_ref"])
            origin = state["provenance"][request_id]
            if validate_manifest(root, manifest, event, blobs.get(origin["sources_ref"]), scan["events"], origin["approved_templates"]):
                raise ContractError("Baseline connection unexpectedly includes memory spans")
            wire = blobs.get(payload["request_ref"])
            role = payload["actor_role"]
            if role not in {"worker", "user_simulator"}:
                raise ContractError("Unexpected model role")
            if wire["provider"] != provider_route(config, role) or wire["model"] != config[role]["model_id"]:
                raise ContractError("Request route differs from the declared profile")
            for key, value in config[role]["parameters"].items():
                if wire.get(key) != value:
                    raise ContractError("Request parameters differ from the declared profile")
            if not forward_seed(config, role) and "seed" in wire:
                raise ContractError("Unsupported native seed was forwarded")
            if any(t["function"]["name"].startswith("memory_") for t in wire.get("tools", [])):
                raise ContractError("Baseline exposed memory tools")
            requests[request_id] = role
            counts[role] += 1
        if event["type"] == "model.request_finished":
            if payload["request_id"] not in requests:
                raise ContractError("Completion has no request")
            statuses[payload["status"]] += 1
            unknown_cost |= payload["cost_status"] == "unknown"
            if payload["status"] == "response":
                response = blobs.get(payload["response_ref"])
                role = requests[payload["request_id"]]
                model = state["connection_check"]["model_catalog_entries"][role]
                endpoint = model.get("selected_endpoint")
                if endpoint is not None:
                    if endpoint["tag"] != provider_route(config, role)["only"][0]:
                        raise ContractError("Saved endpoint differs from the pinned role")
                    expected_provider = endpoint["provider_name"]
                elif config["profile_version"] == "1.0":
                    expected_provider = "OpenAI"
                else:
                    raise ContractError("Provider endpoint metadata is missing")
                if response.get("provider") != expected_provider or response.get("model") not in {model["id"], model.get("canonical_slug")}:
                    raise ContractError("Response identity differs from the pinned role")
                if payload["usage"] != response.get("usage"):
                    raise ContractError("Usage differs from provider response")
                if payload["cost_status"] == "observed":
                    if payload["cost_usd"] != response["usage"].get("cost"):
                        raise ContractError("Cost differs from provider response")
                    total += Decimal(str(payload["cost_usd"]))
                tokens += response.get("usage", {}).get("total_tokens", 0)
                models[response["model"]] += 1
                providers[str(response.get("provider", "unknown"))] += 1
    if scan["tail_bytes"] or scan["incomplete_runs"] or scan["pending_requests"]:
        raise ContractError("Connection evidence is incomplete")
    summary = json.loads((root / "connection-summary.json").read_text())
    if summary["requests_by_role"] != dict(counts):
        raise ContractError("Summary request counts differ from the journal")
    if abs(Decimal(str(summary["reported_cost_usd"])) - total) > Decimal("0.000000001"):
        raise ContractError("Summary cost differs from provider records")
    if summary["unknown_cost"] != unknown_cost:
        raise ContractError("Summary unknown cost differs from provider records")
    outcomes = [e["payload"] for e in scan["events"].values()
                if e["type"] == "outcome.scored" and e["run_id"] == summary["run_id"]]
    if len(outcomes) != 1:
        raise ContractError("Connection needs one linked outcome")
    outcome = outcomes[0]
    if (outcome["grading_status"], outcome["official_success"], outcome["official_score"]) != (
            summary["grading_status"], summary["official_success"], summary["official_score"]):
        raise ContractError("Summary outcome differs from the journal")
    native_tool_calls = None
    if summary.get("grading_status") == "graded":
        report = blobs.get(summary["report_ref"])
        if (report["reward_info"]["reward"] != summary["official_score"]
                or outcome["official_artifact_ref"] != summary["report_ref"]
                or summary["official_success"] != (summary["official_score"] == 1.0)):
            raise ContractError("Summary differs from native grading artifact")
        native_tool_calls = sum(len(m.get("tool_calls") or []) for m in report["messages"] if m["role"] == "assistant")
    return dict(intent="connection_check", status="audit_passed", experiment_id=experiment,
        requests_by_role=dict(counts), completion_statuses=dict(statuses),
        returned_models=dict(models), returned_providers=dict(providers),
        reported_cost_usd_decimal=str(total), unknown_cost=unknown_cost,
        reported_tokens=tokens, native_business_tool_calls=native_tool_calls,
        log_sha256=digest((root / "log.jsonl").read_bytes()),
        summary_sha256=digest((root / "connection-summary.json").read_bytes()),
        memory_enabled=False, official_success=summary.get("official_success"),
        official_score=summary.get("official_score"), native_revision=config["benchmark"]["source_revision"])
