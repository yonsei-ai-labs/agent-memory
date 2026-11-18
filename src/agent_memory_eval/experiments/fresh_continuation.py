"""Continue only untouched assignments from the scoped four-form airline comparison."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import shutil
import time

from agent_memory_eval.analysis.forms_results import checked_rows
from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.experiments.forms_batch import BatchGuard, endpoint_terms, live_models, run_jobs, write_json
from agent_memory_eval.experiments.forms_supplement import cooldown_seconds, solve_recorded_row, totals
from agent_memory_eval.experiments.fresh_forms import CostReservations, prepare_study
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.recording.journal import BlobStore, safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity


AMENDMENT = "protocols/pilots/tau2-new-customer-forms-continuation-v1.json"
CONFIG = "protocols/pilots/tau2-new-customer-forms-v1.json"


def parent_evidence(root, workspace):
    amendment = read_json((root / AMENDMENT).read_bytes())
    config = (root / CONFIG).read_bytes()
    cfg = read_json(config)
    if digest(config) != amendment["config_sha256"] or amendment["parent"] != cfg["comparison_name"]:
        raise ContractError("Continuation configuration differs from the original study")
    raw = safe_path(workspace, "runs/" + amendment["parent"])
    if digest((raw / "completion.json").read_bytes()) != amendment["parent_completion_sha256"]:
        raise ContractError("Continuation parent seal differs")
    design, seal, rows, _ = checked_rows(raw)
    ctl = read_json((raw / "control.json").read_bytes())
    pending = [r["index"] for r in rows if r["status"] == "not_started"]
    if (pending != list(range(amendment["first_untouched"], amendment["last_untouched"] + 1))
            or seal["stop_reason"] != "unknown_cost" or any(
                r["phase"] != "solve" or r.get("reason") != "unknown_cost" for r in rows if r["index"] in pending)):
        raise ContractError("Only the originally untouched solver assignments can continue")
    errors = [r for r in rows if r.get("result", {}).get("unknown_cost")]
    if [r["index"] for r in errors] != amendment["received_unpriced_parent_jobs"]:
        raise ContractError("Unexpected unpriced parent assignments")
    limits = cfg["comparison_guards"]
    check = CostReservations(raw, dict(cfg["unpriced_http"], maximum_per_stage=len(errors)),
                             BatchGuard(**limits), ctl["model_catalog_entries"])
    for r in errors:
        original = deepcopy(r["result"].get("unpriced_reservation"))
        if not check(r) or (original and original != r["result"]["unpriced_reservation"]):
            raise ContractError("The original unpriced wire could not be verified")
    carry = totals(rows)
    carry.update(reserved_usd=str(check.reserved), original_completion_reserved_usd=seal["reserved_usd"])
    return amendment, cfg, raw, ctl, design, rows, carry


def verify_continuation(root, workspace, run):
    amendment, cfg, _, ctl, design, old, carry = parent_evidence(root, workspace)
    current = read_json((run / "control.json").read_bytes())
    rows = read_json((run / "allocations.json").read_bytes())
    seal = read_json((run / "completion.json").read_bytes())
    cutoff = amendment["first_untouched"] - 1
    if (run.resolve() != safe_path(workspace, "runs/" + amendment["destination"]).resolve()
            or current.get("continuation") != dict(amendment=amendment, carry=carry, parent_implementation=ctl["implementation"])
            or current["design"] != design or current["source_outcomes"] != ctl["source_outcomes"]
            or current["profile"]["guards"] != cfg["comparison_guards"]
            or endpoint_terms(current["model_catalog_entries"]) != endpoint_terms(ctl["model_catalog_entries"])
            or seal["control_sha256"] != digest((run / "control.json").read_bytes())
            or seal["allocation_sha256"] != digest((run / "allocations.json").read_bytes())
            or len(rows) != len(old) or rows[:cutoff] != old[:cutoff]):
        raise ContractError("Continuation does not preserve the frozen parent, settings, or prefix")
    for parent, child in zip(old[cutoff:], rows[cutoff:]):
        if any(parent[k] != child[k] for k in parent if k not in {"status", "reason"}):
            raise ContractError("An original untouched assignment was replaced")
    return len(rows) - cutoff + len(amendment["received_unpriced_parent_jobs"])


def continue_study(root, workspace, *, live=False):
    amendment, cfg, raw, ctl, design, rows, carry = parent_evidence(root, workspace)
    out = safe_path(workspace, "runs/" + amendment["destination"])
    check, plan = prepare_study(root, workspace, workspace / "data/tau2-bench", cfg, amendment["config_sha256"], "comparison")
    if check != design:
        raise ContractError("Current native setup differs from the original")
    closed, pending = rows[:amendment["first_untouched"] - 1], rows[amendment["first_untouched"] - 1:]
    summary = dict(source_completion_sha256=amendment["parent_completion_sha256"], inherited=len(closed),
                   untouched=len(pending), original_shared_guards=cfg["comparison_guards"], carry=carry)
    if not live:
        return summary
    identity = implementation_identity(root)
    if out.exists() or not os.environ.get("OPENROUTER_API_KEY"):
        raise ContractError("Live continuation requires a new output and the configured route")
    models = live_models(plan["connection"])
    if endpoint_terms(models) != endpoint_terms(ctl["model_catalog_entries"]) or implementation_identity(root) != identity:
        raise ContractError("Provider, pricing or implementation changed")
    limits = cfg["comparison_guards"]
    deadline = datetime.fromisoformat(ctl["started_at"]) + timedelta(seconds=limits["max_seconds"])
    seconds = (deadline - datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0:
        raise ContractError("Original shared deadline has elapsed")
    guard = BatchGuard(**dict(limits, max_seconds=seconds))
    guard.calls, guard.reported_usd = carry["http_calls"], float(carry["known_usd"])
    reserve = CostReservations(out, dict(cfg["unpriced_http"], maximum_per_stage=len(pending)), guard, models)
    reserve.reserved = Decimal(carry["reserved_usd"])
    guard.max_usd = float(reserve.limit - reserve.reserved)
    writers = {(r["task_id"], r["repetition"], r["arm"]): r for r in closed if r["phase"] == "write"}
    bindings = {(rep, task, arm): r for (task, rep, arm), r in writers.items()}
    clients = {role: OpenRouterClient(models[role], guard, route=plan["connection"][role]["provider"],
                                      max_input_bytes=design["second_pass"]["proposed_limits"]["max_input_bytes"],
                                      timeout_seconds=cfg["http_timeout_seconds"]) for role in models}
    out.mkdir(exist_ok=False)
    (out / "jobs").mkdir()
    for r in closed:
        shutil.copytree(safe_path(raw, r["directory"]), safe_path(out, r["directory"]))
    for r in pending:
        r.pop("reason")
        r["status"] = "planned"
    profile = dict(ctl["profile"], name=out.name, implementation_commit=identity["implementation_commit"])
    control = dict(ctl, profile=profile, implementation=identity, model_catalog_entries=models,
                   continued_at=datetime.now(timezone.utc).isoformat(),
                   continuation=dict(amendment=amendment, carry=carry, parent_implementation=ctl["implementation"]))
    write_json(out / "control.json", control)
    ledger = out / "allocations.json"
    delays = amendment["cooldown_seconds"]
    consecutive = 0

    def execute(row, writer):
        nonlocal consecutive
        result, _ = solve_recorded_row(row, raw, out, plan, design, models, clients, guard, writers,
                                       limits["max_usd"], lambda *args: None)
        if not result["unknown_cost"]:
            consecutive = 0
        return result

    def close(row):
        nonlocal consecutive
        if not reserve(row):
            return False
        receipt = row["result"]["unpriced_reservation"]
        err = BlobStore(out / row["directory"]).get(receipt["response_ref"])["error"]
        delay = cooldown_seconds(err, delays[min(consecutive, len(delays) - 1)], datetime.now(timezone.utc))
        consecutive += 1
        if row["index"] < amendment["last_untouched"]:
            write_json(ledger, closed + pending)
            time.sleep(min(delay, max(0, guard.deadline - time.monotonic())))
        return True

    stop = run_jobs(pending, execute, guard, lambda values: write_json(ledger, closed + values),
                    written_arms=cfg["forms"], initial_writers=bindings, close_cost=close)
    final = dict(stop_reason=stop, http_calls=guard.calls, reported_usd=guard.reported_usd,
                 unknown_cost=any(r.get("result", {}).get("unknown_cost") for r in rows), reserved_usd=str(reserve.reserved),
                 ended_at=datetime.now(timezone.utc).isoformat(), allocation_sha256=digest(ledger.read_bytes()),
                 control_sha256=digest((out / "control.json").read_bytes()))
    write_json(out / "completion.json", final)
    return final
