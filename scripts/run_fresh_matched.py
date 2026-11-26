"""Register and run a new matched first pass, five writers and two readers per bank."""

import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from agent_memory_eval.contracts.connection import validate_profile
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.batch_recovery import POLICY as RESUME_UNSTARTED, stage_lease
from agent_memory_eval.experiments.forms_batch import (
    BatchGuard, FRESH_MATCHED, endpoint_terms, execute_prepared, live_models, solve_connection,
)
from agent_memory_eval.experiments.fresh_forms import CostReservations, scenario_customer
from agent_memory_eval.experiments.matched_trial import native_control, opening_hash, role_settings, source_receipt
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.runner.tau2_baseline import build_baseline_orchestrator, native_config
from agent_memory_eval.runner.task_start import FORMAT as TASK_START_FORMAT
from scripts.run_full_retail import modules_for, owner_ids, resolve_live, run_study
from scripts.run_recheck_rounds import read, sealed_rows, source_from


PROFILE = "protocols/pilots/tau2-fresh-matched-v1.json"
PREFLIGHT = "artifacts/pilots/20261124-fresh-matched-preflight.json"
FORMS = ("fact", "episode", "procedure", "lesson", "free")
MODES = ("preload", "on_demand")
STAGES = ("source", "write", "solve")


def jobs_for(case, stage):
    forms = list(FORMS)
    modes = tuple(case.get("reader_modes", MODES))
    if not modes or len(set(modes)) != len(modes) or any(m not in MODES for m in modes):
        raise ContractError("Invalid registered reader delivery modes")
    arms = {"source": ["no_memory"], "write": forms,
            "solve": [f"{form}__{mode}" for form in forms for mode in modes]}[stage]
    ordered = sorted(arms, key=lambda a: digest(canonical([FRESH_MATCHED, case["task"], stage, a])))
    return [dict(index=i, repetition=1, task_id=case["task_id"],
                 phase="write" if stage == "write" else "solve", arm=arm)
            for i, arm in enumerate(ordered, 1)]


def design_for(cfg, case, stage, source=None):
    feedback = cfg.get("writer_feedback_level", "none")
    if feedback not in {"none", "result_only"}:
        raise ContractError("Unknown matched writer feedback level")
    cohort = dict(task_id=case["task_id"], source_group=case["customer"])
    if source is not None:
        cohort.update(run_id=source.source_run_id, source_log_sha256=source.source_log_sha256,
                      public_context_sha256=source.public_context_sha256, public_context_bytes=source.public_context_bytes)
        if feedback == "result_only":
            cohort["result_feedback"] = source.result_receipt()
    if "first_writer_inputs" in case:
        cohort["first_writer_inputs"] = case["first_writer_inputs"]
    jobs = jobs_for(case, stage)
    design = dict(format=FRESH_MATCHED, stage=stage, source=dict(domain=case["domain"], split=case["split"],
        feedback_level=feedback, cohort=[cohort]), writing=cfg["writing"],
        second_pass=dict(proposed_limits=cfg["limits"]), model=dict(connection_profile_ref=cfg["connection"],
        seed_pairs=[dict(repetition=1, writer=cfg["writer_seed"], native_and_solver=cfg["native_and_worker_seed"])]),
        delivery_arms={j["arm"]: dict(zip(("writer", "read_mode"), j["arm"].split("__"))) for j in jobs
                       if stage == "solve"}, allocation=dict(blocks=[dict(task_id=case["task_id"], repetition=1,
            write=[j["arm"] for j in jobs if stage == "write"])], jobs_sha256=digest(canonical(jobs))))
    if "reader_admission" in cfg:
        design["reader_admission"] = cfg["reader_admission"]
    return design


def judge_config(cfg, domain):
    return dict(cfg["judge"], timeout_seconds=cfg["http_timeout_seconds"]) if domain == "retail" else None


def study_models(cfg, conn):
    if "grader" in cfg["expected_endpoint_terms"]:
        return resolve_live(cfg, conn)
    return live_models(conn), None


def check_published_cases(record, published):
    scope = record["scope"]
    if published["scope"] != "full" or scope not in ("development", "airline", "full"):
        raise ContractError("Unknown registered comparison scope")
    expected = deepcopy(published)
    if scope != "full":
        early = {"airline-21", "airline-23"}
        cases = [c for c in expected["cases"] if c["domain"] == "airline" and (scope == "airline" or c["task"] in early)]
        for case in cases:
            case["scheduled_stages"] = ["initial-" + s if case["task"] in early else s for s in STAGES]
        expected.update(scope=scope, **allocation_record(cases))
    if expected != record:
        raise ContractError("Selected inputs, task membership or allocations differ from the published preflight")


def allocation_record(cases):
    schedule = {c["task"]: {s: jobs_for(c, s) for s in STAGES} for c in cases}
    counts = {stage: sum(len(jobs[stage]) for jobs in schedule.values()) for stage in STAGES}
    counts["second_pass_no_memory"] = sum(j["arm"] == "no_memory" for jobs in schedule.values() for j in jobs["solve"])
    return dict(task_counts=dict(Counter(c["domain"] for c in cases)),
                unique_customers=len({(c["domain"], c["customer"]) for c in cases}),
                assigned=counts, schedule_sha256=digest(canonical(schedule)), cases=cases)


def prepare(root, workspace, scope, *, protocol=PROFILE):
    cfg = read(root / protocol)
    if "task_start_mode" in cfg and cfg["task_start_mode"] != TASK_START_FORMAT:
        raise ContractError("Unsupported registered task start")
    if (cfg["format"] != FRESH_MATCHED or tuple(cfg["writing"]["lines"]) != FORMS
            or tuple(cfg["read_modes"]) != MODES or scope not in cfg["authorization_options"]):
        raise ContractError("Unexpected matched comparison scope or memory conditions")
    conn = read_json(asset_text(root, cfg["connection"]).encode())
    validate_profile(conn)
    instruction = asset_text(root, cfg["delivery_instruction"])
    common = asset_text(root, cfg["writing"]["common_instruction_ref"]).rstrip("\r\n")
    profile_sha = digest((root / protocol).read_bytes())
    modules = modules_for(workspace)
    from tau2.data_model.tasks import Task
    from tau2.multilingual.runtime import load_run_language
    from tau2.runner.helpers import select_run_tasks

    cases, tasks = [], {}
    for domain, cohorts in cfg["cohorts"].items():
        if domain == "retail" and scope != "full":
            continue
        folder = workspace / "data/tau2-bench/data/tau2/domains" / domain
        originals = {t["id"]: t for t in read(folder / "tasks.json")}
        split_table, db = read(folder / "split_tasks.json"), read(folder / "db.json")
        for split, ids in cohorts.items():
            for tid in ids:
                if scope == "development" and tid not in cfg["development_tasks"]:
                    continue
                key = f"{domain}-{tid}"
                if key in tasks or tid not in split_table[split]:
                    raise ContractError("Duplicate case or incorrect official split")
                original = originals[tid]
                if domain == "airline":
                    customer = digest(scenario_customer(original, db).encode())
                else:
                    owners = owner_ids(original, db)
                    if len(owners) != 1:
                        raise ContractError("Registered retail case must have one identifiable customer")
                    customer = owners.pop()
                case = dict(task=key, domain=domain, split=split, task_id=tid, customer=customer)
                if "task_start_mode" in cfg:
                    case["task_start_mode"] = cfg["task_start_mode"]
                design = design_for(cfg, case, "source")
                conf = solve_connection(conn, design, jobs_for(case, "source")[0], cfg["episode_cap_usd"])
                native = native_config(conf)
                task, = select_run_tasks(native, load_run_language(native))
                if task.id != tid or task.evaluation_criteria != Task.model_validate(original).evaluation_criteria:
                    raise ContractError("The official English task or grading criteria changed")
                tasks[key] = task.model_dump(mode="json")
                orch, native = build_baseline_orchestrator(conf, tasks[key])
                control = native_control(orch, native, conf, cfg["limits"], cfg["http_timeout_seconds"], judge_config(cfg, domain))
                opening = opening_hash(orch, role_settings(control)["user_simulator"], modules[2])
                if control["initial_db"] != orch.environment.get_db_hash() or control["initial_user_db"] != orch.environment.get_user_db_hash():
                    raise ContractError("The native initial state changed while preparing the opening")
                case.update(native_sha256=digest(canonical(control)), customer_opening_sha256=opening,
                            task_sha256=control["task_sha256"], customer_scenario_sha256=digest(canonical(tasks[key]["user_scenario"])),
                            official_criteria_sha256=digest(canonical(tasks[key]["evaluation_criteria"])),
                            initial_db=control["initial_db"])
                # These allocations are part of the selected cohort, with no extra paid repeat.
                early = key in ({"airline-21", "retail-5"} if scope == "full" else {"airline-21", "airline-23"})
                case["scheduled_stages"] = ["initial-" + s if early else s for s in STAGES]
                cases.append(case)
    if len(cases) != cfg["authorization_options"][scope]["tasks"]:
        raise ContractError("Cohort size differs from the registered scope")
    record = dict(series=cfg["name"], scope=scope, protocol_sha256=profile_sha, native_revision=cfg["benchmark_revision"],
        writer_prompt_sha256={f: digest((common + "\n" + line).encode()) for f, line in cfg["writing"]["lines"].items()},
        read_instruction_sha256=digest(instruction.encode()), **allocation_record(cases))
    if "writer_feedback_level" in cfg:
        record["writer_feedback_level"] = cfg["writer_feedback_level"]
    return cfg, conn, cases, tasks, record


def stage_job(args):
    _, output, cfg, _, _, case, *_ = args
    if cfg.get("interruption_policy") == RESUME_UNSTARTED:
        with stage_lease(output / ("task-" + case["task"])):
            return _stage_job(args)
    return _stage_job(args)


def _stage_job(args):
    workspace, output, cfg, _, conn, case, task, stage_name, identity, models, judge, grant = args
    stage = cfg.get("stage_kinds", {}).get(stage_name, stage_name.removeprefix("initial-"))
    task_dir = output / ("task-" + case["task"])
    task_dir.mkdir(exist_ok=True)
    dest = task_dir / stage_name
    source_dir, write_dir = [task_dir / s for s in case["scheduled_stages"][:2]]
    imported = case.get("imported_first_pass")
    if imported:
        if stage == "source":
            raise ContractError("An imported first pass cannot be resubmitted")
        source_dir = safe_path(workspace / "runs", imported["directory"])
    tid = case["task_id"]
    plan = dict(sources={}, tasks={tid: task}, connection=conn, modules=modules_for(workspace),
                instruction=asset_text(ROOT, cfg["delivery_instruction"]))
    spec = dict(registration_sha256=cfg["registration_sha256"], native_sha256=case["native_sha256"],
                customer_opening_sha256=case["customer_opening_sha256"], instruction_sha256=cfg["delivery_instruction"]["sha256"],
                episode_cap_usd=cfg["episode_cap_usd"], http_timeout_seconds=cfg["http_timeout_seconds"])
    if case.get("task_start_mode") != cfg.get("task_start_mode"):
        raise ContractError("Task start differs from case registration")
    if "task_start_mode" in cfg:
        spec["task_start_mode"] = cfg["task_start_mode"]
    source = None
    if stage != "source":
        _, parent, first_rows = sealed_rows(source_dir)
        source_registration = imported["registration_sha256"] if imported else cfg["registration_sha256"]
        if parent["profile"]["registration_sha256"] != source_registration or parent["design"]["stage"] != "source":
            raise ContractError("The new writer or reader was linked to another study")
        source = source_from(source_dir, tid)
        if source is None:
            plan["missing_sources"] = [tid]
        else:
            first_row, = first_rows
            if imported:
                if (source.source_run_id, source.source_log_sha256, source.public_context_sha256) != (
                        imported["run_id"], imported["log_sha256"], imported["public_context_sha256"]):
                    raise ContractError("Imported first pass or public experience changed")
                spec["source_registration_sha256"] = source_registration
            spec.update(source=source_receipt(safe_path(source_dir, first_row["directory"]), source,
                                              dict(spec, registration_sha256=source_registration)),
                        public_context_sha256=source.public_context_sha256)
            plan["sources"][tid] = source
    design = design_for(cfg, case, stage, source)
    jobs = jobs_for(case, stage)
    profile = dict(name=cfg["name"], stage=stage, case=case["task"], registration_sha256=cfg["registration_sha256"],
        guards=dict(max_usd=float(grant), max_calls={"source":201,"write":20,"solve":2010}[stage],
                    max_seconds={"source":1500,"write":1800,"solve":12000}[stage]),
        http_timeout_seconds=cfg["http_timeout_seconds"])
    if "task_start_mode" in cfg:
        profile["task_start_mode"] = cfg["task_start_mode"]
    if "interruption_policy" in cfg:
        profile["interruption_policy"] = cfg["interruption_policy"]
    if not dest.exists() or (cfg.get("interruption_policy") == RESUME_UNSTARTED
                             and not (dest / "completion.json").exists()):
        bindings = None
        if stage == "solve":
            _, parent, written = sealed_rows(write_dir)
            if (parent["profile"]["registration_sha256"] != cfg["registration_sha256"]
                    or parent["design"] != design_for(cfg, case, "write", source)
                    or [r["arm"] for r in written] != [j["arm"] for j in jobs_for(case, "write")]):
                raise ContractError("Writer banks do not belong to this registered task and first pass")
            by_arm = {r["arm"]: r for r in written}
            bindings = (write_dir, {(1, tid, arm): by_arm[d["writer"]] for arm, d in design["delivery_arms"].items()})
        if stage != "write" and case["domain"] == "retail":
            plan["grader"] = judge_config(cfg, "retail"), judge
        plan.update(jobs=jobs, matched_trial=spec, summary=dict(native_sha256=case["native_sha256"],
                    jobs_sha256=design["allocation"]["jobs_sha256"]))
        guard = BatchGuard(**profile["guards"])
        reserve = CostReservations(dest, dict(codes=cfg["unpriced_http_codes"], maximum_per_stage=len(jobs),
                                   reserve_uncertain_transport=True), guard, dict(models, grader=judge))
        execute_prepared(profile, ROOT, dest, identity, design, plan, models, guard,
                         imported_writers=bindings, close_cost=reserve)
    _, ctl, rows = sealed_rows(dest)
    if (ctl["profile"] != profile or ctl["design"] != design or ctl["implementation"] != identity
            or endpoint_terms(ctl["model_catalog_entries"]) != endpoint_terms(models)
            or len(rows) != len(jobs) or any(any(r[k] != v for k, v in j.items()) for r, j in zip(rows, jobs))
            or any(r.get("result", {}).get("error_code") in {"FIRST_PASS_MISMATCH", "TASK_START_MISMATCH"} for r in rows)):
        raise ContractError("The sealed trial does not match its registration")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run"))
    parser.add_argument("--scope", choices=("development", "airline", "full"), required=True)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--protocol", type=Path, default=Path(PROFILE))
    parser.add_argument("--published-preflight", type=Path, default=Path(PREFLIGHT))
    parser.add_argument("--authorization", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    cfg, conn, cases, tasks, record = prepare(ROOT, workspace, args.scope, protocol=args.protocol)
    if args.mode == "preflight":
        print(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if args.authorization is None:
        parser.error("Paid execution requires the new, study-specific spending authorization")
    check_published_cases(record, read(ROOT / args.published_preflight))
    approval = read(args.authorization)
    expected = dict(series=cfg["name"], scope=args.scope, protocol_sha256=record["protocol_sha256"],
                    maximum_usd=cfg["authorization_options"][args.scope]["maximum_usd"])
    if any(approval.get(k) != v for k, v in expected.items()) or not approval.get("user_authorization"):
        raise ContractError("Spending authorization does not cover this scope and protocol")
    runs_root = workspace / "runs"
    if safe_path(runs_root, cfg.get("run_directory", cfg["name"])) == runs_root:
        raise ContractError("A registered series requires its own run directory")
    record["authorization"] = approval
    roles = cfg["endpoint_terms"] if args.scope == "full" else {k:v for k,v in cfg["endpoint_terms"].items() if k != "grader"}
    cfg = dict(cfg, registration_sha256=digest(canonical(record)), total_usd_limit=expected["maximum_usd"],
               expected_endpoint_terms=roles, record_unfunded_shards=True,
               stages=["initial-" + s for s in STAGES] + list(STAGES))
    run_study(workspace, cfg, {}, conn, cases, tasks, record, stage_runner=stage_job, model_resolver=study_models)


if __name__ == "__main__":
    main()
