"""Run the frozen full retail test set through source, four writers and six native solves."""

import argparse
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal, ROUND_DOWN
import fcntl
from functools import lru_cache
from multiprocessing import get_context
import os
from pathlib import Path

from agent_memory_eval.adapters.tau2 import PIN, native_modules, verify_checkout
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import (
    FORMS,
    RETAIL_FULL,
    BatchGuard,
    endpoint_terms,
    execute_prepared,
    live_models,
    preflight_sizes,
    solve_connection,
    write_json,
)
from agent_memory_eval.experiments.fresh_forms import CostReservations
from agent_memory_eval.models.openrouter import (
    model_catalog,
    model_endpoints,
    resolve_endpoint,
    resolve_model,
)
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.experience import _pair, _source
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.experiments.forms_write import FormSource, _project_context
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.runner.tau2_baseline import native_config

if __package__:
    from .run_recheck_rounds import design_for, fixed, read, sealed_rows, source_from
else:
    from run_recheck_rounds import design_for, fixed, read, sealed_rows, source_from

PROFILE = "protocols/pilots/tau2-retail-full-forms-v1.json"
COHORT = "test"


@lru_cache(maxsize=None)
def modules_for(workspace):
    modules = native_modules(verify_checkout(workspace / "data/tau2-bench"))
    from loguru import logger

    logger.disable("tau2")
    return modules


def owner_ids(t, db):
    args = [a["arguments"] for a in t["evaluation_criteria"]["actions"] or []]
    users = {a["user_id"] for a in args if a.get("user_id") in db["users"]}
    users.update(
        db["orders"][a["order_id"]]["user_id"]
        for a in args
        if a.get("order_id") in db["orders"]
    )
    return {digest(user.encode()) for user in users}


def single_owner(task, db):
    users = owner_ids(task, db)
    if len(users) != 1:
        raise ContractError("Expected one reference customer per retail task")
    return users.pop()


def bind_task(cfg, base, conn, row, raw_task):
    from tau2.data_model.tasks import Task
    from tau2.multilingual.runtime import load_run_language, task_digest
    from tau2.runner.helpers import select_run_tasks

    tid = row["task"]
    design, jobs = full_design(cfg, base, row, "source", None)
    settings = native_config(
        solve_connection(conn, design, jobs[0], cfg["total_usd_limit"])
    )
    (task,) = select_run_tasks(settings, load_run_language(settings))
    original = Task.model_validate(raw_task)
    criteria = task.evaluation_criteria
    if (
        task.id != tid
        or criteria != original.evaluation_criteria
        or set(criteria.reward_basis) not in ({"DB"}, {"DB", "NL_ASSERTION"})
    ):
        raise ContractError(
            "Default English task or criteria do not match official split"
        )
    mutations = sorted(
        {
            a.name
            for a in criteria.actions or []
            if a.name.startswith(("modify", "return", "exchange", "cancel"))
        }
    )
    row.update(
        task_sha256=task_digest(task),
        canonical_task_sha256=task_digest(original),
        assertions=len(criteria.nl_assertions or []),
        communicate_items=len(criteria.communicate_info or []),
        reward_basis=list(criteria.reward_basis),
        operations=mutations,
    )
    return task.model_dump(mode="json")


def prepare(workspace):
    cfg = read(ROOT / PROFILE)
    if (
        cfg["status"] != "ready"
        or cfg["benchmark_revision"] != PIN
        or cfg["forms"] != list(FORMS)
        or cfg["controls"] != ["no_memory", "empty_preload"]
        or cfg["conditions"] != ["original"]
        or cfg["stages"] != ["source", "write", "solve1"]
    ):
        raise ContractError("Unexpected full retail protocol")
    checkout = workspace / "data/tau2-bench"
    modules_for(workspace)
    for path, sha in cfg["assets"].items():
        if digest((checkout / path).read_bytes()) != sha:
            raise ContractError(f"Benchmark file differs: {path}")
    from tau2.config import DEFAULT_LLM_NL_ASSERTIONS, DEFAULT_LLM_NL_ASSERTIONS_ARGS

    if cfg["judge"][
        "native_model"
    ] != DEFAULT_LLM_NL_ASSERTIONS or DEFAULT_LLM_NL_ASSERTIONS_ARGS != {
        "reasoning_effort": "low"
    }:
        raise ContractError("Unexpected pinned native evaluator")
    base = read_json(asset_text(ROOT, cfg["base_design"]).encode())
    conn = read_json(asset_text(ROOT, base["model"]["connection_profile_ref"]).encode())
    data = checkout / "data/tau2/domains/retail"
    raw = {t["id"]: t for t in read(data / "tasks.json")}
    db = read(data / "db.json")
    split = read(data / "split_tasks.json")[cfg["split"]]
    prior = read_json(asset_text(ROOT, cfg["prior_protocol"]).encode())["rounds"]

    seen_ids = {
        i
        for c in prior.values()
        for family, ids in c.items()
        if family != "split"
        for i in ids
    }
    seen_owners = {single_owner(raw[i], db) for i in seen_ids}
    rows, tasks = [], {}
    for tid in split:
        row = dict(
            task=tid,
            customer=single_owner(raw[tid], db),
            split=cfg["split"],
            prior_task=tid in seen_ids,
            prior_customer=single_owner(raw[tid], db) in seen_owners,
        )
        tasks[tid] = bind_task(cfg, base, conn, row, raw[tid])
        rows.append(row)
    if len(rows) != 40 or len(tasks) != 40 or len({r["customer"] for r in rows}) != 31:
        raise ContractError("Incomplete official retail test split")
    return cfg, base, conn, rows, tasks


def full_design(cfg, base, row, stage, source):
    design, jobs = design_for(cfg, base, row.get("cohort", COHORT), row, stage, source)
    design["format"] = RETAIL_FULL
    slots = row.get("selected_slots", {}).get(stage)
    if slots is not None:
        jobs = [j for j in jobs if j["index"] in slots]
        if [j["index"] for j in jobs] != slots or not jobs:
            raise ContractError("Supplement indices differ from unstarted assignments")
        block = design["allocation"]["blocks"][0]
        block["solve"] = [j["arm"] for j in jobs]
        design["allocation"]["jobs_sha256"] = digest(canonical(jobs))
    return design, jobs


def resolve_live(cfg, conn):
    roles = live_models(conn)
    previous = read_json(asset_text(ROOT, cfg["endpoints"]).encode())["models"]
    if endpoint_terms(roles) != endpoint_terms(previous):
        raise ContractError("Worker or simulator endpoint terms have changed")
    judge = cfg["judge"]
    params = judge["parameters"]
    model = resolve_model(model_catalog(), judge["model"], params)
    if model["canonical_slug"] != judge["canonical_slug"]:
        raise ContractError("Judge alias no longer points at the pinned date")
    return roles, resolve_endpoint(
        model, model_endpoints(model["id"]), judge["route"], params
    )


def preflight(cfg, base, rows):
    return dict(
        study=cfg["name"],
        protocol_sha256=digest((ROOT / cfg.get("profile_path", PROFILE)).read_bytes()),
        rows=[
            dict(
                row,
                stages={
                    stage: full_design(cfg, base, row, stage, None)[1]
                    for stage in row.get("scheduled_stages", cfg["stages"])
                },
            )
            for row in rows
        ],
    )


def recorded_source(folder, task):
    source = source_from(folder, task)
    if source is not None:
        return source
    _, _, rows = sealed_rows(folder)
    (row,) = rows
    res = row.get("result", {})
    if not res.get("before_grade_ref") or res["termination_reason"] not in {
        "user_stop",
        "agent_stop",
    }:
        return None
    run = row["run_id"]
    with _source(safe_path(folder, row["directory"]), res["log_sha256"]) as (
        scan,
        blobs,
        _,
        _,
    ):
        events = [e for e in scan["events"].values() if e["run_id"] == run]
        (start,) = [e for e in events if e["type"] == "run.started"]
        (end,) = [e for e in events if e["type"] == "run.ended"]
        (grade,) = [e for e in events if e["type"] == "outcome.scored"]
        (state,) = [
            e["payload"]
            for e in events
            if e["type"] == "business.transition"
            and e["payload"].get("origin") == "forms.native_result"
        ]
        sim = blobs.get(res["before_grade_ref"])
        req, reply = _pair(scan, run)
        context = _project_context(
            blobs.get(req["payload"]["request_ref"]),
            blobs.get(reply["payload"]["response_ref"]),
        )
        if (
            grade["payload"]["official_success"] is not None
            or res["official_success"] is not None
            or sim["task_id"] != task
            or state["before_grade_ref"] != res["before_grade_ref"]
            or sim["termination_reason"] != res["termination_reason"]
            or start["condition_id"] != "no_memory"
            or start["payload"]["bank_id"]
            or start["payload"]["source_run_ids"]
            or context["task_context"] != blobs.get(start["payload"]["task_ref"])
        ):
            raise ContractError(
                "Incomplete provenance for a finished but ungraded source"
            )
        data = canonical(context)
        return FormSource(
            task,
            run,
            res["log_sha256"],
            end["event_id"],
            end["event_seq"],
            data.decode(),
            digest(data),
            len(data),
            None,
        )


def bank_parent(workspace, cfg, row):
    return (
        workspace
        / "runs"
        / row.get("source_study", cfg.get("run_directory", cfg["name"]))
        / ("task-" + row["task"])
    )


def stage_order(cfg, rows, stage):
    members = [
        row for row in rows if stage in row.get("scheduled_stages", cfg["stages"])
    ]
    return sorted(
        members, key=lambda r: digest(canonical([cfg["name"], stage, r["task"]]))
    )


def stage_job(args):
    (
        workspace,
        root,
        cfg,
        base,
        conn,
        row,
        task,
        stage,
        identity,
        models,
        judge,
        grant,
    ) = args
    parent = root / ("task-" + row["task"])
    parent.mkdir(exist_ok=True)
    dest = parent / stage
    tid = row["task"]
    bank = bank_parent(workspace, cfg, row)
    source = None if stage == "source" else recorded_source(bank / "source", tid)
    design, jobs = full_design(cfg, base, row, stage, source)
    profile = dict(
        name=cfg["name"],
        stage=stage,
        task=tid,
        config_sha256=digest((ROOT / cfg.get("profile_path", PROFILE)).read_bytes()),
        implementation_commit=identity["implementation_commit"],
        http_timeout_seconds=cfg["http_timeout_seconds"],
        guards=dict(cfg["guards"][stage], max_usd=float(grant)),
    )
    if dest.exists():
        _, ctl, previous = sealed_rows(dest)
        if (
            ctl["profile"] != profile
            or ctl["design"] != design
            or ctl["implementation"] != identity
            or endpoint_terms(ctl["model_catalog_entries"]) != endpoint_terms(models)
            or len(previous) != len(jobs)
            or any(
                any(p[k] != v for k, v in j.items())
                for p, j in zip(previous, jobs, strict=True)
            )
        ):
            raise ContractError("Existing shard differs from registration")
        return
    sources = {tid: source} if source else {}
    plan = dict(
        jobs=jobs,
        sources=sources,
        connection=conn,
        tasks={tid: task},
        instruction=asset_text(ROOT, base["second_pass"]["solve_instruction_ref"]),
        summary=dict(jobs_sha256=digest(canonical(jobs))),
    )
    bindings = None
    if stage == "write":
        if source:
            plan["summary"]["wire_sizes"] = preflight_sizes(
                jobs,
                design,
                sources,
                plan["tasks"],
                conn,
                plan["instruction"],
                asset_text(ROOT, base["writing"]["common_instruction_ref"]),
            )
        else:
            plan["missing_sources"] = [tid]
    else:
        plan["modules"] = modules_for(workspace)
        plan["grader"] = (
            dict(cfg["judge"], timeout_seconds=cfg["http_timeout_seconds"]),
            judge,
        )
        if stage.startswith("solve"):
            _, _, ws = sealed_rows(bank / "write")
            bindings = (
                bank / "write",
                {(w["repetition"], tid, w["arm"]): w for w in ws},
            )
    if cfg.get("episode_admission"):
        from agent_memory_eval.experiments.episode_admission import EpisodeAdmission

        plan["admission"] = EpisodeAdmission(root, cfg["episode_admission"], dest)
    guard = BatchGuard(**profile["guards"])
    reserve = CostReservations(
        dest, cfg["unpriced_http"], guard, dict(models, grader=judge)
    )
    execute_prepared(
        profile,
        ROOT,
        dest,
        identity,
        design,
        plan,
        models,
        guard,
        imported_writers=bindings,
        close_cost=reserve,
    )


def accounting(folder, grant):
    end, _, rows = sealed_rows(folder)
    known = sum(
        (
            Decimal(v["reported_usd"])
            for r in rows
            for v in r.get("result", {}).get("billing_by_role", {}).values()
        ),
        Decimal(0),
    )
    pending = Decimal(end["reserved_usd"])
    unknown = any(
        r.get("result", {}).get("unknown_cost")
        and not r["result"].get("unpriced_reservation")
        for r in rows
    )
    total = Decimal(grant) if unknown else known + pending
    if total > Decimal(grant) or total < known:
        raise ContractError("Committed shard spending exceeds its allocated grant")
    return dict(
        reported=str(known),
        held=str(total - known),
        spent=str(total),
        http=end["http_calls"],
        seal=digest((folder / "completion.json").read_bytes()),
    )


def run_study(workspace, cfg, base, conn, rows, tasks, record):
    root = workspace / "runs" / cfg.get("run_directory", cfg["name"])
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise ContractError("Missing OpenRouter credentials")
        identity = implementation_identity(ROOT)
        models, judge = resolve_live(cfg, conn)
        terms = endpoint_terms(dict(models, grader=judge))
        if "expected_endpoint_terms" in cfg and terms != cfg["expected_endpoint_terms"]:
            raise ContractError("Expansion endpoint terms differ from the source study")
        reg = dict(
            implementation=identity,
            preflight=record,
            endpoint_terms=terms,
            judge_slug=judge["canonical_slug"],
        )
        fixed(root / "registration.json", reg)
        total = Decimal(cfg.get("prior_covered_usd", "0"))
        seals = {}
        size = cfg["max_concurrent"]
        with ProcessPoolExecutor(
            max_workers=size, mp_context=get_context("spawn")
        ) as pool:
            for stage in cfg["stages"]:
                ordered = stage_order(cfg, rows, stage)
                width = cfg.get("concurrency_by_stage", {}).get(stage, size)
                for begin in range(0, len(ordered), width):
                    wave = ordered[begin : begin + width]
                    grant = (
                        (Decimal(str(cfg["total_usd_limit"])) - total) / len(wave)
                    ).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
                    if grant <= 0:
                        raise ContractError("Study budget exhausted")
                    fixed(
                        root / f"{stage}-wave-{begin // width + 1}.json",
                        dict(
                            tasks=[r["task"] for r in wave],
                            per_shard_grant=str(grant),
                            prior_covered=str(total),
                        ),
                    )
                    list(
                        pool.map(
                            stage_job,
                            [
                                (
                                    workspace,
                                    root,
                                    cfg,
                                    base,
                                    conn,
                                    r,
                                    tasks[r["task"]],
                                    stage,
                                    identity,
                                    models,
                                    judge,
                                    str(grant),
                                )
                                for r in wave
                            ],
                        )
                    )
                    for r in wave:
                        name = "task-" + r["task"] + "/" + stage
                        entry = accounting(root / name, grant)
                        total += Decimal(entry["spent"])
                        seals[name] = entry
                    print(
                        stage,
                        begin + len(wave),
                        "of",
                        len(ordered),
                        "cumulative covered USD",
                        total,
                        flush=True,
                    )
        fixed(
            root / "completion.json",
            dict(
                registration_sha256=digest((root / "registration.json").read_bytes()),
                stages=seals,
                total_covered_usd=str(total),
            ),
        )
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["preflight", "run"])
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    a = parser.parse_args()
    workspace = a.workspace.resolve()
    cfg, base, conn, rows, tasks = prepare(workspace)
    record = preflight(cfg, base, rows)
    if a.mode == "preflight":
        if a.output:
            fixed(a.output, record)
        print(
            "40 source episodes, 160 writers, 240 native solves, 31 customer groups",
            flush=True,
        )
        return
    run_study(workspace, cfg, base, conn, rows, tasks, record)


if __name__ == "__main__":
    main()
