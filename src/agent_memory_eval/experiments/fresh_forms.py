"""Collect new customer experiences, then compare four writing conditions on those same tasks."""
from copy import deepcopy
from decimal import Decimal
import os
from pathlib import Path

from agent_memory_eval.adapters.tau2 import PIN, native_modules, verify_checkout
from agent_memory_eval.analysis.forms_results import checked_rows
from agent_memory_eval.contracts.connection import validate_profile
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import (
    FORMS, BatchGuard, endpoint_terms, execute_prepared, live_models, preflight_sizes, solve_connection,
)
from agent_memory_eval.experiments.forms_write import FormSource, _project_context
from agent_memory_eval.experiments.prompt_comparison import CONTROLS, SLUG
from agent_memory_eval.models.openrouter import request_reservation_usd
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.experience import _closed_run, _pair, _source
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.runner.tau2_baseline import native_config

FORMAT = "agent-memory-new-customer-design/v1"


def scenario_customer(task, db):
    text = canonical(task["user_scenario"]).decode()
    ids = {u for u in db["users"] if u in text}
    ids.update(v["user_id"] for k, v in db["reservations"].items() if k in text)
    if len(ids) != 1:
        raise ContractError("Customer must resolve uniquely from the scenario: " + task["id"])
    return ids.pop()


def eligible_customers(config, tasks, splits, db):
    prior = {scenario_customer(tasks[t], db) for t in splits["train"]}
    groups = {}
    for tid in splits[config["split"]]:
        t = tasks[tid]
        criteria = t["evaluation_criteria"]
        identity = scenario_customer(t, db)
        if (identity not in prior and criteria["reward_basis"] == ["DB", "COMMUNICATE"]
                and not criteria["communicate_info"] and any(a["name"] in config["write_tools"] for a in criteria["actions"])):
            groups[tid] = "customer-" + digest(identity.encode())[:12]
    if (sorted(groups, key=int) != config["tasks"] or len(set(groups.values())) != len(groups)
            or set(splits["train"]) & set(splits[config["split"]])):
        raise ContractError("The fresh task or customer selection differs from the preregistration")
    return groups


def schedule(design):
    stage = design["stage"]
    blocks = []
    sequence = []
    for seeds in design["model"]["seed_pairs"]:
        rep = seeds["repetition"]
        def rank(*parts):
            return digest(canonical([design["allocation"]["namespace"], rep, *parts]))
        tasks = [r["task_id"] for r in design["source"]["cohort"]]
        for t in sorted(tasks, key=lambda v: rank(v, "task")):
            write = [] if stage == "source" else sorted(FORMS, key=lambda a: rank(t, "write", a))
            solve = ["no_memory"] if stage == "source" else sorted((*FORMS, *CONTROLS), key=lambda a: rank(t, "solve", a))
            blocks.append(dict(task_id=t, repetition=rep, write=write, solve=solve))
    for phase in ("write", "solve"):
        sequence.extend([phase, b["repetition"], b["task_id"], a] for b in blocks for a in b[phase])
    return blocks, sequence


def planned_jobs(design):
    blocks, seq = schedule(design)
    if (design["allocation"]["blocks"] != blocks or design["allocation"]["ordered_jobs_sha256"] != digest(canonical(seq))
            or design["source"]["cohort_sha256"] != digest(canonical(design["source"]["cohort"]))):
        raise ContractError("Fresh-customer assignment or source changed")
    return [dict(index=i, phase=p, repetition=r, task_id=t, arm=a) for i, (p, r, t, a) in enumerate(seq, 1)]


def experiences(parent, config_hash, expected_design):
    """Reopen the first pass. Only verified final worker inputs and public replies reach writers."""
    control = read_json((parent / "control.json").read_bytes())
    design, completion, rows, _ = checked_rows(parent)
    if design != expected_design or control["profile"]["config_sha256"] != config_hash:
        raise ContractError("The first pass must use the identical preregistered study and schedule")
    sources, entries, missing = {}, [], []
    groups = {r["task_id"]: r["source_group"] for r in design["source"]["cohort"]}
    for row in rows:
        tid, result = row["task_id"], row.get("result", {})
        cat = dict(task_id=tid, source_group=groups[tid], run_id=row["run_id"])
        entries.append(cat)
        if result.get("grading_status") != "graded":
            missing.append(tid)
            continue
        sha = result["log_sha256"]
        folder = safe_path(parent, row["directory"])
        with _source(folder, sha) as (scan, blobs, _, _):
            start, end, grade = _closed_run(scan, row["run_id"])
            if (start["record_kind"] != "observed" or start["condition_id"] != "no_memory"
                    or start["payload"]["bank_id"] is not None or start["payload"]["source_run_ids"]
                    or grade["payload"]["official_success"] is not result["official_success"]):
                raise ContractError("The first pass must be a fresh measured no-memory episode")
            req, fin = _pair(scan, row["run_id"])
            projected = _project_context(blobs.get(req["payload"]["request_ref"]), blobs.get(fin["payload"]["response_ref"]))
            data = canonical(projected)
            if projected["task_context"] != blobs.get(start["payload"]["task_ref"]):
                raise ContractError("Initial native worker policy differs from its recorded public input")
            sources[tid] = FormSource(tid, row["run_id"], sha, end["event_id"], end["event_seq"], data.decode(),
                                      digest(data), len(data), result["official_success"])
            cat.update(source_log_sha256=sha, public_context_sha256=digest(data), public_context_bytes=len(data))
    return sources, sorted(entries, key=lambda e: int(e["task_id"])), missing


class CostReservations:
    """Hold the registered maximum for a single identifiable, unpriced request."""
    def __init__(self, destination, policy, guard, models):
        self.destination, self.policy, self.guard, self.models = destination, policy, guard, models
        self.limit = Decimal(str(guard.max_usd))
        self.reserved, self.count = Decimal(0), 0

    def __call__(self, row):
        result = row["result"]
        if self.count >= self.policy["maximum_per_stage"] or sum(v["unknown_count"] for v in result["billing_by_role"].values()) != 1:
            return False
        folder = safe_path(self.destination, row["directory"])
        scan, _, _ = read_experiment(folder)
        events = list(scan["events"].values())
        unknown = [e["payload"] for e in events if e["type"] == "model.request_finished" and e["payload"]["cost_status"] == "unknown"]
        if len(unknown) != 1:
            return False
        f = unknown[0]
        if f["status"] != "error":
            return False
        blobs = BlobStore(folder)
        if f["delivery_status"] == "response_received" and f["response_ref"]:
            error = blobs.get(f["response_ref"]).get("error")
            if not isinstance(error, dict) or error.get("code") not in self.policy["codes"]:
                return False
            code = error["code"]
        elif (self.policy.get("reserve_uncertain_transport") and result.get("error_code") == "TRANSPORT_UNKNOWN"
              and f["delivery_status"] in {"unknown", "response_received"} and f["response_ref"] is None
              and not scan.get("pending_requests")):
            code = "TRANSPORT_UNKNOWN"
        else:
            return False
        req = next(e["payload"] for e in events if e["type"] == "model.request_started" and e["payload"]["request_id"] == f["request_id"])
        role = req["actor_role"]
        reserve = request_reservation_usd(blobs.get(req["request_ref"]), self.models["worker" if role == "writer" else role])
        self.reserved += reserve
        self.count += 1
        self.guard.max_usd = float(self.limit - self.reserved)
        result["unpriced_reservation"] = dict(request_id=f["request_id"], code=code, role=role, usd=str(reserve),
                                              request_ref=req["request_ref"], response_ref=f["response_ref"])
        return True


def prepare_study(root, workspace, checkout, config, config_hash, stage):
    if (config["format"] != "agent-memory-new-customer-forms/v1" or stage not in ("source", "comparison")
            or config["status"] != "ready" or config["split"] != "test" or tuple(config["forms"]) != FORMS
            or tuple(config["controls"]) != CONTROLS or config["read_mode"] != "preload"
            or config["source_name"] == config["comparison_name"] or not all(SLUG.fullmatch(config[k]) for k in ("source_name", "comparison_name"))):
        raise ContractError("Invalid fresh-customer protocol")
    checkout = verify_checkout(checkout)
    modules = native_modules(checkout)
    from loguru import logger
    from tau2.registry import registry
    logger.disable("tau2")
    folder = checkout / "data/tau2/domains/airline"
    raw = {name: read_json(asset_text(folder, dict(path=name, sha256=sha)).encode()) for name, sha in config["native_assets"].items()}
    tasks = {t["id"]: t for t in raw["tasks.json"]}
    groups = eligible_customers(config, tasks, raw["split_tasks.json"], raw["db.json"])
    factory = registry.get_env_constructor("airline")
    for tid in groups:
        env = factory()
        initial = env.get_db_hash()
        if tasks[tid]["initial_state"] is not None:
            raise ContractError("Fresh-customer reference needs explicit initial state handling")
        for a in tasks[tid]["evaluation_criteria"]["actions"]:
            env.make_tool_call(a["name"], requestor=a.get("requestor", "assistant"), **a["arguments"])
        if env.get_db_hash() == initial:
            raise ContractError("A selected task does not require a different final database")
    base = read_json(asset_text(root, config["base_design_ref"]).encode())
    connection = read_json(asset_text(root, base["model"]["connection_profile_ref"]).encode())
    validate_profile(connection)
    writing = deepcopy(base["writing"])
    writing["lines"] = {a: writing["lines"][a] for a in FORMS}
    solve = {k: deepcopy(base["second_pass"][k]) for k in ("proposed_limits", "solve_instruction_ref", "no_memory", "preload")}
    solve.update(primary_arms=list(FORMS), matched_controls=list(CONTROLS), read_mode="preload", diagnostic_task_ids=[])
    instruction = asset_text(root, solve["solve_instruction_ref"])
    common = asset_text(root, writing["common_instruction_ref"])
    prompt_hashes = {a: digest((common.rstrip("\r\n") + "\n" + line).encode()) for a, line in writing["lines"].items()}
    initial = [dict(task_id=t, source_group=groups[t]) for t in config["tasks"]]
    def assemble(which, cohort, seeds, sets):
        source = dict(split=config["split"], benchmark_revision=PIN, feedback_level="none",
                      cohort=cohort, cohort_sha256=digest(canonical(cohort)))
        d = dict(format=FORMAT, stage=which, source=source, writing=writing, writer_prompt_hashes=prompt_hashes, second_pass=solve,
                 model=dict(base["model"], seed_pairs=seeds), task_sets=sets,
                 allocation=dict(namespace=config[which + "_name"]))
        blocks, seq = schedule(d)
        d["allocation"].update(blocks=blocks, ordered_jobs_sha256=digest(canonical(seq)))
        return d
    seed = config["source_seed"]
    design = assemble("source", initial, [dict(repetition=1, writer=seed, native_and_solver=seed)], {})
    sources, missing = {}, []
    if stage == "comparison":
        parent = safe_path(workspace, "runs/" + config["source_name"])
        sources, cohort, missing = experiences(parent, config_hash, design)
        sets = {key: [t for t in config["tasks"] if t in sources and sources[t].official_success is flag]
                for key, flag in (("first_success", True), ("first_failure", False))}
        design = assemble(stage, cohort, config["seed_pairs"], sets)
        design["source"]["parent_completion"] = dict(name=parent.name, sha256=digest((parent/"completion.json").read_bytes()))
    jobs = planned_jobs(design)
    for row in jobs:
        native_config(solve_connection(connection, design, row, config[stage + "_guards"]["max_usd"]))
    summary = dict(native_split=config["split"], task_ids=config["tasks"], customer_groups=len(set(groups.values())),
                   stage=stage, jobs=len(jobs), graded_source_tasks=list(sources), missing_sources=missing,
                   task_sets=design["task_sets"], schedule_sha256=design["allocation"]["ordered_jobs_sha256"],
                   writes=sum(r["phase"] == "write" for r in jobs), solves=sum(r["phase"] == "solve" for r in jobs))
    if sources:
        available = [r for r in jobs if r["task_id"] in sources]
        summary.update(preflight_sizes(available, design, sources, tasks, connection, instruction, common))
    return design, dict(jobs=jobs, modules=modules, tasks=tasks, sources=sources, missing_sources=missing,
                        connection=connection, instruction=instruction, summary=summary)


def run_study(root, workspace, checkout, config_path, stage, *, live=False):
    root, workspace = Path(root).resolve(), Path(workspace).resolve()
    raw = Path(config_path).read_bytes()
    cfg = read_json(raw)
    design, plan = prepare_study(root, workspace, checkout, cfg, digest(raw), stage)
    if not live:
        return plan["summary"]
    identity = implementation_identity(root)
    name = cfg[stage + "_name"]
    out = safe_path(workspace, "runs/" + name)
    if out.exists() or not os.environ.get("OPENROUTER_API_KEY"):
        raise ContractError("Live runs require a fresh output and a configured key")
    expected = read_json(asset_text(root, cfg["endpoint_ref"]).encode())["models"]
    models = live_models(plan["connection"])
    if endpoint_terms(models) != endpoint_terms(expected) or implementation_identity(root) != identity:
        raise ContractError("Frozen implementation, provider routing or pricing changed")
    guard = BatchGuard(**cfg[stage + "_guards"])
    profile = dict(cfg, name=name, stage=stage, config_sha256=digest(raw), guards=cfg[stage + "_guards"],
                   implementation_commit=identity["implementation_commit"])
    accounting = CostReservations(out, cfg["unpriced_http"], guard, models)
    return execute_prepared(profile, root, out, identity, design, plan, models, guard, close_cost=accounting)
