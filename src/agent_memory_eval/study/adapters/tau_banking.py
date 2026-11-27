"""τ³ banking knowledge tasks with the native customer simulator, driven by a declared study plan.

The stage job keeps the execution core of the November banking studies (``forms_batch.execute_prepared``)
and their checks. What those studies fixed in code (task lists, five forms, two readers, one source draw,
profile paths) comes from the plan instead.
"""

from copy import deepcopy
from decimal import Decimal
from functools import lru_cache
import json
import os
from pathlib import Path
import time

from jsonschema import Draft202012Validator

from agent_memory_eval.adapters.tau2 import PIN, native_modules, verify_checkout
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.connection import validate_profile
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import SCHEMA_ROOT
from agent_memory_eval.experiments.batch_recovery import POLICY as RESUME_UNSTARTED, stage_lease
from agent_memory_eval.experiments.forms_batch import BatchGuard, endpoint_terms, execute_prepared, live_models, solve_connection
from agent_memory_eval.experiments.fresh_forms import CostReservations
from agent_memory_eval.experiments.matched_trial import native_control, opening_hash, role_settings, source_receipt
from agent_memory_eval.experiments.paired_repetitions import FRESH_MATCHED
from agent_memory_eval.memory.writing import REQUIRED_STORE_CONTRACT
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.runner.task_start import FORMAT as START_FORMAT
from agent_memory_eval.runner.tau2_baseline import build_baseline_orchestrator, native_config
from agent_memory_eval.study.orchestrator import run_stages
from agent_memory_eval.study.plan import arm_name, conditions, content_text, plan_digest
from agent_memory_eval.study.records import fixed, read, sealed_rows, shard_seal, source_from

APPROVAL = "agent-memory-study-approval/v1"
RESOLVED = "agent-memory-study-resolved/v1"
CASE_KEYS = ("task_sha256", "native_sha256", "customer_opening_sha256", "official_criteria_sha256", "initial_db")
CALLS = {"source": lambda n: 201, "write": lambda n: 4 * n, "solve": lambda n: 201 * n}
SECONDS = {"source": lambda n: 1500, "write": lambda n: 360 * n, "solve": lambda n: 1200 * n}
# What differs between τ² domains. Everything else (stages, matching, sealing, scoring) is shared.
DOMAINS = {
    "tau-banking": dict(domain="banking_knowledge", retrieval="grep_only", native_tool="grep",
                        basis=({"DB"}, {"ACTION"}), nl_assertions=False, judge=False),
    "tau2-airline": dict(domain="airline", retrieval=None, native_tool=None,
                         basis=({"COMMUNICATE", "DB"},), nl_assertions=True, judge=False),
    "tau2-retail": dict(domain="retail", retrieval=None, native_tool=None,
                        basis=({"DB"}, {"DB", "NL_ASSERTION"}), nl_assertions=True, judge=True),
}


def judge_config(cfg):
    return dict(cfg["judge"], timeout_seconds=cfg["http_timeout_seconds"])


def judge_endpoint(cfg):
    """The pinned native NL-assertion judge, resolved the way the November retail studies did."""
    from agent_memory_eval.models.openrouter import model_catalog, model_endpoints, resolve_endpoint, resolve_model
    judge = cfg["judge"]
    model = resolve_model(model_catalog(), judge["model"], judge["parameters"])
    require(model["canonical_slug"] == judge["canonical_slug"], "Judge alias no longer points at the pinned date")
    return resolve_endpoint(model, model_endpoints(model["id"]), judge["route"], judge["parameters"])


def owner_ids(task, db):
    """Retail customers named by a task's reference actions, directly or through an order."""
    args = [a["arguments"] for a in task["evaluation_criteria"]["actions"] or []]
    users = {a["user_id"] for a in args if a.get("user_id") in db["users"]}
    users.update(db["orders"][a["order_id"]]["user_id"] for a in args if a.get("order_id") in db["orders"])
    return users


@lru_cache(maxsize=None)
def modules_for(workspace):
    modules = native_modules(verify_checkout(Path(workspace) / "data/tau2-bench"))
    from loguru import logger
    logger.disable("tau2")
    return modules


def study_jobs(case, stage):
    """Assignments of one shard, ordered as in the November studies so equal arm sets get equal indices."""
    arms = {"source": ["no_memory"], "write": list(case["representations"]), "solve": list(case["arms"])}[stage]
    ordered = sorted(arms, key=lambda a: digest(canonical([FRESH_MATCHED, case["task"], stage, a])))
    recovery = case.get("interrupted_readers") if stage == "solve" else None
    require(recovery is None or (recovery and set(recovery) <= set(ordered)), "Unregistered reader in the retry set")
    return [dict(index=i, repetition=1, task_id=case["task_id"], phase="write" if stage == "write" else "solve", arm=arm)
            for i, arm in enumerate(ordered, 1) if recovery is None or arm in recovery]


def study_design(cfg, case, stage, source=None):
    feedback = cfg["writer_feedback_level"]
    cohort = dict(task_id=case["task_id"], source_group=case["customer"])
    if source is not None:
        cohort.update(run_id=source.source_run_id, source_log_sha256=source.source_log_sha256,
                      public_context_sha256=source.public_context_sha256, public_context_bytes=source.public_context_bytes)
        if feedback == "result_only":
            cohort["result_feedback"] = source.result_receipt()
    jobs = study_jobs(case, stage)
    design = dict(format=FRESH_MATCHED, stage=stage, source=dict(domain=case["domain"], split=case["split"],
        feedback_level=feedback, cohort=[cohort]), writing=cfg["writing"],
        second_pass=dict(proposed_limits=cfg["limits"]), model=dict(connection_profile_ref=cfg["connection"],
        seed_pairs=[dict(repetition=1, writer=cfg["writer_seed"], native_and_solver=cfg["native_and_worker_seed"])]),
        delivery_arms={j["arm"]: dict(writer=case["arm_writer"][j["arm"]], read_mode=case["arm_mode"][j["arm"]])
                       for j in jobs if stage == "solve"},
        allocation=dict(blocks=[dict(task_id=case["task_id"], repetition=1, write=[j["arm"] for j in jobs if stage == "write"])],
                        jobs_sha256=digest(canonical(jobs))),
        reader_admission=cfg["reader_admission"])
    if stage == "solve" and "interrupted_readers" in case:
        forms = {p["writer"] for p in design["delivery_arms"].values()}
        design["writing"] = dict(cfg["writing"], lines={k: v for k, v in cfg["writing"]["lines"].items() if k in forms})
        design["interrupted_readers"] = case["interrupted_readers"]
    return design


def shard_profile(cfg, case, stage, grant, jobs):
    return dict(name=cfg["name"], stage=stage, case=case["task"], registration_sha256=cfg["registration_sha256"],
                guards=dict(max_usd=float(grant), max_calls=CALLS[stage](len(jobs)), max_seconds=SECONDS[stage](len(jobs))),
                http_timeout_seconds=cfg["http_timeout_seconds"], task_start_mode=cfg["task_start_mode"],
                interruption_policy=RESUME_UNSTARTED)


def customer_tool_stop(folder, case, profile, design, identity, models):
    """Certify a first pass the native customer ended with its own tool call before any worker input."""
    from agent_memory_eval.adapters.banking_source_screen import source_observation
    from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions
    close, ctl, rows = sealed_rows(folder)
    expected = study_jobs(case, "source")
    require(close["stop_reason"] == "task_start_mismatch" and len(rows) == len(expected) == 1
            and ctl["profile"] == profile and ctl["design"] == design and ctl["implementation"] == identity
            and endpoint_terms(ctl["model_catalog_entries"]) == endpoint_terms(models),
            "The terminal source is not bound to the same schedule, settings and implementation")
    row = rows[0]
    result = row["result"]
    require(all(row[k] == v for k, v in expected[0].items()) and row["status"] == "error"
            and result["error_code"] == "TASK_START_MISMATCH" and result["grading_status"] == "incomplete"
            and result["official_success"] is None and result["report_ref"] is None, "Expected an ungraded no-memory source")
    source_observation(folder, row, case, set())
    log = RecordedDecisions(folder / row["directory"], row["run_id"], result["log_sha256"])
    require(len(log.requests) == len(log.finishes) == 1 and result["http_calls"] == result["native_steps"] == 1,
            "An unsupported customer opening must precede all worker traffic")
    request, = log.requests
    finish = log.finishes[request["request_id"]]
    require(request["actor_role"] == "user_simulator" and finish["status"] == "response"
            and [e["payload"]["code"] for e in log.of_type("runtime.error")] == ["TASK_START_MISMATCH"],
            "The original customer opening must have one recorded provider response")
    choice = log.reply(request)
    require(choice["finish_reason"] == "tool_calls" and choice["message"].get("tool_calls"),
            "Expected a native customer tool action on the ordinary greeting")
    return dict(reason="customer_tool_before_first_worker_input", allocation=case["task"], run=row["run_id"],
                shard_completion_sha256=shard_seal(folder), journal_sha256=result["log_sha256"],
                customer_request_sha256=request["request_ref"]["sha256"],
                customer_response_sha256=finish["response_ref"]["sha256"])


def bank_shard(args):
    common, case, stage_name, grant = args
    with stage_lease(Path(common["output"]) / ("task-" + case["task"])):
        return _bank_shard(common, case, stage_name, grant)


def _bank_shard(common, case, stage_name, grant):
    workspace, output, cfg = Path(common["workspace"]), Path(common["output"]), common["cfg"]
    conn, identity, models = common["conn"], common["identity"], common["models"]
    stage = cfg["stage_kinds"][stage_name]
    task_dir = output / ("task-" + case["task"])
    dest = task_dir / stage_name
    imported = case.get("imported_first_pass")
    source_dir = safe_path(workspace / "runs", imported["directory"]) if imported else task_dir / "source"
    if stage == "source":
        require(not imported, "A reused first pass cannot be resubmitted")
    tid = case["task_id"]
    plan = dict(sources={}, tasks={tid: common["tasks"][case["task"]]}, connection=conn, modules=modules_for(str(workspace)),
                instruction=asset_text(workspace, cfg["delivery_instruction"]))
    spec = dict(registration_sha256=cfg["registration_sha256"], native_sha256=case["native_sha256"],
                customer_opening_sha256=case["customer_opening_sha256"], instruction_sha256=cfg["delivery_instruction"]["sha256"],
                episode_cap_usd=cfg["episode_cap_usd"], http_timeout_seconds=cfg["http_timeout_seconds"],
                task_start_mode=cfg["task_start_mode"])
    if "query_tool_descriptions" in cfg:
        spec["query_tool_descriptions"] = cfg["query_tool_descriptions"]
    source = None
    if stage != "source":
        _, parent, first_rows = sealed_rows(source_dir)
        source_registration = imported["registration_sha256"] if imported else cfg["registration_sha256"]
        require(parent["profile"]["registration_sha256"] == source_registration and parent["design"]["stage"] == "source",
                "The writer or reader was linked to another study's first pass")
        source = source_from(source_dir, tid)
        if source is None:
            plan["missing_sources"] = [tid]
        else:
            first_row, = first_rows
            if imported:
                require((source.source_run_id, source.source_log_sha256, source.public_context_sha256)
                        == (imported["run_id"], imported["log_sha256"], imported["public_context_sha256"]),
                        "Reused first pass or its public experience changed")
                spec["source_registration_sha256"] = source_registration
            spec.update(source=source_receipt(safe_path(source_dir, first_row["directory"]), source,
                                              dict(spec, registration_sha256=source_registration)),
                        public_context_sha256=source.public_context_sha256)
            plan["sources"][tid] = source
    design = study_design(cfg, case, stage, source)
    jobs = study_jobs(case, stage)
    profile = shard_profile(cfg, case, stage, grant, jobs)
    if not (dest / "completion.json").exists():
        bindings = None
        if stage == "solve":
            archived = case.get("imported_writer_stage")
            require(bool(archived) == ("interrupted_readers" in case), "A retried reader uses its original writer stage")
            if archived:
                write_dir = safe_path(workspace / "runs", archived["directory"])
                registration, seal = archived["registration_sha256"], archived["seal"]
            else:
                write_dir, registration, seal = task_dir / "write", cfg["registration_sha256"], None
            _, parent, written = sealed_rows(write_dir, seal)
            require(parent["profile"]["registration_sha256"] == registration
                    and parent["design"] == study_design(cfg, case, "write", source)
                    and [r["arm"] for r in written] == [j["arm"] for j in study_jobs(case, "write")],
                    "Writer banks do not belong to this registered task and first pass")
            by_arm = {r["arm"]: r for r in written}
            bindings = (write_dir, {(1, tid, arm): by_arm[d["writer"]] for arm, d in design["delivery_arms"].items()})
            if archived:
                for job in jobs:
                    old = case["interrupted_readers"][job["arm"]]
                    writer = bindings[1][1, tid, job["arm"]]
                    require((old["index"], old["writer"], old["snapshot"]) == (job["index"], writer["run_id"],
                            writer["result"]["snapshot_id"]), "A retried slot has another assignment or memory")
        if stage != "write" and common.get("judge") is not None:
            plan["grader"] = judge_config(cfg), common["judge"]
        plan.update(jobs=jobs, matched_trial=spec, summary=dict(native_sha256=case["native_sha256"],
                    jobs_sha256=design["allocation"]["jobs_sha256"]))
        guard = BatchGuard(**profile["guards"])
        reserve = CostReservations(dest, dict(codes=cfg["unpriced_http_codes"], maximum_per_stage=len(jobs),
                                   reserve_uncertain_transport=True), guard, dict(models, grader=common.get("judge")))
        execute_prepared(profile, workspace, dest, identity, design, plan, models, guard, imported_writers=bindings,
                         close_cost=reserve)
    _, ctl, rows = sealed_rows(dest)
    require(ctl["profile"] == profile and ctl["design"] == design and ctl["implementation"] == identity
            and endpoint_terms(ctl["model_catalog_entries"]) == endpoint_terms(models)
            and len(rows) == len(jobs) and all(all(r[k] == v for k, v in j.items()) for r, j in zip(rows, jobs)),
            "The sealed shard does not match its registration")
    codes = {r.get("result", {}).get("error_code") for r in rows}
    if stage == "source" and codes == {"TASK_START_MISMATCH"}:
        fixed(task_dir / "customer-opening-stop.json", customer_tool_stop(dest, case, profile, design, identity, models))
    elif codes & {"FIRST_PASS_MISMATCH", "TASK_START_MISMATCH"}:
        raise ContractError("A consultation's first input differs from its first pass")


def approval_for(plan, record, *, max_usd, user_authorization):
    """The spending record a person approves: this plan, this resolved allocation, one cumulative ceiling."""
    return dict(format=APPROVAL, plan_id=plan["id"], plan_sha256=plan_digest(plan), resolved_sha256=digest(canonical(record)),
                assigned=record["assigned"], max_usd=str(max_usd), user_authorization=user_authorization)


def study_models(cfg, conn):
    models = live_models(conn)
    require({r: m.get("canonical_slug") for r, m in models.items()} == cfg["expected_model_revisions"],
            "Worker or customer model differs from the registered revision")
    return models, judge_endpoint(cfg) if "grader" in cfg["expected_endpoint_terms"] else None


class Tau2Study:
    """A τ² domain behind the declared-study runner; subclasses only name the domain."""
    name = None

    @property
    def spec(self):
        return DOMAINS[self.name]

    def _pin(self, workspace, plan_id, name, content):
        """A pinned file the stage job can read from the workspace; inline text is written once under the run."""
        if "path" in content:
            asset_text(workspace, content)
            return dict(path=content["path"], sha256=content["sha256"])
        folder = workspace / "runs" / plan_id / "assets"
        folder.mkdir(parents=True, exist_ok=True)
        data = (content["text"] + "\n").encode()
        path = folder / name
        if path.exists():
            require(path.read_bytes() == data, f"Pinned study asset {name} changed")
        else:
            path.write_bytes(data)
        return dict(path=str(path.relative_to(workspace)), sha256=digest(data))

    def _configure(self, plan, workspace, root):
        setup = read_json(asset_text(root, plan["benchmark"]["setup"]).encode())
        base = read_json(asset_text(root, setup["base"]).encode())
        spec = self.spec
        require(setup["revision"] == base["benchmark_revision"] == PIN and setup["task_start_mode"] == START_FORMAT
                and setup["domain"] == spec["domain"] and setup.get("retrieval_config") == spec["retrieval"],
                f"The {self.name} setup differs from its pinned implementation")
        require(plan["purpose"] == "development" or "references" not in plan, "Confirmation plans compare fresh runs only")
        readers = plan.get("reading", {})
        writing = plan.get("writing")
        for name, reader in readers.items():
            tool_only = reader["read_mode"] == "on_demand" and "tool_descriptions" in reader
            require(tool_only != ("notice" in reader),
                    f"Reader {name}: a tool-only on_demand reader shows no notice; every other reader needs one")
        shown = [r["notice"] for r in readers.values() if "notice" in r]
        require(len({canonical(n) for n in shown}) <= 1, "Readers in a τ² plan share one notice in version 1")
        notice = shown[0] if shown else dict(text="Notes from earlier work may be available.")
        limits = plan.get("limits", {})
        pairs = conditions(plan)
        reps = list(dict.fromkeys(rep for rep, _ in pairs))
        cfg = deepcopy(base)
        cfg.update(name=plan["id"], task_start_mode=START_FORMAT,
                   writer_feedback_level=writing["feedback"] if writing else "result_only",
                   reader_admission="sealed_bank", max_concurrent=limits.get("max_concurrent", setup["max_concurrent"]),
                   delivery_instruction=self._pin(workspace, plan["id"], "reader-notice.txt", notice))
        cfg["limits"]["max_input_bytes"] = limits.get("max_input_bytes", setup["max_input_bytes"])
        cfg["writing"].update(completion_contract=REQUIRED_STORE_CONTRACT,
                              lines={rep: content_text(root, writing["representations"][rep]) for rep in reps})
        if writing:
            cfg["writing"]["common_instruction_ref"] = self._pin(workspace, plan["id"], "writer-instruction.txt",
                                                                 writing["instruction"])
        cfg["writing"]["proposed_limits"]["bank_items"] = 1
        if "writer_max_input_bytes" in limits:
            cfg["writing"]["proposed_limits"]["input_bytes"] = limits["writer_max_input_bytes"]
        if reps:
            schema = read_json((SCHEMA_ROOT / "required-form-writing-v1.schema.json").read_bytes())
            error = next(Draft202012Validator(schema).iter_errors(cfg["writing"]), None)
            require(error is None, f"Invalid writing policy: {error.message if error else ''}")
        for reader in readers.values():
            if "tool_descriptions" in reader:
                cfg["query_tool_descriptions"] = reader["tool_descriptions"]
        asset_text(workspace, cfg["connection"])
        conn = read_json(asset_text(root, cfg["connection"]).encode())
        validate_profile(conn)
        conn["benchmark"].update(domain=setup["domain"], split=setup.get("split"))
        if spec["retrieval"]:
            conn["benchmark"]["retrieval_config"] = spec["retrieval"]
        endpoints = read_json(asset_text(root, cfg["endpoints"]).encode())["models"]
        roles = ("worker", "user_simulator")
        cfg["expected_endpoint_terms"] = {r: cfg["endpoint_terms"][r] for r in (*roles, *(("grader",) if spec["judge"] else ()))}
        cfg["expected_model_revisions"] = {r: endpoints[r]["canonical_slug"] for r in roles}
        solves = ["solve", *(f"solve-repeat-{n}" for n in range(2, plan["repeats"] + 1))]
        cfg["stage_kinds"] = {"source": "source", "write": "write", **{s: "solve" for s in solves}}
        for stage in solves:
            for k in range(1, len(plan["retry"]["wait_seconds"]) + 1):
                cfg["stage_kinds"][f"{stage}-retry-{k}"] = "solve"
        cfg["solve_stages"] = solves
        cfg["registration_sha256"] = None
        return setup, cfg, conn, pairs, reps

    def _native_case(self, cfg, conn, modules, registered):
        """Bind the native task, its initial state, tools, customer opening and grading configuration."""
        from tau2.multilingual.runtime import load_run_language
        from tau2.runner.helpers import select_run_tasks
        tid = registered.id
        case = dict(task=tid, task_id=tid, domain=conn["benchmark"]["domain"], split=conn["benchmark"]["split"],
                    customer="source-" + tid,
                    task_start_mode=START_FORMAT, representations=[], arms=[], arm_writer={}, arm_mode={})
        conf = solve_connection(conn, study_design(cfg, case, "source"), study_jobs(case, "source")[0], cfg["episode_cap_usd"])
        native = native_config(conf)
        task, = select_run_tasks(native, load_run_language(native))
        require(task.id == registered.id and task.evaluation_criteria == registered.evaluation_criteria,
                "Runtime task differs from the registered selection or grading")
        task_data = task.model_dump(mode="json")
        orch, native = build_baseline_orchestrator(conf, task_data)
        control = native_control(orch, native, conf, cfg["limits"], cfg["http_timeout_seconds"],
                                 judge_config(cfg) if self.spec["judge"] else None)
        opening = opening_hash(orch, role_settings(control)["user_simulator"], modules[2])
        names = {t.name for t in orch.agent.tools}
        require(not any(n.startswith("memory_") for n in names) and self.spec["native_tool"] in (None, *names),
                "Expected only the native business tools before memory assignment")
        native_case = dict(native_sha256=digest(canonical(control)), task_sha256=control["task_sha256"],
                           customer_opening_sha256=opening, initial_db=control["initial_db"],
                           official_criteria_sha256=digest(canonical(task.evaluation_criteria.model_dump(mode="json"))))
        if "initialized_databases" in control:  # Tasks that fill their databases before the first turn.
            observed = dict(agent=orch.environment.get_db_hash(), user=orch.environment.get_user_db_hash())
            require(observed == control["initialized_databases"], "Native opening differs from the initialized databases")
            native_case["initialized_databases"] = observed
        else:
            require(control["initial_db"] == orch.environment.get_db_hash(), "The native initial state changed while opening")
        return native_case, task_data

    def _reused(self, workspace, plan, name, case):
        for ref in plan["first_pass"].get("reuse", []):
            recording = workspace / "runs" / ref["recording"]
            shard = recording / f"task-{name}" / "source"
            if not (shard / "completion.json").is_file():
                continue
            prior = {c["task"]: c for c in read(recording / "registration.json")["preflight"]["cases"]}.get(name)
            require(prior is not None and all(prior.get(k) == case[k] for k in CASE_KEYS),
                    f"Reused first pass {ref['recording']}/{name} ran in another native setting")
            if (recording / "completion.json").is_file():
                require(read(recording / "completion.json")["stages"][f"task-{name}/source"]["seal"] == shard_seal(shard),
                        "Reused first-pass shard changed after its study closed")
            _, ctl, (row,) = sealed_rows(shard)
            result = row.get("result") or {}
            if result.get("grading_status") != "graded":
                reason = result.get("error_code") or row.get("reason") or row["status"]
                technical = {"BATCH_SPEND_LIMIT", *(f"HTTP_{code}" for code in plan["retry"]["http_codes"])}
                if reason in technical:  # Lost to our own guard or a declared provider error, not to the task.
                    return dict(replaces_first_pass=dict(recording=ref["recording"], reason=reason, run=row["run_id"]))
                return dict(first_pass_missing=dict(recording=ref["recording"], reason=reason))
            source = source_from(shard, case["task_id"])
            return dict(imported_first_pass=dict(directory=f"{ref['recording']}/task-{name}/source",
                        registration_sha256=ctl["profile"]["registration_sha256"], run_id=row["run_id"],
                        log_sha256=result["log_sha256"], public_context_sha256=source.public_context_sha256))
        return {}

    def _customers(self, domain_dir, tids, population):
        """Customer identity per task, for reporting and customer-level grouping. Unresolved stays explicit."""
        if self.spec["domain"] == "banking_knowledge":
            from agent_memory_eval.adapters.tau_banking_results import task_customers
            return task_customers(domain_dir, set(tids))
        from agent_memory_eval.experiments.fresh_forms import scenario_customer
        db = read_json((domain_dir / "db.json").read_bytes())
        found = {}
        for tid in tids:
            task = population[tid].model_dump(mode="json") if tid in population else None
            if task is None:
                continue
            if self.spec["domain"] == "airline":
                try:
                    found[tid] = {scenario_customer(task, db)}
                except ContractError:
                    found[tid] = set()
            else:
                found[tid] = owner_ids(task, db)
        return found

    def resolve(self, plan, workspace, root):
        """Cases, stage schedule and allocation counts for this plan, before any model call."""
        workspace, root = Path(workspace), Path(root)
        setup, cfg, conn, pairs, reps = self._configure(plan, workspace, root)
        modules = modules_for(str(workspace))  # Binds tau2 to the pinned checkout before its first import.
        import importlib
        from agent_memory_eval.adapters.tau2_task_start import greeting_start
        spec = self.spec
        env = importlib.import_module(f"tau2.domains.{spec['domain']}.environment")
        population = {t.id: t for t in env.get_tasks()}
        split = setup.get("split")
        allowed = set(env.get_tasks_split()[split]) if split else set(population)
        domain_dir = workspace / "data/tau2-bench/data/tau2/domains" / setup["domain"]
        identities = self._customers(domain_dir, plan["benchmark"]["tasks"], population)
        cases, tasks = [], {}
        readers = plan.get("reading", {})
        arms = [arm_name(rep, reader) for rep, reader in pairs]
        for tid in plan["benchmark"]["tasks"]:
            require(tid in population and tid in allowed, f"Task {tid} is not in the {self.name} setup's split")
            task = population[tid]
            basis = {r.value for r in task.evaluation_criteria.reward_basis}
            require(basis in spec["basis"] and (spec["nl_assertions"] or not task.evaluation_criteria.nl_assertions)
                    and greeting_start(task.initial_state), f"Task {tid} has unregistered grading or an initial action")
            native, data = self._native_case(cfg, conn, modules, task)
            people = identities.get(tid, set())
            for draw in range(1, plan["first_pass"]["draws"] + 1):
                name = f"{tid}__source-{draw}"
                case = dict(task=name, task_id=tid, source_draw=draw, domain=conn["benchmark"]["domain"], split=split,
                            customer=next(iter(people)) if len(people) == 1 else "unresolved-" + tid,
                            task_start_mode=START_FORMAT, representations=reps, arms=arms,
                            arm_writer={arm_name(rep, reader): rep for rep, reader in pairs},
                            arm_mode={arm_name(rep, reader): readers[reader]["read_mode"] for rep, reader in pairs},
                            **native)
                case.update(self._reused(workspace, plan, name, case))
                memory_stages = ["write", *cfg["solve_stages"]] if reps else []
                if "first_pass_missing" in case:
                    case["scheduled_stages"] = []
                elif "imported_first_pass" in case:
                    case["scheduled_stages"] = memory_stages
                else:
                    case["scheduled_stages"] = ["source", *memory_stages]
                cases.append(case)
                tasks[name] = data
        record_check = self._budget_check(plan, cfg, cases)
        live = [c for c in cases if "write" in c["scheduled_stages"]]
        assigned = dict(source=sum("source" in c["scheduled_stages"] for c in cases),
                        reused_first_pass=sum("imported_first_pass" in c for c in cases),
                        write=len(live) * len(reps), solve=len(live) * len(arms) * plan["repeats"])
        record = dict(format=RESOLVED, adapter=self.name, plan_id=plan["id"], plan_sha256=plan_digest(plan),
                      native_revision=PIN, domain=spec["domain"], split=split,
                      domain_db_sha256=digest((domain_dir / "db.json").read_bytes()),
                      stages=["source", "write", *cfg["solve_stages"]], assigned=assigned, budget_check=record_check,
                      cases=cases)
        if spec["domain"] == "banking_knowledge":
            record["corpus_sha256"] = digest(canonical(env.get_knowledge_base().model_dump(mode="json")))
        cfg["registration_sha256"] = digest(canonical(record))
        return dict(cfg=cfg, conn=conn, cases=cases, tasks=tasks, record=record)

    def _budget_check(self, plan, cfg, cases):
        """Each shard's grant must cover the largest single-request reservation twice, or requests stop mid-episode."""
        terms = cfg["expected_endpoint_terms"]
        def reserve(role, input_bytes, output_tokens):
            rates = terms[role]["reservation_pricing"]
            return Decimal(str(input_bytes)) * Decimal(str(rates["prompt"])) + output_tokens * Decimal(str(rates["completion"]))
        largest = max(reserve("worker", max(cfg["limits"]["max_input_bytes"], cfg["writing"]["proposed_limits"]["input_bytes"]), 4096),
                      reserve("user_simulator", cfg["limits"]["max_input_bytes"], 4096))
        if "grader" in terms:
            judge = cfg["judge"]
            largest = max(largest, reserve("grader", judge["max_input_bytes"], judge["parameters"]["max_completion_tokens"]))
        shards = max(1, min(cfg["max_concurrent"], len([c for c in cases if c["scheduled_stages"]]) or 1))
        grant = Decimal(plan["budget"]["max_usd"]) / shards
        require(grant >= 2 * largest, f"Budget leaves each shard less than twice the largest request reservation "
                                      f"(${largest:.4f} per request, ${grant:.4f} per shard). Raise budget.max_usd or lower "
                                      f"limits.max_concurrent.")
        return dict(largest_request_reservation_usd=f"{largest:.6f}", first_wave_shard_grant_usd=f"{grant:.6f}")

    def _admission(self, cfg):
        def admit(root, stage, case):
            kind = cfg["stage_kinds"][stage]
            task_dir = root / ("task-" + case["task"])
            if kind == "source":
                return None
            if "imported_first_pass" not in case:
                if not (task_dir / "source" / "completion.json").is_file():
                    return "source_not_started"
                _, _, (row,) = sealed_rows(task_dir / "source")
                if (row.get("result") or {}).get("grading_status") != "graded":
                    return "source_ungraded"
            if kind == "solve" and "imported_writer_stage" not in case and not (task_dir / "write" / "completion.json").is_file():
                return "writer_not_started"
            return None
        return admit

    def _retry_cases(self, root, cfg, cases, stage, attempt, codes):
        """Readers whose latest attempt of ``stage`` ended on a registered transient provider error."""
        retry = []
        latest = stage if attempt == 1 else f"{stage}-retry-{attempt - 1}"
        for case in cases:
            task_dir = root / ("task-" + case["task"])
            if not (task_dir / latest / "completion.json").is_file():
                continue
            _, _, rows = sealed_rows(task_dir / latest)
            failed = [r for r in rows if r["status"] == "error" and (r.get("result") or {}).get("official_success") is None
                      and (r.get("result") or {}).get("error_code") in codes]
            if not failed:
                continue
            _, _, written = sealed_rows(task_dir / "write")
            writers = {w["arm"]: w for w in written}
            readers = {}
            for r in failed:
                writer = writers[case["arm_writer"][r["arm"]]]
                readers[r["arm"]] = dict(index=r["index"], writer=writer["run_id"], snapshot=writer["result"]["snapshot_id"])
            retry.append(dict(case, scheduled_stages=[f"{stage}-retry-{attempt}"], interrupted_readers=readers,
                              imported_writer_stage=dict(directory=f"{root.name}/task-{case['task']}/write",
                                                         seal=shard_seal(task_dir / "write"),
                                                         registration_sha256=cfg["registration_sha256"])))
        return retry

    def _groups(self, root, plan, cfg, cases, sleep):
        yield ("source", "source", [c for c in cases if "source" in c["scheduled_stages"]])
        live = [c for c in cases if "write" in c["scheduled_stages"]]
        if not live:
            return
        yield ("write", "write", live)
        codes = {f"HTTP_{code}" for code in plan["retry"]["http_codes"]}
        for stage in cfg["solve_stages"]:
            yield (stage, stage, live)
            for attempt, wait in enumerate(plan["retry"]["wait_seconds"], 1):
                retry = self._retry_cases(root, cfg, live, stage, attempt, codes)
                if not retry:
                    break
                sleep(wait)
                yield (f"{stage}-retry-{attempt}", f"{stage}-retry-{attempt}", retry)

    def run(self, plan, workspace, root, *, approval, identity=None, model_resolver=None, pool_factory=None,
            sleep=time.sleep, progress=None):
        from agent_memory_eval.reproduction.freeze import implementation_identity
        workspace, root = Path(workspace), Path(root)
        resolved = self.resolve(plan, workspace, root)
        record, cfg = resolved["record"], resolved["cfg"]
        require(approval.get("format") == APPROVAL and approval.get("plan_sha256") == plan_digest(plan)
                and approval.get("resolved_sha256") == digest(canonical(record)) and approval.get("assigned") == record["assigned"]
                and Decimal(approval["max_usd"]) <= Decimal(plan["budget"]["max_usd"]) and approval.get("user_authorization"),
                "Spending is not authorized for this resolved plan")
        require(bool(os.environ.get("OPENROUTER_API_KEY")), "Missing OpenRouter credentials")
        identity = identity or implementation_identity(root)
        models, judge = (model_resolver or study_models)(cfg, resolved["conn"])
        terms = endpoint_terms(dict(models, grader=judge) if judge is not None else models)
        require(terms == cfg["expected_endpoint_terms"], "Worker, customer or judge endpoint terms changed")
        run_root = workspace / "runs" / plan["id"]
        run_root.mkdir(parents=True, exist_ok=True)
        fixed(run_root / "resolved-plan.json", record)
        registration = dict(format="agent-memory-study-run/v1", adapter=self.name, implementation=identity,
                            preflight=record, endpoint_terms=terms, approval=approval)
        common = dict(workspace=str(workspace), output=str(run_root), cfg=cfg, conn=resolved["conn"], identity=identity,
                      models=models, judge=judge, tasks=resolved["tasks"])
        return run_stages(run_root, registration, self._groups(run_root, plan, cfg, resolved["cases"], sleep),
                          (bank_shard, common), total_usd_limit=approval["max_usd"], max_concurrent=cfg["max_concurrent"],
                          admission=self._admission(cfg), pool_factory=pool_factory, progress=progress)

    def _case_rows(self, workspace, run_root, case, origin, label, attempt_base):
        """Outcome rows of one case: its first pass, then every solve stage and retry in order."""
        from agent_memory_eval.adapters.banking_memory_cohort import observation
        from agent_memory_eval.experiments.forms_write import read_form_bank
        from agent_memory_eval.analysis.recorded_decisions import READS, RecordedDecisions
        rows = []
        task_dir = run_root / ("task-" + case["task"])
        def first_decision(folder, r):
            """The worker's first business tool call(s), skipping replies and memory reads; none in a graded episode is kept."""
            result = r.get("result") or {}
            if not result.get("log_sha256"):
                return None
            log = RecordedDecisions(safe_path(folder, r["directory"]), r["run_id"], result["log_sha256"])
            for req in log.requests:
                finish = log.finishes.get(req["request_id"])
                if req["actor_role"] != "worker" or not finish or finish["status"] != "response":
                    continue
                calls = [c for c in log.reply(req)["message"].get("tool_calls") or [] if c["function"]["name"] not in READS]
                if calls:
                    return canonical([[c["function"]["name"], c["function"]["arguments"]] for c in calls]).decode()
            return "no_business_call" if result.get("grading_status") == "graded" else None
        def row(condition, obs, attempt, repeat=1, origin_name=origin, decision=None, memory=None):
            graded = obs["success"] is not None
            effort = obs.get("effort") or {}
            return dict(task=case["task_id"], source=case["task"], repeat=repeat, condition=condition, origin=origin_name,
                        attempt=attempt, status="graded" if graded else "missing", success=obs["success"],
                        missing_reason=None if graded else (obs.get("reason") or obs["status"]),
                        tokens=effort.get("tokens"), business_calls=effort.get("business_calls"),
                        reads_list=obs.get("reads", {}).get("memory_list", 0), reads_get=obs.get("reads", {}).get("memory_get", 0),
                        preloaded="body" in obs.get("first_fields", []), cost_usd=effort.get("usd"), first_decision=decision,
                        memory_title=memory["title"] if memory else None, memory_body=memory["body"] if memory else None)
        missing = case.get("first_pass_missing")
        if missing:
            rows.append(dict(row("no_memory", dict(success=None, status="missing", reason=missing["reason"]), 0),
                             origin=missing["recording"]))
            return rows
        imported = case.get("imported_first_pass")
        if imported:
            source_dir = safe_path(workspace / "runs", imported["directory"])
            source_origin = imported["directory"].split("/")[0]
        else:
            source_dir, source_origin = task_dir / "source", origin
        if (source_dir / "completion.json").is_file():
            _, _, (srow,) = sealed_rows(source_dir)
            rows.append(row("no_memory", observation(source_dir, srow), 0, origin_name=source_origin,
                            decision=first_decision(source_dir, srow)))
        writer_stage = case.get("imported_writer_stage")
        write_dir = safe_path(workspace / "runs", writer_stage["directory"]) if writer_stage else task_dir / "write"
        banks = {}
        if (write_dir / "completion.json").is_file():
            for w in sealed_rows(write_dir)[2]:
                if w["status"] == "completed":
                    banks[w["arm"]] = read_form_bank(safe_path(write_dir, w["directory"]),
                                                     expected_log_sha256=w["result"]["log_sha256"])
        solves = sorted(p for p in task_dir.iterdir() if p.is_dir() and p.name.startswith("solve")) if task_dir.is_dir() else []
        for folder in solves:
            name = folder.name
            base, _, retry = name.partition("-retry-")
            repeat = int(base.removeprefix("solve-repeat-")) if base.startswith("solve-repeat-") else 1
            attempt = attempt_base + (int(retry) if retry else 0)
            if not (folder / "completion.json").is_file():
                continue
            for r in sealed_rows(folder)[2]:
                rep = r["arm"].split("__")[0]
                bank = banks.get(rep)
                rows.append(row(label(r["arm"]), observation(folder, r, writer=bank), attempt, repeat,
                                decision=first_decision(folder, r), memory=bank["items"][0] if bank and bank["items"] else None))
        return rows

    def outcomes(self, plan, workspace, root):
        """Rows for this plan's run, then for each reference recording in the order declared."""
        workspace = Path(workspace)
        run_root = workspace / "runs" / plan["id"]
        if (run_root / "resolved-plan.json").is_file():
            record = read(run_root / "resolved-plan.json")
            require(record["plan_sha256"] == plan_digest(plan), "The run was resolved from another plan")
        else:  # Not run yet: first passes and references only.
            record = self.resolve(plan, workspace, root)["record"]
        rows = []
        for case in record["cases"]:
            rows += self._case_rows(workspace, run_root, case, plan["id"], lambda arm: arm, 0)
            if "first_pass_missing" in case:
                rows += [dict(task=case["task_id"], source=case["task"], repeat=n, condition=arm, origin=plan["id"],
                              attempt=0, status="missing", success=None, missing_reason="first_pass_ungraded",
                              tokens=None, business_calls=None, reads_list=0, reads_get=0, preloaded=False, cost_usd=None)
                         for arm in case["arms"] for n in range(1, plan["repeats"] + 1)]
        sources = {c["task"] for c in record["cases"]}
        for ref in plan.get("references", []):
            ref_root = workspace / "runs" / ref["recording"]
            label_root = ref.get("attempt_of", ref["recording"])
            ref_cases = [c for c in read(ref_root / "registration.json")["preflight"]["cases"] if c["task"] in sources]
            require(ref_cases, f"Reference {ref['recording']} shares no first pass with the plan")
            for case in ref_cases:
                rows += [r for r in self._case_rows(workspace, ref_root, case, ref["recording"],
                                                    lambda arm, p=label_root: f"{p}/{arm}", 1 if "attempt_of" in ref else 0)
                         if r["condition"] != "no_memory"]
        return rows


class TauBanking(Tau2Study):
    name = "tau-banking"


class Tau2Airline(Tau2Study):
    name = "tau2-airline"


class Tau2Retail(Tau2Study):
    name = "tau2-retail"
