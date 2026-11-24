"""Run the preregistered two-pass allocation once, with isolated per-job evidence."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile

from agent_memory_eval.adapters.tau2 import PIN, native_modules, verify_checkout
from agent_memory_eval.contracts.connection import validate_profile
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest, new_id, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.models.openrouter import (
    OpenRouterClient, ProviderError, SpendGuard, model_catalog, model_endpoints, resolve_endpoint, resolve_model,
)
from agent_memory_eval.memory.context import request_with_past_memory
from agent_memory_eval.memory.writing import writer_template
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import Journal, safe_path, sync_directory
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.runner.tau2_baseline import batch_stop, build_baseline_orchestrator, native_config


FORMS = ("fact", "episode", "lesson", "procedure")
DESIGN = "protocols/drafts/tau2-two-pass-memory-forms-v1.json"
RETAIL_RECHECK = "agent-memory-retail-recheck-design/v1"
RETAIL_FULL = "agent-memory-retail-full-design/v1"


def ordered_jobs(design):
    if design.get("format") == "agent-memory-new-customer-design/v1":
        from agent_memory_eval.experiments.fresh_forms import planned_jobs
        return planned_jobs(design)
    if design.get("format") in {"agent-memory-prompt-comparison-design/v1", "agent-memory-prompt-delivery-design/v1"}:
        from agent_memory_eval.experiments.prompt_comparison import comparison_jobs
        return comparison_jobs(design)
    allocation = design["allocation"]
    cohort = design["source"]["cohort"]
    tasks = [s["task_id"] for s in cohort]
    diagnostics = set(design["second_pass"]["diagnostic_task_ids"])
    if (len(tasks) != len(set(tasks)) or len(tasks) != 28 or not diagnostics <= set(tasks)
            or digest(canonical(cohort)) != design["source"]["cohort_sha256"]):
        raise ContractError("Source cohort or its identity changed")
    namespace = allocation["namespace"]
    def rank(*parts):
        return digest(canonical([namespace, *parts]))
    blocks = []
    for rep in (1, 2):
        for task in sorted(tasks, key=lambda task: rank(rep, task, "block")):
            write = [*FORMS, *(["summary"] if task in diagnostics else [])]
            solve = [*write, "no_memory", "empty_preload", *(["public_record"] if task in diagnostics else [])]
            blocks.append(dict(repetition=rep, task_id=task,
                write=sorted(write, key=lambda arm: rank(rep, task, "write", arm)),
                solve=sorted(solve, key=lambda arm: rank(rep, task, "solve", arm))))
    if blocks != allocation["blocks"]:
        raise ContractError("Frozen block order or condition membership differs from specification")
    rows = [[phase, block["repetition"], block["task_id"], arm]
            for phase in ("write", "solve") for block in blocks for arm in block[phase]]
    if (digest(canonical(rows)) != allocation["ordered_jobs_sha256"] or len(rows) != 584
            or sum(p == "write" for p, *_ in rows) != 232):
        raise ContractError("Incorrect allocation hash or size")
    return [dict(index=i, phase=p, repetition=r, task_id=t, arm=a) for i, (p, r, t, a) in enumerate(rows, 1)]


def write_json(path, value):
    """Atomically publish a human-readable controller ledger on the same filesystem."""
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="wb", delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write((json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
        sync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


class BatchGuard(SpendGuard):
    """A denied batch reservation ends the schedule even below the nominal spend cap."""
    denied = None

    def before(self, request, model):
        if self.denied:
            raise ProviderError(self.denied)
        try:
            super().before(request, model)
        except ProviderError as error:
            self.denied = error.code
            raise

    def stop(self):
        return batch_stop(self) or (self.denied.lower() if self.denied else None)


def live_models(connection):
    catalog = model_catalog()
    models = {}
    for actor in ("worker", "user_simulator"):
        cfg = connection[actor]
        parameters = deepcopy(cfg["parameters"])
        if actor == "worker":
            parameters.update(tools=[], tool_choice="auto")
        model = resolve_model(catalog, cfg["model_id"], cfg["parameters"])
        models[actor] = resolve_endpoint(model, model_endpoints(model["id"]), cfg["provider"], parameters)
    return models


def endpoint_terms(models):
    return {actor: dict(model=m["id"], provider=m["selected_endpoint"]["tag"],
        provider_name=m["selected_endpoint"]["provider_name"], pricing=m["selected_endpoint"]["pricing"],
        reservation_pricing=m["reservation_pricing"]) for actor, m in models.items()}


def solve_connection(connection, design, row, cap):
    cfg = deepcopy(connection)
    limits = design["second_pass"]["proposed_limits"]
    seed = next(s["native_and_solver"] for s in design["model"]["seed_pairs"] if s["repetition"] == row["repetition"])
    cfg["worker"]["parameters"]["seed"] = seed
    cfg["benchmark"].update(task_id=row["task_id"], seed=seed, max_steps=limits["native_steps"], max_errors=limits["native_errors"])
    if design.get("format") == "agent-memory-bank-transfer-shard/v1":
        cfg["benchmark"]["split"] = design["target"]["split"]
    elif design.get("format") == "agent-memory-new-customer-design/v1":
        cfg["benchmark"]["split"] = design["source"]["split"]
    elif design.get("format") in (RETAIL_RECHECK, RETAIL_FULL):
        if design["source"]["domain"] != "retail":
            raise ContractError("The retail recheck design requires the retail domain")
        cfg["benchmark"].update(domain="retail", split=design.get("target", design["source"])["split"])
    cfg["guards"] = dict(max_seconds=limits["episode_seconds"], max_model_requests=limits["max_total_requests_per_episode"],
                         max_reported_cost_usd=cap)
    return cfg


def preflight_sizes(jobs, design, sources, tasks, connection, instruction, common):
    from agent_memory_eval.experiments.forms_solve import _bank_input
    cfg = connection["worker"]
    base = dict(model=cfg["model_id"], provider=cfg["provider"], stream=False, **cfg["parameters"])
    writer_sizes = []
    solve_stress = []
    w = design["writing"]
    s = design["second_pass"]["proposed_limits"]
    policy = MemoryPolicy(True, "none", False, s["read_bank_items"], read_mode="preload",
        max_items=s["read_bank_items"], max_payload_bytes=s["read_bank_payload_bytes"], max_return_bytes=s["read_bank_return_bytes"])
    for row in jobs:
        source = sources[row["task_id"]]
        if row["phase"] == "write":
            seed = next(p["writer"] for p in design["model"]["seed_pairs"] if p["repetition"] == row["repetition"])
            template = writer_template(dict(base, seed=seed), common.rstrip("\r\n") + "\n" + w["lines"][row["arm"]])
            template["messages"] = [template.pop("system"), dict(role="user", content=source.public_context),
                                    dict(role="user", content=canonical(dict(kind="evaluation_feedback", level="none")).decode())]
            writer_sizes.append(len(canonical(template)))
        elif row["arm"] == "public_record":
            original = read_json(source.public_context.encode())
            cfg_run = solve_connection(connection, design, row, 1)
            orch, _ = build_baseline_orchestrator(cfg_run, tasks[row["task_id"]])
            items, _ = _bank_input("public_record", row["task_id"], row["repetition"], None, source.public_context,
                                   source.public_context_sha256)
            native_wire = dict(base, seed=cfg_run["benchmark"]["seed"],
                messages=[original["task_context"], *original["messages"][:-1]],
                tools=[tool.openai_schema for tool in orch.agent.tools], tool_choice="auto")
            size = len(canonical(request_with_past_memory(native_wire, policy, items, instruction=instruction)))
            if (len(canonical({k: items[0][k] for k in ("title", "body")})) > s["read_bank_payload_bytes"]
                    or len(canonical(dict(items=items))) > s["read_bank_return_bytes"]):
                raise ContractError("Public record exceeds the frozen read bank")
            solve_stress.append(size)
    if max(writer_sizes) > w["proposed_limits"]["input_bytes"] or max(solve_stress, default=0) > s["max_input_bytes"]:
        raise ContractError("Preflight request exceeds the frozen input budget")
    return dict(writer_initial_wires=len(writer_sizes), writer_initial_wire_bytes=[min(writer_sizes), max(writer_sizes)],
                public_record_archived_final_wire_checks=len(solve_stress),
                public_record_archived_final_wire_bytes=[min(solve_stress), max(solve_stress)] if solve_stress else [])


def prepare(root, workspace, checkout, design):
    from agent_memory_eval.experiments.forms_write import load_form_sources
    jobs = ordered_jobs(design)
    checkout = verify_checkout(checkout)
    modules = native_modules(checkout)
    from loguru import logger
    logger.disable("tau2")
    base = checkout / "data/tau2/domains/airline"
    tasks = {r["id"]: r for r in read_json((base / "tasks.json").read_bytes())}
    split = read_json((base / "split_tasks.json").read_bytes())["train"]
    selected = set(split) - set(design["source"]["excluded_tasks"])
    if PIN != design["source"]["benchmark_revision"] or selected != {s["task_id"] for s in design["source"]["cohort"]}:
        raise ContractError("Task selection differs from the pinned native train split")
    connection = read_json(asset_text(root, design["model"]["connection_profile_ref"]).encode())
    validate_profile(connection)
    instruction = asset_text(root, design["second_pass"]["solve_instruction_ref"])
    common = asset_text(root, design["writing"]["common_instruction_ref"])
    sources = load_form_sources(design, workspace)
    for block in design["allocation"]["blocks"]:
        native_config(solve_connection(connection, design, block, 1))
    summary = dict(writers=sum(r["phase"] == "write" for r in jobs), episodes=sum(r["phase"] == "solve" for r in jobs),
        public_sources=len(sources), successful_sources=sum(s.official_success for s in sources.values()),
        public_context_bytes_range=[f(s.public_context_bytes for s in sources.values()) for f in (min, max)],
        customer_groups=len({s["source_group"] for s in design["source"]["cohort"]}),
        ordered_jobs_sha256=design["allocation"]["ordered_jobs_sha256"], tau2_revision=PIN,
        **preflight_sizes(jobs, design, sources, tasks, connection, instruction, common))
    return dict(summary=summary, jobs=jobs, sources=sources, tasks=tasks, modules=modules, connection=connection,
                instruction=instruction)


def run_jobs(rows, execute, guard, persist, *, written_arms=(*FORMS, "summary"), initial_writers=None,
             missing_sources=(), close_cost=None):
    """Persist all assignments before HTTP, a start before side effects, and every terminal disposition."""
    writers = dict(initial_writers or {})
    persist(rows)
    stop = None
    for position, row in enumerate(rows):
        if stop is None:
            stop = guard.stop()
        key = (row["repetition"], row.get("source_task_id", row["task_id"]), row["arm"])
        writer = writers.get(key)
        if stop:
            row.update(status="not_started", reason=stop)
            continue
        if row.get("source_task_id", row["task_id"]) in missing_sources:
            row.update(status="not_started", reason="first_pass_ungraded")
            continue
        if row["phase"] == "solve" and row["arm"] in written_arms:
            if writer is None:
                raise ContractError("Solver allocation precedes its writer")
            if writer["status"] not in {"completed", "empty"}:
                row.update(status="not_started", reason="writer_incomplete")
                persist(rows)
                continue
        row["status"] = "started"
        persist(rows)
        try:
            data = execute(row, writer)
        except Exception as error:
            stop = guard.stop() or "controller_error"
            row.update(status="interrupted", reason=stop, error_type=type(error).__name__)
            for pending in rows[position + 1:]:
                pending.update(status="not_started", reason=stop)
            persist(rows)
            raise
        row.update(status=data["status"], result=data)
        if row["phase"] == "write":
            writers[key] = row
        if data["unknown_cost"]:
            guard.unknown_cost = not (close_cost and close_cost(row))
        stop = guard.stop()
        persist(rows)
    persist(rows)
    return stop


def run_batch(profile, workspace, checkout):
    root, workspace = ROOT.resolve(), Path(workspace).resolve()
    if (profile.get("format") != "agent-memory-two-pass-memory-forms-ready/v1" or profile.get("status") != "ready"
            or profile["design_ref"]["path"] != DESIGN):
        raise ContractError("Live execution requires the separate ready profile")
    identity = implementation_identity(root)
    if identity["implementation_commit"] != profile["implementation_commit"]:
        raise ContractError("Runtime differs from the frozen implementation commit")
    design = read_json(asset_text(root, profile["design_ref"]).encode())
    asset_text(root, profile["research_ref"])
    endpoint_receipt = read_json(asset_text(workspace, profile["endpoint_ref"]).encode())
    verified = read_json(asset_text(workspace, profile["preflight_ref"]).encode())
    if verified["implementation_commit"] != identity["implementation_commit"]:
        raise ContractError("Preflight and running implementation commits differ")
    destination = safe_path(workspace, profile["output_root"])
    if (destination.parent != workspace / "runs" or destination.exists()
            or destination.is_relative_to(safe_path(workspace, design["source"]["root"]))):
        raise ContractError("Live execution requires a new, isolated runs directory")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ContractError("OPENROUTER_API_KEY_MISSING")
    guard_config = profile["guards"]
    guard = BatchGuard(**guard_config)
    plan = prepare(root, workspace, checkout, design)
    if plan["summary"] != verified["native_and_source"]:
        raise ContractError("Fresh source and wire preflight differs from the frozen receipt")
    models = live_models(plan["connection"])
    if endpoint_terms(models) != endpoint_terms(endpoint_receipt["models"]):
        raise ContractError("Endpoint or price changed after the ready profile was frozen")
    if implementation_identity(root) != identity:
        raise ContractError("Runtime changed before allocation")
    return execute_prepared(profile, root, destination, identity, design, plan, models, guard)


def execute_prepared(profile, root, destination, identity, design, plan, models, guard, *, imported_writers=None,
                     close_cost=None):
    """Execute a verified schedule once, preserving every job's original journal."""
    from agent_memory_eval.experiments.forms_solve import run_forms_solve
    from agent_memory_eval.experiments.forms_write import read_form_bank, run_form_writer
    written_arms = tuple(design["writing"]["lines"])
    delivery_arms = design.get("delivery_arms", {})
    pools = plan.get("retrieval_pools")
    if pools is not None and (imported_writers is not None or not pools or set(pools) != set(delivery_arms)
            or any(delivery_arms[a].get("pool") not in {"own", "mixed"} for a in pools)):
        raise ContractError("Every registered retrieval arm needs its own verified source binding")
    writer_root, bindings = imported_writers or (destination, {})
    rows = [dict(row, status="planned", directory=f'jobs/{row["index"]:04d}', run_id=new_id("r")) for row in plan["jobs"]]
    destination.mkdir(exist_ok=False)
    (destination / "jobs").mkdir()
    control = dict(profile=profile, implementation=identity, model_catalog_entries=models,
                   source_outcomes={**dict.fromkeys(plan.get("missing_sources", [])),
                                    **{task: source.official_success for task, source in plan["sources"].items()}},
                   preflight=plan["summary"], design=design, started_at=datetime.now(timezone.utc).isoformat())
    if "task_variants" in plan:
        control["task_variants"] = plan["task_variants"]
    write_json(destination / "control.json", control)
    writer_limits = design["writing"]["proposed_limits"]
    limits = design["second_pass"]["proposed_limits"]
    def make_client(role, byte_limit):
        return OpenRouterClient(models[role], guard, route=plan["connection"][role]["provider"],
                                max_input_bytes=byte_limit, timeout_seconds=profile["http_timeout_seconds"])
    client = make_client("worker", writer_limits["input_bytes"])
    episode_clients = {role: make_client(role, limits["max_input_bytes"]) for role in models}
    def execute(row, writer):
        folder = destination / row["directory"]
        source = plan["sources"].get(row.get("source_task_id", row["task_id"]))
        scope_calls, previous_cost = guard.calls, guard.reported_usd
        with Journal(folder, new_id("forms"), contract_version="1.2") as journal:
            if row["phase"] == "write":
                result = run_form_writer(journal, design, source, repetition=row["repetition"], arm=row["arm"],
                                         client=client, guard=guard, root=root, run_id=row["run_id"])
                result.pop("bank", None)
            else:
                arm = row["arm"]
                cfg = solve_connection(plan["connection"], design, row, guard.max_usd)
                task = plan["tasks"][row["task_id"]]
                if "task_variant" in row:
                    task = plan["task_variants"][row["task_id"]][row["task_variant"]]
                orch, native = build_baseline_orchestrator(cfg, task)
                bank = (read_form_bank(safe_path(writer_root, writer["directory"]), expected_log_sha256=writer["result"]["log_sha256"])
                        if writer else None)
                result = run_forms_solve(journal, arm=arm, task_id=row["task_id"], repetition=row["repetition"],
                    run_id=row["run_id"], connection=cfg, prepared=(orch, native, models, plan["modules"]),
                    guard=guard, clients=episode_clients, limits=limits, instruction=plan["instruction"] if arm != "no_memory" else "",
                    writer_bank=bank, public_record=source.public_context if arm == "public_record" else None,
                    public_context_sha256=source.public_context_sha256 if arm not in {"no_memory", "empty_preload"} else None,
                    written_arms=written_arms, delivery=delivery_arms.get(arm) or design.get("delivery_controls", {}).get(arm),
                    expected_split=design.get("target", design["source"]).get("split", "train"),
                    source_task_id=row.get("source_task_id") if bank else None,
                    preload_notice_version=design.get("preload_notice_version") if arm != "no_memory" else None,
                    expected_domain="retail" if design.get("format") in (RETAIL_RECHECK, RETAIL_FULL) else "airline",
                    native_grader=plan.get("grader"), writer_pool=pools.get(arm) if pools is not None else None)
            if result["run_id"] != row["run_id"]:
                raise ContractError("Run differs from preallocated identity")
            result["log_sha256"] = digest(journal.path.read_bytes())
            usage = request_usage(journal)
            result["billing_by_role"] = usage
            result["unknown_cost"] = bool(result.get("unknown_cost") or any(v["unknown_count"] for v in usage.values()))
            if (sum(v["http_calls"] for v in usage.values()) != guard.calls - scope_calls or
                    abs(sum(float(v["reported_usd"]) for v in usage.values()) - (guard.reported_usd - previous_cost)) > 1e-8):
                raise ContractError("Per-job provider ledger differs from journal accounting")
        return result
    ledger = destination / "allocations.json"
    admission = plan.get("admission")
    dispatch = (lambda row, writer: admission.run(execute, row, writer)) if admission is not None else execute
    stop = run_jobs(rows, dispatch, guard, lambda values: write_json(ledger, values),
                    written_arms=() if pools is not None else (delivery_arms or written_arms), initial_writers=bindings,
                    missing_sources=plan.get("missing_sources", ()), close_cost=close_cost)
    summary = dict(stop_reason=stop, http_calls=guard.calls, reported_usd=guard.reported_usd,
        unknown_cost=guard.unknown_cost, ended_at=datetime.now(timezone.utc).isoformat(),
        allocation_sha256=digest(ledger.read_bytes()), control_sha256=digest((destination / "control.json").read_bytes()))
    if close_cost:
        summary.update(unknown_cost=any(r.get("result", {}).get("unknown_cost") for r in rows),
                       reserved_usd=str(close_cost.reserved))
    write_json(destination / "completion.json", summary)
    return summary


def request_usage(journal):
    roles = {}
    stats = {}
    for event in journal.events.values():
        pay = event["payload"]
        if event["type"] == "model.request_started":
            roles[pay["request_id"]] = pay["actor_role"]
        elif event["type"] == "model.request_finished":
            role = roles[pay["request_id"]]
            cell = stats.setdefault(role, dict(requests=0, http_calls=0, total_tokens=0, reported_usd=Decimal(0), unknown_count=0))
            cell["requests"] += 1
            cell["http_calls"] += pay["delivery_status"] != "not_submitted"
            cell["unknown_count"] += pay["cost_status"] == "unknown"
            cell["total_tokens"] += (pay.get("usage") or {}).get("total_tokens") or 0
            if pay["cost_status"] == "observed":
                cell["reported_usd"] += Decimal(str(pay["cost_usd"]))
    for cell in stats.values():
        cell["reported_usd"] = str(cell["reported_usd"])
    return stats
