"""τ³ banking knowledge tasks with the native customer simulator, driven by a declared study plan.

The stage job keeps the execution core of the November banking studies (``forms_batch.execute_prepared``)
and their checks. What those studies fixed in code (task lists, five forms, two readers, one source draw,
profile paths) comes from the plan instead.
"""

from copy import deepcopy
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
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
from agent_memory_eval.experiments.matched_trial import native_control, opening_hash, role_settings, source_receipt, target_opening_receipt
from agent_memory_eval.experiments.paired_repetitions import FRESH_MATCHED
from agent_memory_eval.memory.writing import REQUIRED_STORE_CONTRACT
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.runner.task_start import FORMAT as START_FORMAT
from agent_memory_eval.runner.tau2_baseline import build_baseline_orchestrator, native_config
from agent_memory_eval.study.cohort import check_through_stage, is_cohort
from agent_memory_eval.study.orchestrator import run_stages
from agent_memory_eval.study.plan import TRANSFER_FORMAT, arm_name, conditions, content_text, model_profile, plan_digest, require_declared_models
from agent_memory_eval.study.records import fixed, read, sealed_rows, shard_seal, source_from

APPROVAL = "agent-memory-study-approval/v1"
RESOLVED = "agent-memory-study-resolved/v1"
CASE_KEYS = ("task_sha256", "native_sha256", "customer_opening_sha256", "official_criteria_sha256", "initial_db")
CALLS = {"source": lambda n: 201, "write": lambda n: 4 * n, "solve": lambda n: 201 * n}
SECONDS = {"source": lambda n: 1500, "write": lambda n: 360 * n, "solve": lambda n: 1200 * n}
TARGET_NO_MEMORY = "target_no_memory"
# What differs between τ² domains. Everything else (stages, matching, sealing, scoring) is shared.
DOMAINS = {
    "tau-banking": dict(domain="banking_knowledge", retrieval="grep_only", native_tool="grep",
                        basis=({"DB"}, {"ACTION"}), nl_assertions=False, judge=False),
    "tau2-airline": dict(domain="airline", retrieval=None, native_tool=None,
                         basis=({"COMMUNICATE", "DB"},), nl_assertions=True, judge=False),
    "tau2-retail": dict(domain="retail", retrieval=None, native_tool=None,
                        basis=({"DB"}, {"DB", "NL_ASSERTION"}), nl_assertions=True, judge=True),
}


STEP_CAP = "NATIVE_MAX_STEPS"


def outcome_fields(obs):
    """(status, success, missing reason, step_cap). τ² scores an episode stopped at its native step cap 0."""
    if obs["success"] is not None:
        return "graded", obs["success"], None, False
    reason = obs.get("reason") or obs["status"]
    if reason == STEP_CAP:
        return "graded", False, None, True
    return "missing", None, reason, False


def unstarted_stages(run_root):
    close = run_root / "completion.json"
    if not close.is_file():
        return {}
    return {name: entry["reason"] for name, entry in read(close)["stages"].items()
            if entry.get("status") == "not_started"}


def first_pass_identity(workspace, run_root, case):
    imported = case.get("imported_first_pass")
    source = (safe_path(workspace / "runs", imported["directory"]) if imported
              else run_root / ("task-" + case["task"]) / "source")
    if not (source / "completion.json").is_file():
        return None
    _, _, (row,) = sealed_rows(source)
    log_hash = (row.get("result") or {}).get("log_sha256")
    if not log_hash:
        return None
    identity = row["run_id"], log_hash
    require(not imported or identity == (imported["run_id"], imported["log_sha256"]),
            "Imported first-pass identity differs from its sealed source")
    return identity


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
    arms = {"source": ["no_memory"], TARGET_NO_MEMORY: ["no_memory"],
            "write": case["representations"], "solve": case["arms"]}[stage]
    ordered = sorted(arms, key=lambda a: digest(canonical([FRESH_MATCHED, case["task"], stage, a])))
    recovery = case.get("interrupted_readers") if stage == "solve" else None
    require(recovery is None or (recovery and set(recovery) <= set(ordered)), "Unregistered reader in the retry set")
    binding = dict(source_task_id=case["donor"]["task_id"]) if "donor" in case else {}
    if stage == TARGET_NO_MEMORY:
        binding["target_no_memory_index"] = case["target_no_memory_index"]
    return [dict(index=i, repetition=1, task_id=case["task_id"], phase="write" if stage == "write" else "solve", arm=arm, **binding)
            for i, arm in enumerate(ordered, 1) if recovery is None or arm in recovery]


def study_design(cfg, case, stage, source=None):
    writing, feedback = cfg["writing"], cfg["writer_feedback_level"]
    if stage == TARGET_NO_MEMORY:
        writing, feedback = dict(writing, lines={}), "none"
    donor = case.get("donor", case)
    cohort = dict(task_id=donor["task_id"], source_group=donor["customer"])
    if source is not None:
        cohort.update(run_id=source.source_run_id, source_log_sha256=source.source_log_sha256,
                      public_context_sha256=source.public_context_sha256, public_context_bytes=source.public_context_bytes)
        if feedback == "result_only":
            cohort["result_feedback"] = source.result_receipt()
    jobs = study_jobs(case, stage)
    design = dict(format=FRESH_MATCHED, stage=stage, source=dict(domain=case["domain"], split=case["split"],
        feedback_level=feedback, cohort=[cohort]), writing=writing,
        second_pass=dict(proposed_limits=cfg["limits"]), model=dict(connection_profile_ref=cfg["connection"],
        seed_pairs=[dict(repetition=1, writer=cfg["writer_seed"], native_and_solver=cfg["native_and_worker_seed"])]),
        delivery_arms={j["arm"]: dict(writer=case["arm_writer"][j["arm"]], read_mode=case["arm_mode"][j["arm"]])
                       for j in jobs if stage == "solve"},
        allocation=dict(blocks=[dict(task_id=case["task_id"], repetition=1, write=[j["arm"] for j in jobs if stage == "write"])],
                        jobs_sha256=digest(canonical(jobs))),
        reader_admission=cfg["reader_admission"])
    for arm, descriptions in cfg.get("query_tools_by_arm", {}).items():
        if arm in design["delivery_arms"]:
            design["delivery_arms"][arm]["tool_descriptions"] = descriptions
    if "donor" in case:
        for arm, delivery in design["delivery_arms"].items():
            scope = case["contents"][arm]
            if "pool_sources" in case:
                delivery.update(pool=scope, source_tasks=[case["task_id"]] if scope == "own" else case["pool_sources"])
            else:
                delivery["bank_contents"] = scope
    if stage == "solve" and "interrupted_readers" in case:
        forms = {p["writer"] for p in design["delivery_arms"].values()}
        design["writing"] = dict(cfg["writing"], lines={k: v for k, v in cfg["writing"]["lines"].items() if k in forms})
        design["interrupted_readers"] = case["interrupted_readers"]
    return design


def shard_profile(cfg, case, stage, grant, jobs):
    guard_kind = "source" if stage == TARGET_NO_MEMORY else stage
    return dict(name=cfg["name"], stage=stage, case=case["task"], registration_sha256=cfg["registration_sha256"],
                guards=dict(max_usd=float(grant), max_calls=CALLS[guard_kind](len(jobs)),
                            max_seconds=SECONDS[guard_kind](len(jobs))),
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


def checked_input_stop(folder, case, close, rows, profile, design, identity, models):
    invalid = {"FIRST_PASS_MISMATCH", "TASK_START_MISMATCH"}
    codes = {(r.get("result") or {}).get("error_code") for r in rows}
    if not codes & invalid and close.get("stop_reason") not in {s.lower() for s in invalid}:
        return None
    if profile["stage"] == "source" and codes == {"TASK_START_MISMATCH"}:
        return customer_tool_stop(folder, case, profile, design, identity, models)
    raise ContractError("A consultation's first input differs from its first pass")


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
    opening = case.get("opening", case)
    donor = case.get("donor", case)
    imported = opening.get("imported_first_pass")
    source_dir = (safe_path(workspace / "runs", imported["directory"]) if imported
                  else output / ("task-" + opening["task"]) / "source")
    if stage == "source":
        require(not imported, "A reused first pass cannot be resubmitted")
    if stage == TARGET_NO_MEMORY:
        require("donor" not in case and stage_name in case["scheduled_stages"] and stage_name in cfg["target_no_memory_stages"]
                and case["task_id"] in cfg["transfer_tools"], "Unregistered no-memory target control")
        case = dict(case, target_no_memory_index=cfg["target_no_memory_stages"].index(stage_name) + 2)
    tid = case["task_id"]
    plan = dict(sources={}, tasks={tid: common["tasks"][opening["task"]]}, connection=conn, modules=modules_for(str(workspace)),
                instruction="" if stage == TARGET_NO_MEMORY else asset_text(workspace, cfg["delivery_instruction"]))
    spec = dict(registration_sha256=cfg["registration_sha256"], native_sha256=case["native_sha256"],
                customer_opening_sha256=case["customer_opening_sha256"], instruction_sha256=cfg["delivery_instruction"]["sha256"],
                episode_cap_usd=cfg["episode_cap_usd"], http_timeout_seconds=cfg["http_timeout_seconds"],
                task_start_mode=cfg["task_start_mode"])
    if case.get("banking_nl_assertion_judge"):
        spec["banking_nl_assertion_judge"] = True
    if cfg.get("grade_native_step_cap"):
        spec["grade_native_step_cap"] = True
    if "query_tool_descriptions" in cfg and stage != TARGET_NO_MEMORY:
        spec["query_tool_descriptions"] = cfg["query_tool_descriptions"]
    source = None
    if "donor" in case or stage == TARGET_NO_MEMORY:
        _, parent, (row,) = sealed_rows(source_dir)
        require(not imported and row["task_id"] == tid and row["arm"] == "no_memory"
                and parent["profile"]["case"] == opening["task"]
                and parent["profile"]["registration_sha256"] == cfg["registration_sha256"]
                and parent["design"]["stage"] == "source", "Wrong independent target opening")
        receipt = target_opening_receipt(safe_path(source_dir, row["directory"]), row, spec)
        spec.update(source=receipt, public_context_sha256=receipt["public_context_sha256"])
        if stage == TARGET_NO_MEMORY:
            spec["target_no_memory_control"] = True
    elif stage != "source":
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
    if "donor" in case:
        donor_import = donor.get("imported_first_pass")
        donor_dir = (safe_path(workspace / "runs", donor_import["directory"]) if donor_import
                     else output / ("task-" + donor["task"]) / "source")
        source = source_from(donor_dir, donor["task_id"])
        require(source is not None and spec.get("source"), "Transfer needs its donor and an independent target opening")
        if donor_import:
            require((source.source_run_id, source.source_log_sha256) == (donor_import["run_id"], donor_import["log_sha256"]),
                    "Imported transfer donor changed")
        if "pool_sources" in case:
            require(donor["task_id"] == tid and source.source_run_id == spec["source"]["run_id"]
                    and source.source_log_sha256 == spec["source"]["log_sha256"],
                    "Cohort writer and opening must come from the same first pass")
            spec["cohort"] = dict(sources=case["pool_sources"])
        else:
            spec["donor"] = dict(task_id=donor["task_id"], run_id=source.source_run_id,
                                 log_sha256=source.source_log_sha256, public_context_sha256=source.public_context_sha256)
        plan["sources"] = {donor["task_id"]: source}
    design = study_design(cfg, case, stage, source)
    jobs = study_jobs(case, stage)
    profile = shard_profile(cfg, case, stage, grant, jobs)
    if "pool_sources" in case:
        from agent_memory_eval.study.adapters.tau_cohort import bind_cohort_pools
        bind_cohort_pools(design, plan, case, cfg["cohort_catalog"])
    if not (dest / "completion.json").exists():
        bindings = None
        if stage == "solve" and "pool_sources" not in case:
            archived = case.get("imported_writer_stage")
            require(bool(archived) == ("interrupted_readers" in case), "A retried reader uses its original writer stage")
            if archived:
                write_dir = safe_path(workspace / "runs", archived["directory"])
                registration, seal = archived["registration_sha256"], archived["seal"]
            else:
                write_dir, registration, seal = output / ("task-" + donor["task"]) / "write", cfg["registration_sha256"], None
            _, parent, written = sealed_rows(write_dir, seal)
            require(parent["profile"]["registration_sha256"] == registration
                    and parent["design"] == study_design(cfg, donor, "write", source)
                    and [r["arm"] for r in written] == [j["arm"] for j in study_jobs(donor, "write")],
                    "Writer banks do not belong to this registered task and first pass")
            by_arm = {r["arm"]: r for r in written}
            bindings = (write_dir, {(1, donor["task_id"], arm): by_arm[d["writer"]] for arm, d in design["delivery_arms"].items()})
            if archived:
                for job in jobs:
                    old = case["interrupted_readers"][job["arm"]]
                    writer = bindings[1][1, donor["task_id"], job["arm"]]
                    require((old["index"], old["writer"], old["snapshot"]) == (job["index"], writer["run_id"],
                            writer["result"]["snapshot_id"]), "A retried slot has another assignment or memory")
        if stage != "write" and common.get("judge") is not None and case.get("banking_nl_assertion_judge", True):
            plan["grader"] = judge_config(cfg), common["judge"]
        plan.update(jobs=jobs, matched_trial=spec, summary=dict(native_sha256=case["native_sha256"],
                    jobs_sha256=design["allocation"]["jobs_sha256"]))
        guard = BatchGuard(**profile["guards"])
        reserve = CostReservations(dest, dict(codes=cfg["unpriced_http_codes"], maximum_per_stage=len(jobs),
                                   reserve_uncertain_transport=True), guard, dict(models, grader=common.get("judge")))
        execute_prepared(profile, workspace, dest, identity, design, plan, models, guard, imported_writers=bindings,
                         close_cost=reserve)
    close, ctl, rows = sealed_rows(dest)
    require(ctl["profile"] == profile and ctl["design"] == design and ctl["implementation"] == identity
            and endpoint_terms(ctl["model_catalog_entries"]) == endpoint_terms(models)
            and len(rows) == len(jobs) and all(all(r[k] == v for k, v in j.items()) for r, j in zip(rows, jobs)),
            "The sealed shard does not match its registration")
    stop = checked_input_stop(dest, case, close, rows, profile, design, identity, models)
    if stop:
        fixed(task_dir / "customer-opening-stop.json", stop)


def approval_for(plan, record, *, max_usd, user_authorization, through_stage=None):
    """The spending record a person approves: this plan, this resolved allocation, one cumulative ceiling."""
    return dict(format=APPROVAL, plan_id=plan["id"], plan_sha256=plan_digest(plan), resolved_sha256=digest(canonical(record)),
                assigned=record["assigned"], max_usd=str(max_usd), user_authorization=user_authorization,
                **({"through_stage": through_stage} if through_stage is not None else {}))


def study_models(cfg, conn):
    return live_models(conn), judge_endpoint(cfg) if "grader" in cfg["expected_endpoint_terms"] else None


class Tau2Study:
    """A τ² domain behind the declared-study runner; subclasses only name the domain."""
    cross_task = False
    name = None
    execution_mode = "staged"

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
        require(("transfer" in plan) == self.cross_task, "Select the adapter for the full study plan")
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
        queries = {name: r["tool_descriptions"] for name, r in readers.items() if "tool_descriptions" in r}
        if len(queries) > 1 or queries and any(r["read_mode"] == "on_demand" and "tool_descriptions" not in r
                                               for r in readers.values()):
            cfg["query_tools_by_arm"] = {arm_name(rep, name): queries[name] for rep, name in pairs if name in queries}
        elif queries:
            cfg["query_tool_descriptions"] = next(iter(queries.values()))
        asset_text(workspace, cfg["connection"])
        conn = read_json(asset_text(root, cfg["connection"]).encode())
        validate_profile(conn)
        conn["benchmark"].update(domain=setup["domain"], split=setup.get("split"))
        if spec["retrieval"]:
            conn["benchmark"]["retrieval_config"] = spec["retrieval"]
        endpoints = read_json(asset_text(root, cfg["endpoints"]).encode())["models"]
        roles = ("worker", "user_simulator")
        if "nl_assertion_judge" in setup:
            require(self.name == "tau-banking" and plan["format"] == TRANSFER_FORMAT and is_cohort(plan)
                    and setup["nl_assertion_judge"] == "when_active",
                    "The full banking judge setup requires a registered own/mixed cohort")
            modules_for(str(workspace))
            from tau2.domains.banking_knowledge.environment import get_tasks
            requested = set(plan["benchmark"]["tasks"])
            cfg["banking_nl_assertion_tasks"] = [
                t.id for t in get_tasks() if t.id in requested and "NL_ASSERTION" in t.evaluation_criteria.reward_basis
                and t.evaluation_criteria.nl_assertions
            ]
        with_judge = spec["judge"] or bool(cfg.get("banking_nl_assertion_tasks"))
        cfg["expected_endpoint_terms"] = {r: cfg["endpoint_terms"][r] for r in (*roles, *(("grader",) if with_judge else ()))}
        cfg["expected_model_revisions"] = {r: endpoints[r]["canonical_slug"] for r in roles}
        declared = model_profile(plan, root)
        if declared is not None:
            revisions = dict(cfg["expected_model_revisions"])
            if with_judge:
                revisions["grader"] = cfg["judge"]["canonical_slug"]
                require(declared.get("judge") == cfg["judge"], "Declared judge differs from the native judge setup")
            require(declared.get("connection") == cfg["connection"]
                    and all(declared["expected_revisions"].get(r) == v for r, v in revisions.items())
                    and all(declared["endpoint_terms"].get(r) == v for r, v in cfg["expected_endpoint_terms"].items()),
                    "Declared model profile differs from the worker, simulator or grader in the benchmark setup")
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
        active_assertions = tid in cfg.get("banking_nl_assertion_tasks", ())
        control = native_control(orch, native, conf, cfg["limits"], cfg["http_timeout_seconds"],
                                 judge_config(cfg) if self.spec["judge"] or active_assertions else None)
        opening = opening_hash(orch, role_settings(control)["user_simulator"], modules[2])
        names = {t.name for t in orch.agent.tools}
        require(set(cfg.get("transfer_tools", {}).get(tid, [])) <= names, "Target decision names an unavailable native tool")
        require(not any(n.startswith("memory_") for n in names) and self.spec["native_tool"] in (None, *names),
                "Expected only the native business tools before memory assignment")
        native_case = dict(native_sha256=digest(canonical(control)), task_sha256=control["task_sha256"],
                           customer_opening_sha256=opening, initial_db=control["initial_db"],
                           official_criteria_sha256=digest(canonical(task.evaluation_criteria.model_dump(mode="json"))))
        if "banking_nl_assertion_tasks" in cfg:
            native_case["banking_nl_assertion_judge"] = active_assertions
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
            supported = basis in spec["basis"] and (spec["nl_assertions"] or not task.evaluation_criteria.nl_assertions)
            if tid in cfg.get("banking_nl_assertion_tasks", ()):
                supported = basis == {"DB", "NL_ASSERTION"} and bool(task.evaluation_criteria.nl_assertions)
            require(supported and greeting_start(task.initial_state), f"Task {tid} has unregistered grading or an initial action")
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
        expected_sources = plan["first_pass"].get("required_sources_sha256")
        if expected_sources:
            source_ids = []
            for case in cases:
                source = case.get("imported_first_pass")
                require(source, f"Missing the registered graded first pass for {case['task']}")
                source_ids.append([case["task"], source["run_id"], source["log_sha256"]])
            require(digest(canonical(source_ids)) == expected_sources, "The sealed first passes differ from the planned cohort")
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

    def _budget_check(self, plan, cfg, cases, *, approved=None):
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
        if is_cohort(plan) or approved is not None:
            wave = max((sum(stage in c["scheduled_stages"] for c in cases)
                        for stage in ("source", "write", *cfg["solve_stages"])), default=0)
        else:
            wave = sum(bool(c["scheduled_stages"]) for c in cases)
        shards = max(1, min(cfg["max_concurrent"], wave))
        ceiling = Decimal(plan["budget"]["max_usd"]) if approved is None else approved
        grant = ceiling / shards
        if is_cohort(plan) and approved is None and ceiling == 0:
            minimum = (2 * largest).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
            return dict(largest_request_reservation_usd=f"{largest:.6f}", first_wave_shard_grant_usd="0.000000",
                        minimum_shard_grant_usd=str(minimum), funding="pending")
        if approved is not None or is_cohort(plan):
            grant = grant.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        require(grant >= 2 * largest, f"Budget leaves each shard less than twice the largest request reservation "
                                      f"(${largest:.4f} per request, ${grant:.4f} per shard). Raise budget.max_usd or lower "
                                      f"limits.max_concurrent.")
        return dict(largest_request_reservation_usd=f"{largest:.6f}", first_wave_shard_grant_usd=f"{grant:.6f}")

    def check_approval_budget(self, plan, resolved, max_usd):
        from agent_memory_eval.study.followup_budget import approval_amount
        amount = approval_amount(max_usd)
        require(amount <= Decimal(plan["budget"]["max_usd"]), "Approval exceeds the registered spending ceiling")
        return self._budget_check(plan, resolved["cfg"], resolved["cases"], approved=amount)

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
            _, ctl, rows = sealed_rows(task_dir / latest)
            failed = [r for r in rows if r["status"] == "error" and (r.get("result") or {}).get("official_success") is None
                      and (r.get("result") or {}).get("error_code") in codes]
            if not failed:
                continue
            write_dir = root / ("task-" + case.get("donor", case)["task"]) / "write"
            _, _, written = sealed_rows(write_dir)
            writers = {w["arm"]: w for w in written}
            readers = {}
            for r in failed:
                writer = writers[case["arm_writer"][r["arm"]]]
                readers[r["arm"]] = dict(index=r["index"], writer=writer["run_id"], snapshot=writer["result"]["snapshot_id"])
                if "pool_sources" in case:
                    readers[r["arm"]]["sources_sha256"] = ctl["design"]["delivery_arms"][r["arm"]]["sources_sha256"]
            retry.append(dict(case, scheduled_stages=[f"{stage}-retry-{attempt}"], interrupted_readers=readers,
                              imported_writer_stage=dict(directory=str(write_dir.relative_to(root.parent)),
                                                         seal=shard_seal(write_dir),
                                                         registration_sha256=cfg["registration_sha256"])))
        return retry

    def _groups(self, root, plan, cfg, cases, sleep):
        yield ("source", "source", [c for c in cases if "source" in c["scheduled_stages"]])
        for stage in cfg.get("target_no_memory_stages", ()):
            yield (stage, stage, [c for c in cases if stage in c["scheduled_stages"]])
        writers = [c for c in cases if "write" in c["scheduled_stages"]]
        if writers:
            yield ("write", "write", writers)
        live = [c for c in cases if "solve" in c["scheduled_stages"]]
        if not live:
            return
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
            sleep=time.sleep, progress=None, through_stage=None):
        from agent_memory_eval.reproduction.freeze import implementation_identity
        check_through_stage(plan, through_stage, approved_through=approval.get("through_stage"))
        require_declared_models(plan)
        workspace, root = Path(workspace), Path(root)
        resolved = self.resolve(plan, workspace, root)
        record, cfg = resolved["record"], resolved["cfg"]
        self.check_approval_budget(plan, resolved, approval.get("max_usd"))
        require(approval.get("format") == APPROVAL and approval.get("plan_sha256") == plan_digest(plan)
                and approval.get("resolved_sha256") == digest(canonical(record)) and approval.get("assigned") == record["assigned"]
                and Decimal(approval["max_usd"]) <= Decimal(plan["budget"]["max_usd"]) and approval.get("user_authorization"),
                "Spending is not authorized for this resolved plan")
        require(bool(os.environ.get("OPENROUTER_API_KEY")), "Missing OpenRouter credentials")
        identity = identity or implementation_identity(root)
        models, judge = (model_resolver or study_models)(cfg, resolved["conn"])
        terms = endpoint_terms(dict(models, grader=judge) if judge is not None else models)
        require(terms == cfg["expected_endpoint_terms"], "Worker, customer or judge endpoint terms changed")
        require({role: m.get("canonical_slug") for role, m in models.items()} == cfg["expected_model_revisions"]
                and (judge is None or judge.get("canonical_slug") == cfg["judge"]["canonical_slug"]),
                "Worker, customer or judge differs from the registered model revision")
        run_root = workspace / "runs" / plan["id"]
        run_root.mkdir(parents=True, exist_ok=True)
        fixed(run_root / "resolved-plan.json", record)
        registration = dict(format="agent-memory-study-run/v1", adapter=self.name, implementation=identity,
                            preflight=record, endpoint_terms=terms, approval=approval)
        common = dict(workspace=str(workspace), output=str(run_root), cfg=cfg, conn=resolved["conn"], identity=identity,
                      models=models, judge=judge, tasks=resolved["tasks"])

        def verify_sealed(case, stage, grant):
            folder = run_root / ("task-" + case["task"]) / stage
            close, ctl, rows = sealed_rows(folder)
            kind = cfg["stage_kinds"][stage]
            # Only the first-pass customer-tool exception needs a reconstructed registration.
            profile = shard_profile(cfg, case, kind, grant, rows)
            design = study_design(cfg, case, "source") if kind == "source" else ctl["design"]
            stop = checked_input_stop(folder, case, close, rows, profile, design, identity, models)
            if stop:
                fixed(folder.parent / "customer-opening-stop.json", stop)

        return run_stages(run_root, registration, self._groups(run_root, plan, cfg, resolved["cases"], sleep),
                          (bank_shard, common), total_usd_limit=approval["max_usd"], max_concurrent=cfg["max_concurrent"],
                          admission=self._admission(cfg), verify_sealed=verify_sealed, pool_factory=pool_factory,
                          progress=progress, through_stage=through_stage)

    def _case_rows(self, workspace, run_root, case, origin, label, attempt_base, unstarted):
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
            status, success, reason, step_cap = outcome_fields(obs)
            effort = obs.get("effort") or {}
            return dict(task=case["task_id"], source=case["task"], repeat=repeat, condition=condition, origin=origin_name,
                        attempt=attempt, status=status, success=success, missing_reason=reason, step_cap=step_cap,
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
        for stage in case.get("scheduled_stages", []):
            reason = unstarted.get(f"{task_dir.name}/{stage}")
            if not reason:
                continue
            obs = dict(success=None, status="missing", reason=reason)
            if stage == "source":
                rows.append(row("no_memory", obs, 0))
            elif stage == "solve" or stage.startswith("solve-repeat-"):
                repeat = 1 if stage == "solve" else int(stage.removeprefix("solve-repeat-"))
                rows.extend(row(label(arm), obs, attempt_base, repeat) for arm in case["arms"])
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
        unstarted = unstarted_stages(run_root)
        for case in record["cases"]:
            rows += self._case_rows(workspace, run_root, case, plan["id"], lambda arm: arm, 0, unstarted)
            if "first_pass_missing" in case:
                rows += [dict(task=case["task_id"], source=case["task"], repeat=n, condition=arm, origin=plan["id"],
                              attempt=0, status="missing", success=None, missing_reason="first_pass_ungraded",
                              tokens=None, business_calls=None, reads_list=0, reads_get=0, preloaded=False, cost_usd=None)
                         for arm in case["arms"] for n in range(1, plan["repeats"] + 1)]
        sources = {c["task"]: c for c in record["cases"]}
        for ref in plan.get("references", []):
            ref_root = workspace / "runs" / ref["recording"]
            label_root = ref.get("attempt_of", ref["recording"])
            ref_cases = [c for c in read(ref_root / "registration.json")["preflight"]["cases"] if c["task"] in sources]
            require(ref_cases, f"Reference {ref['recording']} shares no first pass with the plan")
            ref_unstarted = unstarted_stages(ref_root)
            for case in ref_cases:
                source = first_pass_identity(workspace, run_root, sources[case["task"]])
                reference_source = first_pass_identity(workspace, ref_root, case)
                if source is None or reference_source is None:
                    reason = "plan_first_pass_unavailable" if source is None else "reference_first_pass_unavailable"
                    solve_stages = [s for s in case.get("scheduled_stages", ["solve"])
                                    if s == "solve" or s.startswith("solve-repeat-")]
                    for stage in solve_stages:
                        repeat = 1 if stage == "solve" else int(stage.removeprefix("solve-repeat-"))
                        rows.extend(dict(task=case["task_id"], source=case["task"], repeat=repeat,
                                         condition=f"{label_root}/{arm}", origin=ref["recording"], attempt=int("attempt_of" in ref),
                                         status="missing", success=None, missing_reason=reason, tokens=None, business_calls=None,
                                         reads_list=0, reads_get=0, preloaded=False, cost_usd=None) for arm in case["arms"])
                    continue
                require(source == reference_source,
                        f"Reference {ref['recording']}/{case['task']} does not share the sealed first pass")
                rows += [r for r in self._case_rows(workspace, ref_root, case, ref["recording"],
                                                    lambda arm, p=label_root: f"{p}/{arm}", 1 if "attempt_of" in ref else 0,
                                                    ref_unstarted)
                         if r["condition"] != "no_memory"]
        return rows


class TauBanking(Tau2Study):
    name = "tau-banking"


class Tau2Airline(Tau2Study):
    name = "tau2-airline"


class Tau2Retail(Tau2Study):
    name = "tau2-retail"
