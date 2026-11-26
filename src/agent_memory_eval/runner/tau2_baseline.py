"""No-memory native batches with explicit allocation, two guard scopes and one journal."""

from copy import deepcopy
import random
import sys
import time

from jsonschema import Draft202012Validator, ValidationError

from agent_memory_eval.adapters.tau2 import PIN, Tau2Bridge, native_modules, verify_checkout
from agent_memory_eval.contracts.connection import provider_route, validate_profile
from agent_memory_eval.contracts.core import ContractError, RunContext, canonical, digest, new_id, read_json
from agent_memory_eval.contracts.validation import ROOT, SCHEMA_ROOT
from agent_memory_eval.models.openrouter import (
    ProviderError, SpendGuard, model_catalog, model_endpoints, resolve_endpoint, resolve_model,
)
from agent_memory_eval.recording.provenance import RequestLimit


def compile_baseline(profile, *, require_ready=True, connection_text=None):
    try:
        canonical(profile)
    except (ValueError, TypeError) as error:
        raise ContractError("Baseline settings must be finite JSON values") from error
    schema = read_json((SCHEMA_ROOT / "baseline-screening.schema.json").read_bytes())
    try:
        Draft202012Validator(schema).validate(profile)
    except ValidationError as error:
        raise ContractError("Invalid baseline profile: " + error.message) from error
    ref = profile["connection_profile_ref"]
    if connection_text is None:
        path = (ROOT / ref["path"]).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ContractError("Connection asset is outside the repository or missing")
        connection_text = path.read_text()
    if digest(connection_text.encode()) != ref["sha256"]:
        raise ContractError("Baseline connection asset changed")
    connection = read_json(connection_text.encode())
    validate_profile(connection)
    if connection["profile_version"] != "1.1" or connection["memory_enabled"] is not False:
        raise ContractError("Baseline screening requires the current Luna/DeepSeek no-memory profile")
    if connection["benchmark"]["source_revision"] != PIN or connection["benchmark"]["split"] != "train":
        raise ContractError("Baseline screening is limited to the pinned official train split")
    selection = profile["selection"]
    if (selection["mode"] == "all_train") != (selection["task_ids"] is None):
        raise ContractError("All-train selection has no override; explicit selection needs task IDs")
    missing = [k for k in ("repetitions", "native_seeds", "schedule_seed", "benchmark_limits") if profile[k] is None]
    for scope in ("run_guards", "guards"):
        if profile[scope] is None:
            missing.append(scope)
        else:
            missing.extend(scope + "." + key for key, value in profile[scope].items() if value is None)
    if profile["native_seeds"] is not None and profile["repetitions"] is not None:
        if len(profile["native_seeds"]) != profile["repetitions"]:
            raise ContractError("One explicit native seed is required per repetition")
    ready = profile["status"] == "ready" and not missing
    if (require_ready or profile["status"] == "ready") and not ready:
        raise ContractError("Baseline profile is not ready: " + ", ".join(missing or ["status=draft"]))
    return dict(profile=deepcopy(profile), connection=connection, connection_text=connection_text,
                ready=ready, unresolved_fields=missing)


def allocations(profile, task_ids):
    """Deterministic rows; opaque run/session IDs are assigned only when recording a batch."""
    rng, rows = random.Random(profile["schedule_seed"]), []
    for repetition, seed in enumerate(profile["native_seeds"], 1):
        order = list(task_ids)
        rng.shuffle(order)
        for position, task_id in enumerate(order, 1):
            rows.append(dict(allocation_id=f"rep-{repetition:03d}-pos-{position:03d}",
                repetition=repetition, position=position, task_id=task_id, native_seed=seed))
    return rows


def select_tasks(profile, split, tasks):
    train = list(map(str, split["train"]))
    if not train or len(set(train)) != len(train):
        raise ContractError("Invalid official train split")
    ids = train if profile["selection"]["mode"] == "all_train" else profile["selection"]["task_ids"]
    if not set(ids) <= set(train) or not set(ids) <= set(tasks):
        raise ContractError("A baseline task is outside the declared official train split")
    return list(ids)


def preflight_baseline(profile, checkout, *, require_ready=False):
    """Local source/hash/split/seed checks only, without catalog or model HTTP calls."""
    plan = compile_baseline(profile, require_ready=require_ready)
    checkout = verify_checkout(checkout)
    folder = checkout / "data/tau2/domains" / plan["connection"]["benchmark"]["domain"]
    paths = [folder / name for name in ("tasks.json", "split_tasks.json", "db.json", "policy.md")]
    paths += [checkout / "src/tau2" / name for name in (
        "domains/airline/tools.py", "evaluator/evaluator.py", "evaluator/evaluator_env.py")]
    plan["assets"] = {str(p.relative_to(checkout)): p.read_text() for p in paths}
    plan["asset_hashes"] = {name: digest(text.encode()) for name, text in plan["assets"].items()}
    plan["tasks"] = {t["id"]: t for t in read_json((folder / "tasks.json").read_bytes())}
    plan["split"] = read_json((folder / "split_tasks.json").read_bytes())
    plan["task_ids"] = select_tasks(profile, plan["split"], plan["tasks"])
    if profile["native_seeds"] is not None and profile["schedule_seed"] is not None:
        plan["allocations"] = allocations(profile, plan["task_ids"])
    return plan, checkout


def resolved_connection(plan, row):
    """New batch schema authorizes only seed and limit overrides to the historical profile."""
    config = deepcopy(plan["connection"])
    config["benchmark"].update(plan["profile"]["benchmark_limits"], task_id=row["task_id"], seed=row["native_seed"])
    config["guards"] = deepcopy(plan["profile"]["run_guards"])
    config["worker"]["parameters"]["seed"] = row["native_seed"]
    return config


def native_config(config):
    from tau2.data_model.simulation import TextRunConfig
    bench = config["benchmark"]
    return TextRunConfig(domain=bench["domain"], agent="llm_agent", user="user_simulator",
        llm_agent=config["worker"]["model_id"], llm_args_agent=deepcopy(config["worker"]["parameters"]),
        llm_user=config["user_simulator"]["model_id"], llm_args_user=deepcopy(config["user_simulator"]["parameters"]),
        max_steps=bench["max_steps"], max_errors=bench["max_errors"], seed=bench["seed"],
        timeout=config["guards"]["max_seconds"], localize_db=bench["localize_db"],
        communicate_scoring=dict(scorer=bench["communicate_scorer"]), task_ids=[bench["task_id"]],
        task_split_name=bench["split"], num_trials=1, max_concurrency=1,
        **({"retrieval_config": bench["retrieval_config"]} if "retrieval_config" in bench else {}))


def build_baseline_orchestrator(config, task):
    from tau2.data_model.tasks import Task
    from tau2.runner.build import build_text_orchestrator
    native = native_config(config)
    orch = build_text_orchestrator(native, Task.model_validate(deepcopy(task)), seed=config["benchmark"]["seed"])
    return orch, native


def prepare_baseline(profile, checkout):
    plan, checkout = preflight_baseline(profile, checkout, require_ready=True)
    modules = native_modules(checkout)
    from loguru import logger
    logger.disable("tau2")
    # Validate every distinct runtime configuration before any paid request.
    for row in plan["allocations"]:
        native_config(resolved_connection(plan, row))
    catalog = model_catalog()
    models = {}
    for role in ("worker", "user_simulator"):
        settings = plan["connection"][role]
        model = resolve_model(catalog, settings["model_id"], settings["parameters"])
        parameters = deepcopy(settings["parameters"])
        if role == "worker":
            parameters.update(tools=[], tool_choice="auto")
        models[role] = resolve_endpoint(model, model_endpoints(model["id"]), provider_route(plan["connection"], role), parameters)
    return dict(plan=plan, checkout=checkout, models=models, modules=modules)


def spend_guard(limits):
    return SpendGuard(max_calls=limits["max_model_requests"], max_usd=limits["max_reported_cost_usd"],
                      max_seconds=limits["max_seconds"])


class ScopedGuard:
    """Reserve both scopes before HTTP; a rejection never counts as a submitted call."""
    def __init__(self, batch, run):
        self.batch, self.run = batch, run

    @property
    def deadline(self):
        return min(self.batch.deadline, self.run.deadline)

    @property
    def unknown_cost(self):
        return self.batch.unknown_cost or self.run.unknown_cost

    @unknown_cost.setter
    def unknown_cost(self, value):
        self.batch.unknown_cost = self.run.unknown_cost = value

    def before(self, request, model):
        old = self.run.calls, self.batch.calls
        try:
            for scope, guard in (("RUN", self.run), ("BATCH", self.batch)):
                try:
                    guard.before(request, model)
                except ProviderError as error:
                    raise ProviderError(scope + "_" + error.code) from None
        except Exception:
            self.run.calls, self.batch.calls = old
            raise

    def after(self, response):
        self.run.after(response)
        self.batch.after(response)


def batch_stop(guard):
    if guard.unknown_cost:
        return "unknown_cost"
    if guard.calls >= guard.max_calls:
        return "call_limit"
    if guard.reported_usd >= guard.max_usd:
        return "spend_limit"
    if time.monotonic() >= guard.deadline:
        return "time_limit"
    return None


def run_baseline(journal, profile, prepared, *, opener=None):
    if journal.contract_version != "1.1" or journal.scan["records"]:
        raise ContractError("Baseline batches require a new v1.1 journal")
    plan, _ = preflight_baseline(profile, prepared["checkout"], require_ready=True)
    if plan != prepared["plan"]:
        raise ContractError("Prepared baseline plan or source assets changed")
    schedule = [dict(row, run_id=new_id("r"), session_id=new_id("s")) for row in plan["allocations"]]
    control = dict(profile=deepcopy(profile), connection_text=plan["connection_text"],
        assets={name: dict(sha256=plan["asset_hashes"][name], text_ref=journal.blobs.put(text))
                for name, text in plan["assets"].items()},
        schedule=schedule, models=prepared["models"], response_cache_enabled=False,
        implementation=dict(python=sys.version.split()[0], sources={str(p.relative_to(ROOT)): digest(p.read_bytes())
            for p in sorted((ROOT / "src/agent_memory_eval").rglob("*.py"))}))
    state = deepcopy(journal.state)
    state["baseline_screening"] = dict(control_ref=journal.blobs.put(control), runtime_status="running")
    journal.append([], state=state)
    controller = RunContext(journal.experiment_id, None, None, None, "controller", "prepare", "observed")
    batch = spend_guard(profile["guards"])
    stopped = None
    for allocation_index, row in enumerate(schedule):
        stopped = stopped or batch_stop(batch)
        if stopped:
            journal.append([controller.event("business.transition", dict(origin="baseline.allocation_skipped",
                allocation_id=row["allocation_id"], run_id=row["run_id"], reason=stopped))])
            continue
        config = resolved_connection(plan, row)
        ctx = RunContext(journal.experiment_id, row["run_id"], row["session_id"], "no_memory", "diagnostic", "solve", "observed")
        journal.append([ctx.at("prepare").event("run.started", dict(bank_id=None, start_snapshot_id=None,
            fresh_session=True, source_run_ids=[], task_ref=journal.blobs.put(dict(task_id=row["task_id"]))))])
        run_guard = spend_guard(profile["run_guards"])
        guard = ScopedGuard(batch, run_guard)
        orch, report_ref, code, score, success = None, None, None, None, None
        grading, termination = "incomplete", "runtime_error"
        try:
            orch, native = build_baseline_orchestrator(config, plan["tasks"][row["task_id"]])
            tools = dict(worker=[t.openai_schema for t in orch.agent.tools],
                         user_simulator=[t.openai_schema for t in (orch.user.tools or [])])
            if any(t["function"]["name"].startswith("memory_") for ts in tools.values() for t in ts):
                raise ContractError("Baseline native tools overlap memory namespace")
            run_control = dict(config=config, native_resolved=native.model_dump(mode="json"),
                task_ref=journal.blobs.put(orch.task.model_dump(mode="json")), tools=tools,
                systems=dict(worker=orch.agent.system_prompt, user_simulator=orch.user.system_prompt))
            journal.append([ctx.at("prepare").event("business.transition", dict(origin="baseline.run_control",
                control_ref=journal.blobs.put(run_control)))])
            from tau2.runner.simulation import run_simulation
            with Tau2Bridge(journal, ctx, orch, config, prepared["models"], guard, prepared["modules"], opener=opener):
                result = run_simulation(orch)
            report_ref = journal.blobs.put(result.model_dump(mode="json"))
            score, grading = result.reward_info.reward, "graded"
            termination = getattr(result.termination_reason, "value", result.termination_reason)
            success = score == 1.0
        except Exception as error:
            code = "RUN_REQUEST_LIMIT" if isinstance(error, RequestLimit) else getattr(error, "code", type(error).__name__)
            journal.append([ctx.event("runtime.error", dict(code=code))])
            if orch is not None:
                report_ref = journal.blobs.put(dict(partial_trajectory=[m.model_dump(mode="json") for m in orch.get_trajectory()]))
        native_counts = dict(native_steps=orch.step_count if orch else 0, native_tool_errors=orch.num_errors if orch else 0)
        journal.append([ctx.at("grade").event("business.transition", dict(origin="baseline.run_result",
            report_ref=report_ref, error_code=code, http_calls=run_guard.calls,
            reported_cost_usd=run_guard.reported_usd, unknown_cost=guard.unknown_cost,
            final_db_hash=orch.environment.get_db_hash() if orch else None, **native_counts))])
        journal.append([ctx.at("grade").event("outcome.scored", dict(grading_status=grading,
            official_success=success, official_score=score, official_artifact_ref=report_ref if grading == "graded" else None))])
        # Prior request manifests and registries remain immutable in their original commits.
        # The next run's head no longer copies all preceding runs' provenance dictionaries.
        state = deepcopy(journal.state)
        state["provenance"] = {}
        journal.append([ctx.at("seal").event("run.ended", dict(termination_reason=str(termination),
            grading_status=grading, end_snapshot_id=None))], state=state)
        stopped = batch_stop(batch)
        if code and code.startswith("BATCH_"):
            stopped = code.lower()
        elif code and not (code.startswith("RUN_") or code == "INPUT_SIZE_LIMIT"):
            stopped = stopped or "runtime_error"
        if allocation_index + 1 == len(schedule) and code is None and not batch.unknown_cost:
            # Reaching a guard exactly when all work completes is normal completion.
            stopped = None
    state = deepcopy(journal.state)
    state["baseline_screening"]["runtime_status"] = "finished"
    journal.append([controller.at("seal").event("business.transition", dict(origin="baseline.batch_finished",
        stop_reason=stopped, http_calls=batch.calls))], state=state)
    journal.export_views()
    from agent_memory_eval.analysis.tau2_baseline import summarize_baseline
    return summarize_baseline(journal.root)
