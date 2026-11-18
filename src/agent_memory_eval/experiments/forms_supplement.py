"""Execute only the untouched suffix of a sealed forms study under its approved billing amendment."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import time

from agent_memory_eval.contracts.core import ContractError, canonical, digest, new_id, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import (
    BatchGuard, DESIGN, endpoint_terms, live_models, ordered_jobs, prepare, request_usage, solve_connection, write_json,
)
from agent_memory_eval.experiments.forms_solve import WRITTEN_ARMS, run_forms_solve
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.models.openrouter import OpenRouterClient, request_reservation_usd
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, Journal, read_experiment, safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.runner.tau2_baseline import build_baseline_orchestrator

SOURCE_ROOT = "runs/tau2-two-pass-forms-luna-deepseek-01"
OUT_ROOT = "runs/tau2-two-pass-forms-luna-deepseek-supplement-01"
COMPLETION = "3528eed073199e62e38b2b30959f6b368c3a00e4e6a54021270de548e8b91381"
BASE_COMMIT = "7e30fa24b2d4481e0a91fa30209debccba1c175f"
PROFILE_FORMAT = "agent-memory-two-pass-forms-supplement/v1"


def totals(rows):
    bills = [v for row in rows for v in row.get("result", {}).get("billing_by_role", {}).values()]
    return dict(http_calls=sum(v["http_calls"] for v in bills),
                known_usd=str(sum((Decimal(v["reported_usd"]) for v in bills), Decimal(0))),
                unknown_calls=sum(v["unknown_count"] for v in bills))


def rate_limit_evidence(result, events, blobs, models):
    """Qualify only a received transport-level HTTP 429, tied to its exact failed wire."""
    if result.get("error_code") != "HTTP_429":
        return None
    starts = {e["payload"]["request_id"]: e["payload"] for e in events if e["type"] == "model.request_started"}
    finishes = [e["payload"] for e in events if e["type"] == "model.request_finished"]
    if not finishes:
        raise ContractError("Rate-limit exception has no request receipt")
    last = finishes[-1]
    if (last["status"] != "error" or last["delivery_status"] != "response_received"
            or not last["response_ref"] or last["cost_status"] not in {"unknown", "observed"}):
        raise ContractError("Rate-limit exception has no received HTTP failure")
    response = blobs.get(last["response_ref"])
    error = response.get("error") if isinstance(response, dict) else None
    if not isinstance(error, dict) or type(error.get("code")) is not int or error["code"] != 429:
        raise ContractError("Rate-limit code differs from the provider receipt")
    start = starts[last["request_id"]]
    wire = blobs.get(start["request_ref"])
    role = start["actor_role"]
    reserve = request_reservation_usd(wire, models[role]) if last["cost_status"] == "unknown" else Decimal(0)
    return dict(request_id=last["request_id"], actor=role, request_sha256=start["request_ref"]["sha256"],
                response_sha256=last["response_ref"]["sha256"], cost_status=last["cost_status"],
                reserved_usd=str(reserve), retry_after_seconds=error.get("retry_after_seconds"),
                retry_after_utc=error.get("retry_after_utc"))


def load_parent(workspace):
    raw = safe_path(workspace, SOURCE_ROOT)
    completion = read_json(asset_text(workspace, dict(path=SOURCE_ROOT + "/completion.json", sha256=COMPLETION)).encode())
    control = read_json(asset_text(raw, dict(path="control.json", sha256=completion["control_sha256"])).encode())
    rows = read_json(asset_text(raw, dict(path="allocations.json", sha256=completion["allocation_sha256"])).encode())
    expected = ordered_jobs(control["design"])
    if (completion["stop_reason"] != "unknown_cost" or control["implementation"]["implementation_commit"] != BASE_COMMIT
            or len(rows) != len(expected) or len({r["run_id"] for r in rows}) != len(rows)
            or any(any(r[k] != v for k, v in e.items()) or r["directory"] != f'jobs/{r["index"]:04d}'
                   for r, e in zip(rows, expected))):
        raise ContractError("Sealed parent or allocation differs from the approved batch")
    pending = []
    writers = {}
    for row in rows:
        folder = safe_path(raw, row["directory"])
        if row["status"] == "not_started":
            if folder.exists() or "result" in row or row["reason"] != "unknown_cost" or row["phase"] != "solve":
                raise ContractError("A supposedly untouched assignment has side effects or an unexpected disposition")
            pending.append({k: row[k] for k in ("index", "phase", "task_id", "repetition", "arm", "directory", "run_id")})
            continue
        if digest((folder / "log.jsonl").read_bytes()) != row["result"]["log_sha256"]:
            raise ContractError("Sealed parent job differs from its original allocation")
        if row["phase"] == "write":
            if row["status"] not in {"completed", "empty"}:
                raise ContractError("A source writer is unavailable")
            writers[row["task_id"], row["repetition"], row["arm"]] = row
    if [r["index"] for r in pending] != list(range(277, 585)) or len(writers) != 232:
        raise ContractError("Supplement must contain exactly the 308 never-started assignments")
    failed = rows[275]
    evidence = read_experiment(raw / failed["directory"])
    failure = rate_limit_evidence(failed["result"], evidence[0]["events"].values(),
                                  BlobStore(raw / failed["directory"]), control["model_catalog_entries"])
    billing = totals(rows)
    if (billing["unknown_calls"] != 1 or billing["http_calls"] != completion["http_calls"] or
            abs(Decimal(billing["known_usd"]) - Decimal(str(completion["reported_usd"]))) > Decimal("1e-8") or
            not failure or failure["cost_status"] != "unknown" or Decimal(failure["reserved_usd"]) != Decimal("0.0018567")):
        raise ContractError("Parent accounting differs from the approved carry-forward")
    return raw, control, pending, writers, dict(**billing, original_429=failure, pending_sha256=digest(canonical(pending)))


class SupplementGuard(BatchGuard):
    def __init__(self, limits, started_at, carry):
        original_deadline = datetime.fromisoformat(started_at) + timedelta(seconds=limits["max_seconds"])
        seconds = (original_deadline - datetime.now(timezone.utc)).total_seconds()
        if seconds <= 0:
            raise ContractError("Original shared time budget is exhausted")
        super().__init__(max_calls=limits["max_calls"], max_usd=limits["max_usd"], max_seconds=seconds)
        self.study_cap = Decimal(str(limits["max_usd"]))
        self.calls = carry["http_calls"]
        self.reported_usd = float(carry["known_usd"])
        self.reserved = Decimal(carry["original_429"]["reserved_usd"])
        self.max_usd = float(self.study_cap - self.reserved)

    def close_job(self, result, receipt):
        unknown = sum(c["unknown_count"] for c in result["billing_by_role"].values())
        if bool(unknown) != self.unknown_cost:
            raise ContractError("Transport and journal disagree on an unpriced request")
        if unknown == 1 and receipt is not None and receipt["cost_status"] == "unknown":
            self.reserved += Decimal(receipt["reserved_usd"])
            self.max_usd = float(self.study_cap - self.reserved)
            self.unknown_cost = False


def cooldown_seconds(receipt, minimum, now):
    if receipt is None:
        return 0
    external = receipt.get("retry_after_seconds") or 0
    date = receipt.get("retry_after_utc")
    if date:
        external = max(external, (datetime.fromisoformat(date) - now).total_seconds())
    return max(minimum, external)


def run_pending(rows, execute, guard, persist, cooldown, sleep=time.sleep):
    for row in rows:
        row["status"] = "planned"
    persist(rows)
    for position, row in enumerate(rows):
        reason = guard.stop()
        if reason:
            for pending in rows[position:]:
                pending.update(status="not_started", reason=reason)
            persist(rows)
            return reason
        row["status"] = "started"
        persist(rows)
        try:
            result, receipt = execute(row)
            row.update(result=result, status=result["status"])
            if receipt:
                row["http_429"] = receipt
            guard.close_job(result, receipt)
            persist(rows)
        except Exception as error:
            row.update(status="interrupted", reason="controller_error", error_type=type(error).__name__)
            for pending in rows[position + 1:]:
                pending.update(status="not_started", reason="controller_error")
            persist(rows)
            raise
        delay = cooldown_seconds(receipt, cooldown, datetime.now(timezone.utc))
        if delay and position + 1 < len(rows) and not guard.stop():
            sleep(min(delay, max(0, guard.deadline - time.monotonic())))
    return guard.stop()


def preflight(workspace):
    raw, control, rows, writers, carry = load_parent(workspace)
    key = lambda r: (r["task_id"], r["repetition"], r["arm"])
    relevant = [writers[key(r)] for r in rows if r["arm"] in WRITTEN_ARMS]
    for writer in relevant:
        bank = read_form_bank(raw / writer["directory"], expected_log_sha256=writer["result"]["log_sha256"])
        if (bank["run_id"] != writer["run_id"] or bank["snapshot_id"] != writer["result"]["snapshot_id"]
                or (bank["task_id"], bank["repetition"], bank["arm"]) != key(writer)):
            raise ContractError("Supplement allocation has a different writer")
    return dict(parent_completion_sha256=COMPLETION, parent_implementation=BASE_COMMIT,
                parent_guards=control["profile"]["guards"], started_at=control["started_at"], untouched=len(rows),
                sealed_writer_banks_required=len(relevant), scope=[rows[0]["index"], rows[-1]["index"]], **carry)


def run_supplement(profile, workspace, checkout):
    workspace, root = Path(workspace).resolve(), ROOT.resolve()
    if (profile.get("format") != PROFILE_FORMAT or profile.get("status") != "ready" or
            profile.get("source_root") != SOURCE_ROOT or profile.get("output_root") != OUT_ROOT
            or profile.get("http_429_cooldown_seconds") != 60):
        raise ContractError("A frozen, scoped supplement profile is required")
    identity = implementation_identity(root)
    if profile["implementation_commit"] != identity["implementation_commit"]:
        raise ContractError("Supplement runtime differs from the frozen commit")
    destination = safe_path(workspace, OUT_ROOT)
    if destination.exists() or not os.environ.get("OPENROUTER_API_KEY"):
        raise ContractError("Supplement needs an unused output root and an available credential")
    raw, control, rows, writers, carry = load_parent(workspace)
    recorded = read_json(asset_text(workspace, profile["preflight_ref"]).encode())
    if (recorded["implementation_commit"] != identity["implementation_commit"] or
            recorded["parent"] != preflight(workspace) or carry["pending_sha256"] != profile["pending_sha256"]):
        raise ContractError("Supplement preflight changed before launch")
    parent_profile = control["profile"]
    if parent_profile != read_json(asset_text(workspace, profile["parent_profile_ref"]).encode()):
        raise ContractError("Supplement differs from the original frozen settings")
    design = read_json(asset_text(root, parent_profile["design_ref"]).encode())
    asset_text(root, parent_profile["research_ref"])
    if design != control["design"] or parent_profile["design_ref"]["path"] != DESIGN:
        raise ContractError("Original and current designs differ")
    plan = prepare(root, workspace, checkout, design)
    if plan["summary"] != control["preflight"]:
        raise ContractError("Native benchmark, source bytes or original allocations changed")
    models = live_models(plan["connection"])
    if endpoint_terms(models) != endpoint_terms(control["model_catalog_entries"]):
        raise ContractError("Endpoint or prices differ from the original frozen study")
    if identity != implementation_identity(root):
        raise ContractError("Runtime changed during preflight")
    guard = SupplementGuard(parent_profile["guards"], control["started_at"], carry)
    limits = design["second_pass"]["proposed_limits"]
    clients = {role: OpenRouterClient(model, guard, route=plan["connection"][role]["provider"],
        max_input_bytes=limits["max_input_bytes"], timeout_seconds=parent_profile["http_timeout_seconds"])
        for role, model in models.items()}
    destination.mkdir(exist_ok=False)
    (destination / "jobs").mkdir()
    write_json(destination / "control.json", dict(profile=profile, implementation=identity, carry=carry,
        original_started_at=control["started_at"], started_at=datetime.now(timezone.utc).isoformat(),
        model_catalog_entries=models, source_completion_sha256=COMPLETION, source_root=SOURCE_ROOT))

    def execute(row):
        task, rep, arm = row["task_id"], row["repetition"], row["arm"]
        cfg = solve_connection(plan["connection"], design, row, parent_profile["guards"]["max_usd"])
        orch, native = build_baseline_orchestrator(cfg, plan["tasks"][task])
        writer = writers.get((task, rep, arm))
        bank = read_form_bank(raw / writer["directory"], expected_log_sha256=writer["result"]["log_sha256"]) if writer else None
        source = plan["sources"][task]
        before_calls, before_cost = guard.calls, guard.reported_usd
        with Journal(destination / row["directory"], new_id("forms"), contract_version="1.2") as journal:
            result = run_forms_solve(journal, arm=arm, task_id=task, repetition=rep, run_id=row["run_id"],
                connection=cfg, prepared=(orch, native, models, plan["modules"]), guard=guard, clients=clients,
                limits=limits, instruction=plan["instruction"] if arm != "no_memory" else "", writer_bank=bank,
                public_record=source.public_context if arm == "public_record" else None,
                public_context_sha256=source.public_context_sha256 if arm not in {"no_memory", "empty_preload"} else None)
            result["log_sha256"] = digest(journal.path.read_bytes())
            result["billing_by_role"] = request_usage(journal)
            billed = totals([dict(result=result)])
            if (result["run_id"] != row["run_id"] or billed["http_calls"] != guard.calls - before_calls
                    or abs(float(billed["known_usd"]) - (guard.reported_usd - before_cost)) > 1e-8):
                raise ContractError("Supplement run identity or per-job billing differs from the journal")
            receipt = rate_limit_evidence(result, journal.events.values(), journal.blobs, models)
        return result, receipt

    ledger = destination / "allocations.json"
    reason = run_pending(rows, execute, guard, lambda rs: write_json(ledger, rs), profile["http_429_cooldown_seconds"])
    billing = totals(rows)
    summary = dict(stop_reason=reason, supplement=billing, ended_at=datetime.now(timezone.utc).isoformat(),
        combined_http_calls=guard.calls, combined_known_usd=str(Decimal(carry["known_usd"]) + Decimal(billing["known_usd"])),
        combined_unknown_calls=carry["unknown_calls"] + billing["unknown_calls"], combined_reserved_usd=str(guard.reserved),
        other_unpriced_stop=guard.unknown_cost, allocation_sha256=digest(ledger.read_bytes()),
        control_sha256=digest((destination / "control.json").read_bytes()))
    write_json(destination / "completion.json", summary)
    return summary
