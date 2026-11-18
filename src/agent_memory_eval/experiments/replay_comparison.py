"""Execute and reconstruct scheduled single-reply comparisons using sealed v1.2 journals."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from typing import Callable

from agent_memory_eval.analysis.snapshot_replay import audit_snapshot_replays
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, digest, read_json
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.experiments.forms_supplement import cooldown_seconds
from agent_memory_eval.models.openrouter import (OpenRouterClient, SpendGuard, model_catalog, model_endpoints,
    request_reservation_usd, resolve_endpoint, resolve_model)
from agent_memory_eval.recording.journal import BlobStore, Journal, read_experiment
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.runner.snapshot_replay import run_snapshot_replay


@dataclass(frozen=True)
class ReplayStudy:
    profile_path: str
    key: str
    policy: MemoryPolicy
    stage: Callable
    schedule: Callable
    classify: Callable
    block_field: str = "repeat"


def preflight(workspace, profile, study):
    with TemporaryDirectory() as directory, Journal(Path(directory), "calibration_preflight", contract_version="1.2") as journal:
        inputs = study.stage(journal, workspace, profile)
        return dict(cells=inputs["cells"], first_pass_evidence=inputs["evidence"],
                    slots=study.schedule(profile)), inputs["wires"]


def settle(profile, guard, result, finish, raw, reserve, reserved):
    code = result["error_code"]
    if result["unknown_cost"] != guard.unknown_cost:
        raise ContractError("Provider and journal disagree on billing")
    if not result["stop_required"]:
        return None, Decimal(0), 0
    detail = raw.get("error", {}) if isinstance(raw, dict) else {}
    if (isinstance(detail, dict) and detail.get("code") in profile["unpriced_http"]
            and code == f"HTTP_{detail['code']}" and finish["delivery_status"] == "response_received"):
        addition = reserve if result["unknown_cost"] else Decimal(0)
        guard.max_usd = float(Decimal(str(profile["max_usd"])) - reserved - addition)
        guard.unknown_cost = False
        return None, addition, cooldown_seconds(detail, 5, datetime.now(timezone.utc))
    return code or "unknown_cost", Decimal(0), 0


def live(workspace, profile, study):
    identity = implementation_identity(ROOT)
    if not os.getenv("OPENROUTER_API_KEY"):
        raise ContractError("OPENROUTER_API_KEY_MISSING")
    validated, wires = preflight(workspace, profile, study)
    prototype = next(iter(wires.values()))
    params = {k: v for k, v in prototype.items() if k not in {"model", "provider", "stream", "messages"}}
    model = resolve_model(model_catalog(), profile["model"], params)
    route = prototype["provider"]
    model = resolve_endpoint(model, model_endpoints(model["id"]), route, params)
    if model["canonical_slug"] != profile["canonical_slug"] or implementation_identity(ROOT) != identity:
        raise ContractError("Verified worker or implementation differs from the freeze")
    slots = validated["slots"]
    reservations = {key: request_reservation_usd(wire, model) for key, wire in wires.items()}
    upper = sum((reservations[r["case"], r["arm"]] for r in slots), Decimal(0))
    if upper > Decimal(str(profile["max_usd"])):
        raise ContractError("Planned total of per-request reservations exceeds the study cap")
    destination = workspace / "runs" / profile["study"]
    destination.mkdir(exist_ok=False)
    control = dict(profile=profile, implementation=identity, verified_model=model, **validated,
                   profile_file_sha256=digest((ROOT / study.profile_path).read_bytes()), maximum_reserved_usd=str(upper))
    write_json(destination / "control.json", control)
    guard = SpendGuard(max_calls=len(slots), max_usd=profile["max_usd"], max_seconds=profile["max_seconds"])
    limits = ExecutionLimits(1, 0, 0, profile["max_input_bytes"])
    client = OpenRouterClient(model, guard, route=route, max_input_bytes=limits.max_input_bytes,
                              timeout_seconds=profile["timeout_seconds"])
    reserved, stop = Decimal(0), None
    ledger = [dict(r, status="not_started") for r in slots]
    write_json(destination / "allocations.json", ledger)
    for block in dict.fromkeys(r[study.block_field] for r in slots):
        if stop:
            break
        folder = destination / f"{study.block_field}-{block:02d}"
        with Journal(folder, f"{study.key}_{block}", contract_version="1.2") as journal:
            inputs = study.stage(journal, workspace, profile)
            if inputs["cells"] != validated["cells"] or inputs["evidence"] != validated["first_pass_evidence"]:
                raise ContractError("Materialized evidence or requests differ from preflight")
            for row in (r for r in ledger if r[study.block_field] == block):
                if time.monotonic() >= guard.deadline:
                    stop = "time_limit"
                    break
                case, arm = row["case"], row["arm"]
                row.update(status="started", run_id=f"{study.key}_{row['slot']:03d}")
                write_json(destination / "allocations.json", ledger)
                result = run_snapshot_replay(journal, inputs["decisions"][case], inputs["snapshots"][arm],
                    policy=study.policy, instruction=profile["instruction"], condition_id=arm, client=client,
                    execution_limits=limits, expected_request_sha256=inputs["cells"][case][arm]["wire_sha256"],
                    run_id=row["run_id"], context_reader=inputs["reader"])
                finish, = [e["payload"] for e in journal.events.values() if e["run_id"] == row["run_id"]
                           and e["type"] == "model.request_finished"]
                raw = journal.blobs.get(finish["response_ref"]) if finish["response_ref"] else None
                stop, extra, cooldown = settle(profile, guard, result, finish, raw, reservations[case, arm], reserved)
                reserved += extra
                row.update(status=result["status"], error_code=result["error_code"], reserved_usd=str(extra),
                           generation_id=raw.get("id") if isinstance(raw, dict) else None)
                write_json(destination / "allocations.json", ledger)
                if stop:
                    break
                if cooldown:
                    time.sleep(min(cooldown, max(0, guard.deadline - time.monotonic())))
        print(f"{study.block_field} {block} | {guard.calls}/{len(ledger)} submissions | billed USD {guard.reported_usd:.9f}",
              file=sys.stderr, flush=True)
    summary = dict(stop_reason=stop, http_calls=guard.calls, reserved_unconfirmed_usd=str(reserved),
        reported_usd=guard.reported_usd, control_sha256=digest((destination / "control.json").read_bytes()),
        allocations_sha256=digest((destination / "allocations.json").read_bytes()),
        journal_sha256={p.parent.name: digest(p.read_bytes()) for p in sorted(destination.glob(f"{study.block_field}-*/log.jsonl"))})
    write_json(destination / "completion.json", summary)
    return summary


def audited_allocations(workspace, profile, study):
    destination = workspace / "runs" / profile["study"]
    control = read_json((destination / "control.json").read_bytes())
    seal = read_json((destination / "completion.json").read_bytes())
    ledger = read_json((destination / "allocations.json").read_bytes())
    verified, _ = preflight(workspace, profile, study)
    plan = verified["slots"]
    if (profile != control["profile"] or any(verified[k] != control[k] for k in verified)
            or control["profile_file_sha256"] != digest((ROOT / study.profile_path).read_bytes())
            or any(digest((destination / file).read_bytes()) != seal[key] for file, key in
                   (("allocations.json", "allocations_sha256"), ("control.json", "control_sha256")))
            or [{k: r[k] for k in plan[0]} for r in ledger] != plan):
        raise ContractError("Archived inputs, schedule or completion seal differ")
    results = []
    folders = sorted(destination.glob(f"{study.block_field}-*"))
    if {p.name for p in folders} != set(seal["journal_sha256"]):
        raise ContractError("Archived comparison journals differ from the completion seal")
    for folder in folders:
        if digest((folder / "log.jsonl").read_bytes()) != seal["journal_sha256"][folder.name]:
            raise ContractError("Replicate journal differs from its seal")
        scan, state, exp = read_experiment(folder)
        audit = audit_snapshot_replays(folder, evidence=(scan, state, exp))
        rows = [r for r in ledger if r[study.block_field] == int(folder.name.split("-")[-1]) and r["status"] != "not_started"]
        finishes = {e["run_id"]: e["payload"] for e in scan["events"].values() if e["type"] == "model.request_finished"}
        blobs = BlobStore(folder)
        for row, checked in zip(rows, audit["replays"], strict=True):
            case, arm, run = row["case"], row["arm"], row["run_id"]
            expected = verified["cells"][case][arm]["wire_sha256"]
            finish = finishes[run]
            raw = blobs.get(finish["response_ref"]) if finish["response_ref"] else {}
            if (raw.get("id") != row["generation_id"] or checked["run_id"] != run or checked["condition_id"] != arm
                    or checked["status"] != row["status"] or checked["error_code"] != row["error_code"]
                    or checked["request_sha256"] != expected or checked["model_requests"] != 1
                    or checked["memory_input_spans"] != verified["cells"][case][arm].get(
                        "expected_memory_spans", 0 if arm.startswith("empty") else 2)):
                raise ContractError("Scheduled treatment differs from the audited exposure or response")
            score = None
            if checked["status"] == "response":
                score = study.classify(case, profile["cases"][case], blobs.get(checked["message_ref"]))
            results.append(dict(**row, wire_sha256=expected, memory_input_spans=checked["memory_input_spans"],
                response_ref=finish["response_ref"], cost_status=finish["cost_status"], cost_usd=finish["cost_usd"],
                usage=finish["usage"], score=score))
    replies = [r for r in results if r["score"] is not None]
    if (any(not r["generation_id"] for r in replies) or len({r["generation_id"] for r in replies}) != len(replies)
            or len(results) != sum(r["status"] != "not_started" for r in ledger)):
        raise ContractError("Missing allocation, or missing/reused provider generation ID")
    billed = sum((Decimal(str(r["cost_usd"])) for r in results if r["cost_status"] == "observed"), Decimal(0))
    reserve = sum((Decimal(r["reserved_usd"]) for r in results), Decimal(0))
    if abs(billed - Decimal(str(seal["reported_usd"]))) > Decimal("1e-10") or reserve != Decimal(seal["reserved_unconfirmed_usd"]):
        raise ContractError("Reconstructed costs differ from the sealed batch")
    return dict(study=profile["study"], implementation=control["implementation"], completion=seal,
                planned_slots=len(plan), scored_replies=len(replies), distinct_generation_ids=len({r["generation_id"] for r in replies}),
                distinct_wires=len({r["wire_sha256"] for r in results}), reported_usd=str(billed), reserved_usd=str(reserve),
                decisions=verified["cells"], first_pass_evidence=control["first_pass_evidence"], rows=results)
