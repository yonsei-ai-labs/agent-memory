"""Two prespecified cohorts of real first-pass, writing and native retail replay sessions."""

import argparse
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from decimal import Decimal
import fcntl
import json
from multiprocessing import get_context
import os
from pathlib import Path

from agent_memory_eval.adapters.tau2 import PIN, native_modules, verify_checkout
from agent_memory_eval.contracts.connection import validate_profile
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import (
    FORMS,
    RETAIL_RECHECK,
    BatchGuard,
    endpoint_terms,
    execute_prepared,
    live_models,
    preflight_sizes,
    solve_connection,
    write_json,
)
from agent_memory_eval.experiments.forms_solve import model_free_basis
from agent_memory_eval.experiments.forms_write import FormSource, _project_context, read_form_bank
from agent_memory_eval.experiments.fresh_forms import CostReservations
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.experience import _closed_run, _pair, _source
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.runner.tau2_baseline import build_baseline_orchestrator

PROFILE = "protocols/pilots/tau2-retail-recheck-two-rounds-v1.json"
FAMILIES = {
    "cancellation": "cancel_pending_order",
    "refund": "return_delivered_order_items",
    "exchange": "exchange_delivered_order_items",
}


def read(path):
    return read_json(path.read_bytes())


def fixed(path, data):
    if path.exists():
        if read(path) != data:
            raise ContractError(f"Previously sealed {path.name} has different contents")
    else:
        write_json(path, data)


def sealed_rows(folder):
    close = read(folder / "completion.json")

    def load(name, key):
        raw = (folder / name).read_bytes()
        if digest(raw) != close[key]:
            raise ContractError(f"Broken ledger seal in {folder}")
        return read_json(raw)

    return close, load("control.json", "control_sha256"), load("allocations.json", "allocation_sha256")


def select_cohorts(tasks, splits, db):
    seen = set()
    rounds = {}
    for cohort, split in (("confirmation", "test"), ("development", "train")):
        selected = []
        for family, operation in FAMILIES.items():
            candidates = []
            for tid in splits[split]:
                task = tasks[tid]
                criteria = task["evaluation_criteria"]
                actions = criteria["actions"] or []
                writes = [a for a in actions if a["name"].startswith(("modify", "cancel", "return", "exchange"))]
                orders = {a["arguments"]["order_id"] for a in actions if "order_id" in a["arguments"]}
                if (
                    not orders
                    or not orders <= db["orders"].keys()
                    or criteria["nl_assertions"]
                    or criteria["communicate_info"]
                    or set(criteria["reward_basis"]) not in ({"DB", "NL_ASSERTION"}, {"DB"})
                    or not any(
                        a["name"] == operation and (family != "refund" or "payment_method_id" in a["arguments"])
                        for a in writes
                    )
                ):
                    continue
                owners = {db["orders"][i]["user_id"] for i in orders}
                if len(owners) == 1:
                    candidates.append((len(writes), int(tid), owners.pop()))
            count = 0
            for _, index, owner in sorted(candidates):
                if owner in seen:
                    continue
                seen.add(owner)
                tid = str(index)
                selected.append(
                    dict(
                        task=tid,
                        split=split,
                        family=family,
                        customer=digest(owner.encode()),
                        task_sha256=digest(canonical(tasks[tid])),
                    )
                )
                count += 1
                if count == 2:
                    break
            if count != 2:
                raise ContractError("Cannot fill both distinct customer slots")
        rounds[cohort] = selected
    return rounds


def prepare(workspace):
    cfg = read(ROOT / PROFILE)
    if cfg["status"] != "ready" or cfg["benchmark_revision"] != PIN or cfg["domain"] != "retail":
        raise ContractError("The pinned retail protocol is not ready")
    checkout = verify_checkout(workspace / "data/tau2-bench")
    for name, sha in cfg["assets"].items():
        if digest((checkout / name).read_bytes()) != sha:
            raise ContractError(f"Native asset changed: {name}")
    folder = checkout / "data/tau2/domains/retail"
    tasks = {t["id"]: t for t in read(folder / "tasks.json")}
    rounds = select_cohorts(tasks, read(folder / "split_tasks.json"), read(folder / "db.json"))
    for cohort, rows in rounds.items():
        for family in FAMILIES:
            if [r["task"] for r in rows if r["family"] == family] != cfg["rounds"][cohort][family]:
                raise ContractError("Cohort differs from its prespecified selection")
    count = sum(map(len, rounds.values()))
    if (
        sum(Decimal(str(g["max_usd"])) for g in cfg["guards"].values()) * count
        != Decimal(str(cfg["total_usd_limit"]))
        or cfg["forms"] != list(FORMS)
        or cfg["controls"] != ["no_memory", "empty_preload"]
        or cfg["conditions"] != ["original", "recheck"]
        or cfg["delivery"] != "preload"
    ):
        raise ContractError("Study budgets, conditions or cohorts differ")
    base = read_json(asset_text(ROOT, cfg["base_design"]).encode())
    conn = read_json(asset_text(ROOT, base["model"]["connection_profile_ref"]).encode())
    validate_profile(conn)
    if (conn["worker"]["model_id"], conn["user_simulator"]["model_id"]) != (cfg["worker"], cfg["simulator"]):
        raise ContractError("Model roles changed")
    endpoints = endpoint_terms(read_json(asset_text(ROOT, cfg["endpoints"]).encode())["models"])
    return cfg, base, conn, endpoints, rounds, {r["task"]: tasks[r["task"]] for rs in rounds.values() for r in rs}


def design_for(cfg, base, cohort, row, stage, source):
    tid = row["task"]
    common = asset_text(ROOT, base["writing"]["common_instruction_ref"]).rstrip("\r\n")
    writing = deepcopy(base["writing"])
    lines = cfg.get("writing_lines") or {
        f"{form}_{variant}": base["writing"]["lines"][form] + ("\n" + cfg["suffix"] if variant == "recheck" else "")
        for form in cfg["forms"]
        for variant in cfg["conditions"]
    }
    writing["lines"] = lines
    conditions = [
        dict(id=k, writer_instruction_sha256=digest((common + "\n" + line).encode())) for k, line in lines.items()
    ]
    phase = "write" if stage == "write" else "solve"
    seed = cfg["solver_seeds"][int(stage[-1]) - 1] if stage.startswith("solve") else cfg["source_seed"]
    arms = ["no_memory"] if stage == "source" else list(lines)
    if stage.startswith("solve"):
        arms += cfg["controls"]
    arms.sort(key=lambda arm: digest(canonical([cfg["name"], cohort, tid, stage, arm])))
    jobs = [dict(index=i, phase=phase, repetition=1, task_id=tid, arm=arm) for i, arm in enumerate(arms, 1)]
    entry = dict(task_id=tid, source_group=row["customer"])
    if source:
        entry.update(
            run_id=source.source_run_id,
            source_log_sha256=source.source_log_sha256,
            public_context_sha256=source.public_context_sha256,
            public_context_bytes=source.public_context_bytes,
        )
    model = deepcopy(base["model"])
    model["seed_pairs"] = [dict(repetition=1, writer=cfg["writer_seed"], native_and_solver=seed)]
    design = dict(
        format=RETAIL_RECHECK,
        stage=stage,
        round=cohort,
        status="ready",
        conditions=conditions,
        writing=writing,
        model=model,
        second_pass=deepcopy(base["second_pass"]),
        source=dict(
            domain="retail", split=row["split"], benchmark_revision=PIN, feedback_level="none", cohort=[entry]
        ),
        allocation=dict(
            blocks=[
                dict(
                    task_id=tid,
                    repetition=1,
                    write=arms if phase == "write" else [],
                    solve=arms if phase == "solve" else [],
                )
            ],
            jobs_sha256=digest(canonical(jobs)),
        ),
    )
    design["second_pass"].update(primary_arms=list(lines), matched_controls=cfg["controls"], diagnostic_task_ids=[])
    design["second_pass"].pop("diagnostic_arms", None)
    return design, jobs


def source_from(folder, task):
    _, ctl, rows = sealed_rows(folder)
    (row,) = rows
    res = row.get("result", {})
    if (row["arm"], row["task_id"], ctl["design"]["stage"]) != ("no_memory", task, "source"):
        raise ContractError("Expected the matching first-pass source")
    if res.get("grading_status") != "graded":
        return None
    with _source(safe_path(folder, row["directory"]), res["log_sha256"]) as (scan, blobs, _, _):
        run = row["run_id"]
        start, end, grade = _closed_run(scan, run)
        req, reply = _pair(scan, run)
        context = _project_context(
            blobs.get(req["payload"]["request_ref"]), blobs.get(reply["payload"]["response_ref"])
        )
        if (
            start["record_kind"] != "observed"
            or start["condition_id"] != "no_memory"
            or start["payload"]["bank_id"]
            or start["payload"]["source_run_ids"]
            or not start["payload"]["fresh_session"]
            or context["task_context"] != blobs.get(start["payload"]["task_ref"])
            or grade["payload"]["official_success"] is not res["official_success"]
        ):
            raise ContractError("First-pass public projection or outcome differs")
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
            res["official_success"],
        )


def run_stage(args):
    workspace, root, cfg, base, conn, cohort, row, task, stage, identity, models = args
    parent = root / cohort / ("task-" + row["task"])
    parent.mkdir(parents=True, exist_ok=True)
    dest = parent / stage
    source = None if stage == "source" else source_from(parent / "source", row["task"])
    design, jobs = design_for(cfg, base, cohort, row, stage, source)
    profile = dict(
        name=cfg["name"],
        round=cohort,
        stage=stage,
        task=row["task"],
        config_sha256=digest((ROOT / PROFILE).read_bytes()),
        guards=cfg["guards"][stage],
        http_timeout_seconds=cfg["http_timeout_seconds"],
        implementation_commit=identity["implementation_commit"],
    )
    if dest.exists():
        _, ctl, prior = sealed_rows(dest)
        if (
            ctl["profile"] != profile
            or ctl["design"] != design
            or ctl["implementation"] != identity
            or endpoint_terms(ctl["model_catalog_entries"]) != endpoint_terms(models)
            or len(prior) != len(jobs)
            or any(any(r[k] != v for k, v in j.items()) for r, j in zip(prior, jobs, strict=True))
        ):
            raise ContractError("Previously sealed stage differs from its allocation")
        return
    sources = {row["task"]: source} if source else {}
    instruction = asset_text(ROOT, base["second_pass"]["solve_instruction_ref"])
    plan = dict(
        jobs=jobs,
        sources=sources,
        connection=conn,
        tasks={row["task"]: task},
        instruction=instruction,
        summary=dict(jobs_sha256=design["allocation"]["jobs_sha256"]),
    )
    imported = None
    if stage == "write":
        if source:
            plan["summary"]["wire_sizes"] = preflight_sizes(
                jobs,
                design,
                sources,
                plan["tasks"],
                conn,
                instruction,
                asset_text(ROOT, base["writing"]["common_instruction_ref"]),
            )
        else:
            plan["missing_sources"] = [row["task"]]
    else:
        plan["modules"] = native_modules(verify_checkout(workspace / "data/tau2-bench"))
        from loguru import logger

        logger.disable("tau2")
        if stage.startswith("solve"):
            _, _, writer_rows = sealed_rows(parent / "write")
            imported = parent / "write", {(r["repetition"], r["task_id"], r["arm"]): r for r in writer_rows}
    guard = BatchGuard(**cfg["guards"][stage])
    reserve = CostReservations(dest, cfg["unpriced_http"], guard, models)
    execute_prepared(
        profile, ROOT, dest, identity, design, plan, models, guard, imported_writers=imported, close_cost=reserve
    )


def writer_texts(root, cfg, cohort, rows):
    packet, index = {}, {}
    for t in rows:
        folder = root / cohort / ("task-" + t["task"]) / "write"
        _, _, records = sealed_rows(folder)
        for row in records:
            key = digest(canonical([cfg["name"], "mask", row["run_id"]]))[:18]
            body = None
            if row["status"] in {"completed", "empty"}:
                res = row["result"]
                bank = read_form_bank(safe_path(folder, row["directory"]), expected_log_sha256=res["log_sha256"])
                if (bank["arm"], bank["task_id"], bank["run_id"], bank["snapshot_id"]) != (
                    row["arm"],
                    t["task"],
                    row["run_id"],
                    res["snapshot_id"],
                ):
                    raise ContractError("Writer identity or bank differs")
                body = [dict(title=i["title"], body=i["body"]) for i in bank["items"]]
            packet[key] = body
            index[t["task"], row["arm"]] = key
    if len(packet) != len(rows) * len(cfg["forms"]) * 2:
        raise ContractError("Incomplete writer packet")
    return dict(sorted(packet.items())), index


def classify(packet, index, cfg, rows, labels):
    if labels["packet_sha256"] != digest(canonical(packet)) or set(labels["quotes"]) != set(packet):
        raise ContractError("Labels do not cover the original masked packet")
    for k, quote in labels["quotes"].items():
        notes = packet[k]
        if quote is not None and (
            not isinstance(quote, str)
            or len(quote.strip()) < 8
            or not notes
            or not any(quote in n["body"] or quote in n["title"] for n in notes)
        ):
            raise ContractError("A recheck label lacks its verbatim source")
    counts = dict(gain=0, loss=0, both=0, neither=0, missing=0)
    for t in rows:
        for form in cfg["forms"]:
            a, b = (index[t["task"], form + "_" + variant] for variant in cfg["conditions"])
            if packet[a] is None or packet[b] is None:
                counts["missing"] += 1
            else:
                flags = labels["quotes"][a] is not None, labels["quotes"][b] is not None
                counts[
                    {(False, True): "gain", (True, False): "loss", (True, True): "both", (False, False): "neither"}[
                        flags
                    ]
                ] += 1
    return dict(labels=labels, counts=counts, content_changed=bool(counts["gain"] + counts["loss"]))


def preflight(cfg, base, conn, rounds, tasks, workspace):
    native_modules(verify_checkout(workspace / "data/tau2-bench"))
    from loguru import logger

    logger.disable("tau2")
    result = {}
    for cohort, rows in rounds.items():
        result[cohort] = []
        for row in rows:
            schedules = {}
            for stage in cfg["stages"]:
                design, jobs = design_for(cfg, base, cohort, row, stage, None)
                schedules[stage] = jobs
                if stage != "write":
                    run_cfg = solve_connection(conn, design, jobs[0], cfg["guards"][stage]["max_usd"])
                    orch, native = build_baseline_orchestrator(run_cfg, tasks[row["task"]])
                    if (
                        orch.environment.get_domain_name() != "retail"
                        or not model_free_basis("retail", orch.task.evaluation_criteria)
                        or native.task_split_name != row["split"]
                        or native.task_ids != [row["task"]]
                    ):
                        raise ContractError("Native model-free retail preparation disagrees with the cohort")
            result[cohort].append(dict(row, schedules=schedules, writer_instructions=design["writing"]["lines"]))
    return dict(study=cfg["name"], protocol_sha256=digest((ROOT / PROFILE).read_bytes()), rounds=result)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["preflight", "write", "packet", "label", "solve", "close"])
    p.add_argument("--round", choices=["development", "confirmation"], default="development")
    p.add_argument("--workspace", type=Path, default=ROOT)
    p.add_argument("--output", type=Path)
    p.add_argument("--labels", type=Path)
    args = p.parse_args()
    workspace = args.workspace.resolve()
    cfg, base, conn, endpoints, rounds, tasks = prepare(workspace)
    cohort = args.round
    rows = rounds[cohort]
    root = workspace / "runs" / cfg["name"]
    if args.mode == "preflight":
        data = preflight(cfg, base, conn, rounds, tasks, workspace)
        if args.output:
            fixed(args.output, data)
        print(json.dumps({c: len(rs) for c, rs in rounds.items()}))
        return
    root.mkdir(exist_ok=True)
    lock = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        folder = root / cohort
        if cohort == "confirmation":
            dev = read(root / "development" / "round-completion.json")
            gate = root / "development" / "writer-gate.json"
            if (
                dev["gate_sha256"] != digest(gate.read_bytes())
                or not read(gate)["content_changed"]
                or dev["protocol_sha256"] != digest((ROOT / PROFILE).read_bytes())
            ):
                raise ContractError("Confirmation requires the sealed development round and writing change")
        if args.mode in {"packet", "label"}:
            packet, index = writer_texts(root, cfg, cohort, rows)
            if args.mode == "packet":
                if not args.output:
                    p.error("packet requires --output")
                fixed(args.output, packet)
            else:
                if not args.labels:
                    p.error("label requires --labels")
                result = classify(packet, index, cfg, rows, read(args.labels))
                fixed(folder / "writer-gate.json", result)
                print(json.dumps(result["counts"]))
            return
        sha = digest((ROOT / PROFILE).read_bytes())
        if args.mode == "close":
            seals = {}
            for t in rows:
                for stage in cfg["stages"]:
                    sub = folder / ("task-" + t["task"]) / stage
                    sealed_rows(sub)
                    seals[str(sub.relative_to(folder))] = digest((sub / "completion.json").read_bytes())
            fixed(
                folder / "round-completion.json",
                dict(
                    protocol_sha256=sha,
                    stages=seals,
                    gate_sha256=digest((folder / "writer-gate.json").read_bytes()),
                ),
            )
            return
        stages = ["source", "write"] if args.mode == "write" else ["solve1", "solve2"]
        if args.mode == "solve":
            read(folder / "writer-gate.json")
        ident = implementation_identity(ROOT)
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise ContractError("OPENROUTER_API_KEY_MISSING")
        models = live_models(conn)
        if endpoint_terms(models) != endpoints or implementation_identity(ROOT) != ident:
            raise ContractError("Model, endpoint terms or implementation differs from freeze")
        fixed(
            root / "registration.json",
            dict(
                implementation=ident,
                protocol_sha256=sha,
                rounds=rounds,
                conditions=cfg["conditions"],
                forms=cfg["forms"],
                delivery=cfg["delivery"],
                endpoints=endpoints,
            ),
        )
        with ProcessPoolExecutor(max_workers=cfg["max_concurrent"], mp_context=get_context("spawn")) as pool:
            if args.mode == "write":
                groups = [["source"], ["write"]]
            else:
                groups = [stages]
            for group in groups:
                entries = [
                    (workspace, root, cfg, base, conn, cohort, t, tasks[t["task"]], stage, ident, models)
                    for stage in group
                    for t in rows
                ]
                list(pool.map(run_stage, entries))
        print(json.dumps(dict(round=cohort, stages=stages, customers=len(rows))))
    finally:
        os.close(lock)


if __name__ == "__main__":
    main()
