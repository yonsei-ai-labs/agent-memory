"""Complete the remaining forms allocations after a separately authorized, identified 504."""
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import re

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import DESIGN, endpoint_terms, live_models, prepare, write_json
from agent_memory_eval.experiments.forms_solve import WRITTEN_ARMS
from agent_memory_eval.experiments.forms_supplement import (
    COMPLETION, OUT_ROOT, SOURCE_ROOT, SupplementGuard, load_parent, rate_limit_evidence,
    run_pending, solve_recorded_row, totals,
)
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.models.openrouter import OpenRouterClient, request_reservation_usd
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity

SUPPLEMENT_SEAL = "40004aaf9f1d127940bf9782864abe1cb6080796a73fa7bd1141a0e4652a5756"
SOURCE_SEALS = {SOURCE_ROOT: COMPLETION, OUT_ROOT: SUPPLEMENT_SEAL}
FOLLOW_ROOT = "runs/tau2-two-pass-forms-luna-deepseek-supplement-02"
FOLLOW_FORMAT = "agent-memory-two-pass-forms-followon/v1"


def identified_generation(response):
    if not isinstance(response, dict):
        return None
    error = response.get("error")
    metadata = error.get("metadata") if isinstance(error, dict) else None
    values = (response.get("id"), response.get("generation_id"),
              metadata.get("generation_id") if isinstance(metadata, dict) else None)
    return next((v for v in values if isinstance(v, str) and re.fullmatch(r"gen-[A-Za-z0-9-]{1,124}", v)), None)


def provider_error_receipt(result, events, blobs, models):
    """Accept received HTTP 429, or an identified 504 whose exact source wire is available."""
    code = result.get("error_code")
    if code not in {"HTTP_429", "HTTP_504", "PROVIDER_RESPONSE_ERROR"}:
        return None
    events = list(events)
    finishes = [e["payload"] for e in events if e["type"] == "model.request_finished"]
    if not finishes:
        return None
    last = finishes[-1]
    if (last["status"] != "error" or last["delivery_status"] != "response_received"
            or not last["response_ref"] or last["cost_status"] not in {"observed", "unknown"}):
        return None
    response = blobs.get(last["response_ref"])
    generation = identified_generation(response)
    if code == "HTTP_429":
        receipt = rate_limit_evidence(result, events, blobs, models)
        return dict(**receipt, code=429, generation_id=generation)
    detail = response.get("error") if isinstance(response, dict) else None
    if not isinstance(detail, dict) or str(detail.get("code")) != "504" or generation is None:
        return None
    start = next((e["payload"] for e in events if e["type"] == "model.request_started"
                  and e["payload"]["request_id"] == last["request_id"]), None)
    if not start:
        raise ContractError("The identified 504 lacks its wire")
    wire, role = blobs.get(start["request_ref"]), start["actor_role"]
    reserve = request_reservation_usd(wire, models[role]) if last["cost_status"] == "unknown" else Decimal(0)
    # Retry instructions here come only from the sanitized HTTP transport, not an arbitrary JSON envelope.
    return dict(request_id=last["request_id"], actor=role, request_sha256=start["request_ref"]["sha256"],
                response_sha256=last["response_ref"]["sha256"], code=504, generation_id=generation,
                cost_status=last["cost_status"], reserved_usd=str(reserve),
                retry_after_seconds=detail.get("retry_after_seconds") if code == "HTTP_504" else None,
                retry_after_utc=detail.get("retry_after_utc") if code == "HTTP_504" else None)


def pending_after(previous_rows, incoming, previous_raw):
    if len(incoming) != len(previous_rows):
        raise ContractError("Missing first-supplement assignment")
    pending = []
    for old, row in zip(incoming, previous_rows):
        if any(row[k] != v for k, v in old.items()):
            raise ContractError("First supplement changed a preregistered assignment identity")
        folder = safe_path(previous_raw, row["directory"])
        if row["status"] == "not_started":
            if folder.exists() or "result" in row or row.get("reason") != "unknown_cost":
                raise ContractError("Supposedly untouched row has effects or a different reason")
            pending.append(dict(old))
        elif (row["status"] not in {"completed", "error"} or row["result"]["run_id"] != row["run_id"]
              or row["result"]["status"] != row["status"] or digest((folder / "log.jsonl").read_bytes()) != row["result"]["log_sha256"]):
            raise ContractError("Started first-supplement row differs from its sealed run")
    return pending


def load_parents(workspace):
    original, control, assigned, writers, first = load_parent(workspace)
    previous = safe_path(workspace, OUT_ROOT)
    seal = read_json(asset_text(previous, dict(path="completion.json", sha256=SUPPLEMENT_SEAL)).encode())
    pctl = read_json(asset_text(previous, dict(path="control.json", sha256=seal["control_sha256"])).encode())
    rows = read_json(asset_text(previous, dict(path="allocations.json", sha256=seal["allocation_sha256"])).encode())
    pending = pending_after(rows, assigned, previous)
    if (seal["stop_reason"] != "unknown_cost" or pctl["carry"] != first or pctl["source_root"] != SOURCE_ROOT
            or pctl["source_completion_sha256"] != COMPLETION or pctl["original_started_at"] != control["started_at"]
            or [x["index"] for x in pending] != list(range(303, 585))):
        raise ContractError("Parent lineage or the 282 never-started allocations differ from approval")
    failed = rows[25]
    folder = previous / failed["directory"]
    scan, _, _ = read_experiment(folder)
    failure = provider_error_receipt(failed["result"], scan["events"].values(), BlobStore(folder), pctl["model_catalog_entries"])
    bill = totals(rows)
    if (bill != seal["supplement"] or not failure or failed["index"] != 302 or failure["code"] != 504
            or failure["cost_status"] != "unknown" or Decimal(failure["reserved_usd"]) != Decimal("0.0082314")
            or bill["unknown_calls"] != 1 or not seal["other_unpriced_stop"]):
        raise ContractError("Charge carry-forward is not the sealed individual 504")
    reserve = Decimal(first["original_429"]["reserved_usd"]) + Decimal(failure["reserved_usd"])
    carry = dict(http_calls=first["http_calls"] + bill["http_calls"],
                 known_usd=str(Decimal(first["known_usd"]) + Decimal(bill["known_usd"])),
                 unknown_calls=first["unknown_calls"] + bill["unknown_calls"], reserved_usd=str(reserve),
                 original_429=first["original_429"], prior_504=failure, pending_sha256=digest(canonical(pending)))
    if (carry["http_calls"] != seal["combined_http_calls"] or carry["known_usd"] != seal["combined_known_usd"]
            or carry["unknown_calls"] != seal["combined_unknown_calls"]
            or Decimal(seal["combined_reserved_usd"]) != Decimal(first["original_429"]["reserved_usd"])):
        raise ContractError("The three-root opening balances disagree")
    return original, control, pctl, pending, writers, carry


def preflight(workspace):
    original, control, _, rows, writers, carry = load_parents(workspace)
    needed = [r for r in rows if r["arm"] in WRITTEN_ARMS]
    for r in needed:
        key = r["task_id"], r["repetition"], r["arm"]
        source = writers[key]
        bank = read_form_bank(original / source["directory"], expected_log_sha256=source["result"]["log_sha256"])
        if ((bank["task_id"], bank["repetition"], bank["arm"]) != key or bank["run_id"] != source["run_id"]
                or bank["snapshot_id"] != source["result"]["snapshot_id"]):
            raise ContractError("Follow-on cell differs from the original writer")
    return dict(source_seals=SOURCE_SEALS, original_started_at=control["started_at"], parent_guards=control["profile"]["guards"],
                untouched=len(rows), scope=[rows[0]["index"], rows[-1]["index"]], sealed_writer_banks_required=len(needed), **carry)


def run_followon(profile, workspace, checkout):
    workspace, root = Path(workspace).resolve(), ROOT.resolve()
    if (profile.get("format") != FOLLOW_FORMAT or profile.get("status") != "ready" or profile.get("source_seals") != SOURCE_SEALS
            or profile.get("output_root") != FOLLOW_ROOT or profile.get("provider_error_cooldown_seconds") != 60):
        raise ContractError("A frozen 282-cell follow-on profile is required")
    identity = implementation_identity(root)
    destination = safe_path(workspace, FOLLOW_ROOT)
    if (identity["implementation_commit"] != profile["implementation_commit"] or destination.exists()
            or not os.environ.get("OPENROUTER_API_KEY")):
        raise ContractError("Follow-on needs its frozen runtime, a new isolated root and an available credential")
    original, control, pctl, rows, writers, carry = load_parents(workspace)
    approved = read_json(asset_text(workspace, profile["preflight_ref"]).encode())
    previous_audit = read_json(asset_text(workspace, profile["previous_audit_ref"]).encode())
    billing_check = read_json(asset_text(workspace, profile["billing_check_ref"]).encode())
    parent = control["profile"]
    if (approved["implementation_commit"] != identity["implementation_commit"] or approved["parent"] != preflight(workspace)
            or carry["pending_sha256"] != profile["pending_sha256"]
            or parent != read_json(asset_text(workspace, profile["parent_profile_ref"]).encode())
            or pctl["profile"] != read_json(asset_text(workspace, profile["supplement_profile_ref"]).encode())
            or previous_audit["combined"]["http_calls"] != carry["http_calls"] or previous_audit["combined"]["known_usd"] != carry["known_usd"]
            or billing_check["provider_response_sha256"] != carry["prior_504"]["response_sha256"]
            or billing_check["observed_error"]["id"] != carry["prior_504"]["generation_id"]):
        raise ContractError("Changed preflight, earlier audit, billing lookup or ready profile")
    design = read_json(asset_text(root, parent["design_ref"]).encode())
    asset_text(root, parent["research_ref"])
    if design != control["design"] or parent["design_ref"]["path"] != DESIGN:
        raise ContractError("Study design changed")
    plan = prepare(root, workspace, checkout, design)
    if plan["summary"] != control["preflight"]:
        raise ContractError("Native benchmark, source or wire preflight changed")
    models = live_models(plan["connection"])
    if (any(endpoint_terms(models) != endpoint_terms(c["model_catalog_entries"]) for c in (control, pctl))
            or identity != implementation_identity(root)):
        raise ContractError("Provider, pricing or frozen runtime changed")
    guard = SupplementGuard(parent["guards"], control["started_at"], carry)
    limits = design["second_pass"]["proposed_limits"]
    clients = {role: OpenRouterClient(model, guard, route=plan["connection"][role]["provider"],
        max_input_bytes=limits["max_input_bytes"], timeout_seconds=parent["http_timeout_seconds"]) for role, model in models.items()}
    destination.mkdir(exist_ok=False)
    (destination / "jobs").mkdir()
    write_json(destination / "control.json", dict(profile=profile, implementation=identity, carry=carry, source_seals=SOURCE_SEALS,
        original_started_at=control["started_at"], started_at=datetime.now(timezone.utc).isoformat(), model_catalog_entries=models))
    def execute(row):
        return solve_recorded_row(row, original, destination, plan, design, models, clients, guard, writers,
                                  parent["guards"]["max_usd"], provider_error_receipt)
    ledger = destination / "allocations.json"
    reason = run_pending(rows, execute, guard, lambda rs: write_json(ledger, rs), profile["provider_error_cooldown_seconds"],
                         error_field="provider_error")
    bill = totals(rows)
    summary = dict(stop_reason=reason, supplement=bill, ended_at=datetime.now(timezone.utc).isoformat(),
        combined_http_calls=guard.calls, combined_known_usd=str(Decimal(carry["known_usd"]) + Decimal(bill["known_usd"])),
        combined_unknown_calls=carry["unknown_calls"] + bill["unknown_calls"], combined_reserved_usd=str(guard.reserved),
        other_unpriced_stop=guard.unknown_cost, allocation_sha256=digest(ledger.read_bytes()),
        control_sha256=digest((destination / "control.json").read_bytes()))
    write_json(destination / "completion.json", summary)
    return summary
