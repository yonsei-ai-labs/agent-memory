"""Transfer original retail writer snapshots to previously unrun, related native tasks."""

import argparse
from collections import Counter
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.episode_admission import EpisodeAdmission
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path
from scripts import run_full_retail as retail
from scripts import run_retail_sample_expansion as expansion
from scripts.analyze_recheck_rounds import MUTATIONS, inspect_episode, mutation_key
from scripts.replay_full_retail_fields import reference_state, state_hashes

PROFILE = "protocols/pilots/tau2-retail-related-transfer-v1.json"
PREFLIGHT = "artifacts/analyses/20261121-retail-related-transfer-preflight.json"


def select_pairs(raw, db, source_ids, eligible_ids):
    """Select by owner and mutated order, before inspecting any writer or solve."""
    attributes = {}
    for tid in set(source_ids) | set(eligible_ids):
        actions = [a for a in raw[tid]["evaluation_criteria"]["actions"] or [] if a["name"] in MUTATIONS]
        attributes[tid] = (
            retail.owner_ids(raw[tid], db),
            {a["arguments"]["order_id"] for a in actions if "order_id" in a["arguments"]},
            Counter(mutation_key(a["name"], a["arguments"]) for a in actions),
        )
    rows = []
    for target in sorted(set(eligible_ids) - set(source_ids), key=int):
        owners, orders, required = attributes[target]
        if len(owners) != 1 or not orders:
            continue
        pairs = {}
        for source in sorted(source_ids, key=int):
            previous_owners, previous_orders, previous = attributes[source]
            shared = orders & previous_orders
            if owners != previous_owners or not shared:
                continue
            overlap = previous & required
            pairs[source] = {
                "stratum": "same" if previous == required else "partial" if overlap else "changed",
                "shared_orders": sorted(shared),
                "shared_mutations": overlap.total(),
                "source_only_mutations": (previous - required).total(),
                "target_only_mutations": (required - previous).total(),
            }
        if pairs:
            rows.append({"task": target, "customer": next(iter(owners)), "split": "train", "pairs": pairs})
    return rows


def schedule(cfg, row, stage):
    slots = [(s, f) for s in row["pairs"] for f in cfg["transfer_spec"]["forms"]]
    slots.extend((None, control) for control in cfg["controls"])
    slots.sort(key=lambda slot: digest(canonical([cfg["name"], row["task"], stage, *slot])))
    jobs = []
    for index, (source, arm) in enumerate(slots, 1):
        job = {"index": index, "phase": "solve", "repetition": 1, "task_id": row["task"], "arm": arm}
        if source is not None:
            job["source_task_id"] = source
        jobs.append(job)
    return jobs


def design_for(cfg, base, row, stage):
    design, _ = retail.full_design(cfg, base, row, stage, None)
    donors = [cfg["donors"][tid] for tid in row["pairs"]]
    splits = sorted({d["split"] for d in donors})
    design["source"].update(
        split=splits[0] if len(splits) == 1 else "base",
        cohort=[
            {k: d[k] for k in ("task_id", "split", "source_group", "run_id", "public_context_sha256")}
            for d in donors
        ],
    )
    design["target"] = {"task_id": row["task"], "split": row["split"], "domain": "retail"}
    if "notice_by_stage" in row:
        design["preload_notice_version"] = row["notice_by_stage"][stage]
    jobs = schedule(cfg, row, stage)
    if "unstarted_jobs" in row:
        permitted = row["unstarted_jobs"][stage]
        indices = {job["index"] for job in permitted}
        jobs = [job for job in jobs if job["index"] in indices]
        if not jobs or jobs != permitted:
            raise ContractError("Pending jobs differ from the original ordered allocation")
    design["allocation"] = {"jobs": jobs, "jobs_sha256": digest(canonical(jobs))}
    return design, jobs


def check_donor(workspace, src, source_split, writers, catalog):
    tid = src["task"]
    source_dir = str(Path(src["journal"]).parents[2])
    source = retail.recorded_source(safe_path(workspace, source_dir), tid)
    if not source or (source.source_run_id, source.source_log_sha256) != (src["run"], src["journal_sha256"]):
        raise ContractError("Source episode does not match the registered original")
    banks = {}
    for form in catalog:
        cell = writers[tid, form]
        bank = read_form_bank(
            safe_path(workspace, cell["journal"]).parent, expected_log_sha256=cell["journal_sha256"]
        )
        entry = catalog[form][tid]
        if (
            any(
                bank[k] != entry[k]
                for k in ("task_id", "run_id", "snapshot_id", "public_context_sha256", "status")
            )
            or bank["arm"] != form
            or bank["repetition"] != 1
            or entry["journal_sha256"] != cell["journal_sha256"]
            or bank["public_context_sha256"] != source.public_context_sha256
            or cell["source_run"] != src["run"]
            or cell["customer"] != src["customer"]
            or cell["snapshot"] != bank["snapshot_id"]
            or cell["notes"] != [{k: i[k] for k in ("title", "body")} for i in bank["items"]]
        ):
            raise ContractError("Writer version, original customer or source experience differs")
        banks[form] = dict(
            journal=cell["journal"],
            **entry,
            items_sha256=digest(canonical(bank["items"])),
            item_count=len(bank["items"]),
        )
    return {
        "task_id": tid,
        "split": source_split,
        "source_group": src["customer"],
        "run_id": src["run"],
        "journal_sha256": src["journal_sha256"],
        "source_folder": source_dir,
        "public_context_sha256": source.public_context_sha256,
        "banks": banks,
    }


def replay_reference_databases(workspace, cfg, base, conn, rows, tasks, sources):
    """Apply the source's official and historical actions in the pristine target retail environment."""
    from tau2.data_model.simulation import SimulationRun
    from tau2.multilingual.runtime import language_environment_kwargs, load_run_language
    from tau2.registry import registry
    from tau2.runner.presets import reload_recorded_call

    make = registry.get_env_constructor("retail")
    design, jobs = design_for(cfg, base, rows[0], cfg["stages"][0])
    native = retail.native_config(retail.solve_connection(conn, design, jobs[0], 1))
    options = language_environment_kwargs(load_run_language(native)) or {}
    reference, observed = {}, {}
    target_ids = {r["task"] for r in rows}
    for tid in sorted(set(cfg["donors"]) | target_ids, key=int):
        task = tasks[tid]
        if task.get("initial_state") is not None:
            raise ContractError("The same-order comparison requires the same native initial state")
        state, errors = reference_state(make(**options), task["evaluation_criteria"]["actions"])
        reference[tid] = {"end_db": list(state), "errors": errors}
        if tid in target_ids:
            continue
        src = sources[tid]
        report = expansion.verified(safe_path(workspace, src["report"]), src["report_sha256"])
        sim = SimulationRun.model_validate(report)
        if (
            sim.task_id != tid
            or (language_environment_kwargs(reload_recorded_call(sim.language_context, "retail")) or {})
            != options
        ):
            raise ContractError("Source and target language or database binding differs")
        env = make(**options)
        env.set_state(
            initialization_data=None, initialization_actions=None, message_history=sim.messages, strict=True
        )
        end = state_hashes(env)
        if (end == state) != src["grade"]["db_match"]:
            raise ContractError("Historical source end state does not reproduce its official DB grade")
        writes = inspect_episode(report, task["evaluation_criteria"])["writes"]
        observed[tid] = {
            "end_db": list(end),
            "executed": [{"name": w["name"], "arguments": w["args"]} for w in writes if w["success"]],
        }
    for row in rows:
        target = reference[row["task"]]["end_db"]
        for source, pair in row["pairs"].items():
            pair.update(
                source_reference_db_matches=reference[source]["end_db"] == target,
                source_observed_db_matches=observed[source]["end_db"] == target,
            )
    return {"reference": reference, "observed": observed}


def prepare(workspace):
    spec = retail.read(ROOT / PROFILE)
    previous = read_json(asset_text(ROOT, spec["source_protocol"]).encode())
    catalog = read_json(asset_text(ROOT, spec["source_catalog"]).encode())
    cfg, base, conn, cohort, tasks = expansion.prepare(workspace)
    if (
        spec["status"] != "ready"
        or spec["format"] != "agent-memory-retail-related-transfer/v1"
        or spec["forms"] != previous["forms"]
        or spec["controls"] != ["empty_preload", "no_memory"]
        or spec["stages"] != ["solve1", "solve2"]
        or spec["solver_seeds"] != [12, 12]
        or catalog["protocol_sha256"] != spec["source_protocol"]["sha256"]
    ):
        raise ContractError("Transfer design differs from source or repeat settings")
    root = workspace / "data/tau2-bench/data/tau2/domains/retail"
    raw = {t["id"]: t for t in retail.read(root / "tasks.json")}
    previous_rounds = read_json(asset_text(ROOT, cfg["prior_protocol"]).encode())["rounds"]
    old = {
        tid for rnd in previous_rounds.values() for key, ids in rnd.items() if key != "split" for tid in ids
    }
    splits = {r["task"]: r["split"] for r in cohort}
    if set(splits) != {r["task"] for r in catalog["tasks"]}:
        raise ContractError("Frozen retail source cohort differs")
    untouched = set(retail.read(root / "split_tasks.json")["train"]) - old - splits.keys()
    rows = select_pairs(raw, retail.read(root / "db.json"), splits, untouched)
    cells = [
        c for ref in previous["sources"] for c in read_json(asset_text(ROOT, ref).encode())[ref["field"]]
    ]
    src = {c["task"]: c for c in cells if c["stage"] == "source"}
    writers = {(c["task"], c["arm"]): c for c in cells if c["stage"] == "write"}
    expected = {f: {e["task_id"]: e for e in entries} for f, entries in catalog["source_writers"].items()}
    ids = sorted({sid for row in rows for sid in row["pairs"]}, key=int)
    donors = {tid: check_donor(workspace, src[tid], splits[tid], writers, expected) for tid in ids}
    cfg.update(
        name=spec["name"],
        run_directory=spec["name"],
        profile_path=PROFILE,
        transfer_spec=spec,
        controls=spec["controls"],
        stages=spec["stages"],
        solver_seeds=spec["solver_seeds"],
        max_concurrent=spec["max_concurrent"],
        donors=donors,
        record_unfunded_shards=True,
        episode_admission=previous["episode_admission"],
        unpriced_http=previous["unpriced_http"],
    )
    cfg["guards"] = {stage: spec["per_target_round"] for stage in cfg["stages"]}
    target_tasks = {}
    for row in rows:
        tid = row["task"]
        if any(donors[s]["source_group"] != row["customer"] for s in row["pairs"]):
            raise ContractError("Transferred source belongs to a different retail customer")
        target_tasks[tid] = retail.bind_task(cfg, base, conn, row, raw[tid])
    checks = replay_reference_databases(workspace, cfg, base, conn, rows, tasks | target_tasks, src)
    cfg["total_usd_limit"] = None
    record = {
        "study": cfg["name"],
        "protocol_sha256": digest((ROOT / PROFILE).read_bytes()),
        "instruction_sha256": base["second_pass"]["solve_instruction_ref"]["sha256"],
        "benchmark_revision": cfg["benchmark_revision"],
        "eligible_train_tasks": sorted(untouched, key=int),
        "donors": donors,
        "database_replay": checks,
        "targets": [
            dict(row, stages={stage: schedule(cfg, row, stage) for stage in cfg["stages"]}) for row in rows
        ],
        "stage_order": {s: [r["task"] for r in retail.stage_order(cfg, rows, s)] for s in cfg["stages"]},
    }
    record["allocations"] = sum(len(jobs) for r in record["targets"] for jobs in r["stages"].values())
    return cfg, base, conn, rows, target_tasks, record


def transfer_stage(args):
    workspace, root, cfg, base, conn, row, task, stage, identity, models, judge, grant = args
    tid = row["task"]
    design, jobs = design_for(cfg, base, row, stage)
    dest = root / ("task-" + tid) / stage
    dest.parent.mkdir(exist_ok=True)
    profile = {
        "name": cfg["name"],
        "stage": stage,
        "task": tid,
        "config_sha256": digest((ROOT / cfg.get("profile_path", PROFILE)).read_bytes()),
        "implementation_commit": identity["implementation_commit"],
        "http_timeout_seconds": cfg["http_timeout_seconds"],
        "guards": dict(cfg["guards"][stage], max_usd=float(grant)),
    }
    if dest.exists():
        _, ctl, slots = retail.sealed_rows(dest)
        if (
            ctl["profile"] != profile
            or ctl["design"] != design
            or ctl["implementation"] != identity
            or retail.endpoint_terms(ctl["model_catalog_entries"]) != retail.endpoint_terms(models)
            or len(slots) != len(jobs)
            or any(any(slot[k] != v for k, v in j.items()) for slot, j in zip(slots, jobs, strict=True))
        ):
            raise ContractError("Sealed transfer shard differs from its registered assignment")
        return
    sources, writers = {}, {}
    for sid in row["pairs"]:
        donor = cfg["donors"][sid]
        source = retail.recorded_source(safe_path(workspace, donor["source_folder"]), sid)
        if not source or (source.source_run_id, source.public_context_sha256, source.source_log_sha256) != (
            donor["run_id"],
            donor["public_context_sha256"],
            donor["journal_sha256"],
        ):
            raise ContractError("Donor original experience differs from preflight")
        sources[sid] = source
        for arm, b in donor["banks"].items():
            if b["status"] not in ("completed", "empty"):
                writers[1, sid, arm] = {"status": b["status"]}
                continue
            writers[1, sid, arm] = {
                "directory": str(Path(b["journal"]).parent),
                "status": b["status"],
                "result": {"log_sha256": b["journal_sha256"]},
            }
    plan = {
        "jobs": jobs,
        "sources": sources,
        "connection": conn,
        "tasks": {tid: task},
        "modules": retail.modules_for(workspace),
        "instruction": asset_text(ROOT, base["second_pass"]["solve_instruction_ref"]),
        "grader": (dict(cfg["judge"], timeout_seconds=cfg["http_timeout_seconds"]), judge),
        "admission": EpisodeAdmission(root, cfg["episode_admission"], dest),
        "summary": {"jobs_sha256": design["allocation"]["jobs_sha256"]},
    }
    guard = retail.BatchGuard(**profile["guards"])
    reserve = retail.CostReservations(dest, cfg["unpriced_http"], guard, dict(models, grader=judge))
    retail.execute_prepared(
        profile,
        ROOT,
        dest,
        identity,
        design,
        plan,
        models,
        guard,
        imported_writers=(workspace, writers),
        close_cost=reserve,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--max-usd", type=Decimal, help="Authorized aggregate ceiling for this transfer study"
    )
    args = parser.parse_args()
    if args.mode == "run" and (args.max_usd is None or not args.max_usd.is_finite() or args.max_usd <= 0):
        parser.error("run requires a finite positive --max-usd")
    workspace = args.workspace.resolve()
    cfg, base, conn, rows, tasks, record = prepare(workspace)
    print(
        f"{len(rows)} targets, {sum(len(r['pairs']) for r in rows)} source-target pairs, "
        f"{record['allocations']} full native episodes",
        flush=True,
    )
    if args.mode == "preflight":
        if args.output:
            retail.fixed(args.output, record)
        return
    if record != retail.read(ROOT / PREFLIGHT):
        raise ContractError("Live transfer requires the committed source/target/condition assignment")
    cfg["total_usd_limit"] = str(args.max_usd)
    retail.run_study(
        workspace,
        cfg,
        base,
        conn,
        rows,
        tasks,
        dict(record, authorized_usd=str(args.max_usd)),
        stage_runner=transfer_stage,
    )


if __name__ == "__main__":
    main()
