"""Compare two retrieval policies using the exact banks of a sealed prompt comparison."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import (
    BatchGuard, endpoint_terms, execute_prepared, live_models, ordered_jobs, prepare,
)
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.experiments.prompt_comparison import SLUG, _keys, _schedule, compile_config
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity


def parent_bank_rows(parent, pin):
    """Reopen the exact parent seals and check each writer's scheduled identity."""
    seal = read_json(asset_text(parent, dict(path="completion.json", sha256=pin["completion_sha256"])).encode())
    rows = read_json(asset_text(parent, dict(path="allocations.json", sha256=seal["allocation_sha256"])).encode())
    ctl = read_json(asset_text(parent, dict(path="control.json", sha256=seal["control_sha256"])).encode())
    expected = ordered_jobs(ctl["design"])
    if (ctl["profile"]["config_sha256"] != pin["config_sha256"] or len(rows) != len(expected)
            or any(any(r[k] != v for k, v in e.items()) for r, e in zip(rows, expected))):
        raise ContractError("Parent prompt run and its declared assignment differ")
    return ctl, seal, [r for r in rows if r["phase"] == "write"]


def source_banks(parent, pin):
    ctl, seal, rows = parent_bank_rows(parent, pin)
    banks = {}
    for row in rows:
        if row["status"] not in {"completed", "empty"}:
            continue
        bank = read_form_bank(safe_path(parent, row["directory"]), expected_log_sha256=row["result"]["log_sha256"])
        if any(bank[k] != row[k] for k in ("arm", "repetition", "task_id", "run_id", "status")) or bank["snapshot_id"] != row["result"]["snapshot_id"]:
            raise ContractError("Imported bank differs from its recorded writer")
        banks[row["repetition"], row["task_id"], row["arm"]] = bank
    return ctl, seal, rows, banks


def run_delivery_comparison(root, workspace, checkout, config_path, *, live=False):
    root, workspace = Path(root).resolve(), Path(workspace).resolve()
    raw = Path(config_path).read_bytes()
    config = read_json(raw)
    _keys(config, ("format", "status", "name", "parent_config_ref", "read_modes", "instruction_ref"), "delivery comparison")
    if (config["format"] != "agent-memory-prompt-delivery/v1" or config["status"] not in ("ready", "draft")
            or (live and config["status"] != "ready") or not isinstance(config["name"], str) or not SLUG.fullmatch(config["name"])
            or config["read_modes"] != ["preload", "on_demand"]):
        raise ContractError("Expected a named, paired preload and optional-query comparison")
    parent_config = read_json(asset_text(root, config["parent_config_ref"]).encode())
    expected = compile_config(root, parent_config)
    parent = safe_path(workspace, "runs/" + parent_config["name"])
    pin = dict(name=parent_config["name"], completion_sha256=digest((parent / "completion.json").read_bytes()),
               config_sha256=config["parent_config_ref"]["sha256"])
    ctl, seal, inputs, banks = source_banks(parent, pin)
    if ctl["design"] != expected or config["name"] == parent.name:
        raise ContractError("Frozen banks come from another prompt comparison")
    plan = prepare(root, workspace, checkout, expected)
    design = deepcopy(expected)
    design.update(format="agent-memory-prompt-delivery-design/v1", writer_parent=pin, delivery_arms={})
    for c in design["conditions"]:
        for mode in config["read_modes"]:
            design["delivery_arms"][c["id"] + "__" + mode] = dict(writer=c["id"], read_mode=mode)
    design["second_pass"].update(solve_instruction_ref=config["instruction_ref"], primary_arms=list(design["delivery_arms"]),
                                 matched_controls=[], read_mode="paired_delivery")
    design["allocation"]["namespace"] = config["name"]
    blocks, seq = _schedule(design)
    design["allocation"].update(blocks=blocks, ordered_jobs_sha256=digest(canonical(seq)))
    rows = ordered_jobs(design)
    writer_rows = {(r["repetition"], r["task_id"], r["arm"]): r for r in inputs}
    bindings = {}
    for r in rows:
        src = design["delivery_arms"][r["arm"]]["writer"]
        bindings[r["repetition"], r["task_id"], r["arm"]] = writer_rows[r["repetition"], r["task_id"], src]
    summary = dict(public_sources=len(plan["sources"]), writers=0, episodes=len(rows), borrowed_banks=len(banks),
                   empty_banks=sum(b["status"] == "empty" for b in banks.values()), parent=pin,
                   ordered_jobs_sha256=design["allocation"]["ordered_jobs_sha256"])
    plan.update(jobs=rows, summary=summary, instruction=asset_text(root, config["instruction_ref"]))
    if not live:
        return summary
    identity = implementation_identity(root)
    destination = safe_path(workspace, "runs/" + config["name"])
    if destination.exists() or not os.environ.get("OPENROUTER_API_KEY") or seal["unknown_cost"]:
        raise ContractError("A new output root, configured key and reconciled shared budget are required")
    budget = parent_config["guards"]
    start = datetime.fromisoformat(ctl["started_at"])
    remaining = dict(max_calls=budget["max_calls"] - seal["http_calls"],
                     max_usd=float(Decimal(str(budget["max_usd"])) - Decimal(str(seal["reported_usd"]))),
                     max_seconds=budget["max_seconds"] - (datetime.now(timezone.utc) - start).total_seconds())
    guard = BatchGuard(**remaining)
    models = live_models(plan["connection"])
    if endpoint_terms(models) != endpoint_terms(ctl["model_catalog_entries"]) or implementation_identity(root) != identity:
        raise ContractError("Endpoint terms or frozen runtime changed before the first delivery send")
    profile = dict(config, config_sha256=digest(raw), implementation_commit=identity["implementation_commit"],
                   output_root="runs/" + config["name"], guards=remaining, shared_limits=budget,
                   http_timeout_seconds=parent_config["http_timeout_seconds"])
    return execute_prepared(profile, root, destination, identity, design, plan, models, guard,
                            imported_writers=(parent, bindings))
