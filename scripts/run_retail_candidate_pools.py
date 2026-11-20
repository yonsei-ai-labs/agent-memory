"""Compare optional access to own and mixed frozen notes in fresh retail episodes."""

import argparse
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.episode_admission import EpisodeAdmission
from agent_memory_eval.experiments.forms_batch import (
    BatchGuard,
    endpoint_terms,
    execute_prepared,
)
from agent_memory_eval.experiments.forms_solve import resolve_retrieval_pool
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.experiments.fresh_forms import CostReservations
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path

if __package__:
    from . import run_full_retail as retail
    from . import run_retail_sample_expansion as expansion
else:
    import run_full_retail as retail
    import run_retail_sample_expansion as expansion


PROFILE = "protocols/pilots/tau2-retail-candidate-pools-v1.json"
PREFLIGHT = "artifacts/analyses/20261120-retail-candidate-pools-preflight.json"


def bank_inputs(cfg, tid):
    pools, deliveries = {}, {}
    for form in cfg["pool_spec"]["forms"]:
        all_sources = cfg["banks"][form]
        own = [b for b in all_sources if b["task_id"] == tid]
        for mode in cfg["pool_spec"]["pool_modes"]:
            arm = f"{form}__{mode}"
            sources = own if mode == "own" else all_sources
            pools[arm] = sources
            deliveries[arm] = {
                "writer": form,
                "read_mode": "on_demand",
                "pool": mode,
                "sources_sha256": digest(canonical(sources)),
            }
    return pools, deliveries


def design_for(cfg, base, row, source=None):
    tid = row["task"]
    design, _ = retail.full_design(cfg, base, row, "solve1", source)
    pools, delivery = bank_inputs(cfg, tid)
    arms = sorted(
        [*delivery, "no_memory"], key=lambda arm: digest(canonical([cfg["name"], "arm", tid, arm]))
    )
    jobs = [
        {"index": i, "phase": "solve", "repetition": 1, "task_id": tid, "arm": arm}
        for i, arm in enumerate(arms, 1)
    ]
    design["stage"] = "retrieval"
    design["delivery_arms"] = delivery
    previous = design["second_pass"]
    design["second_pass"] = {
        "primary_arms": list(delivery),
        "matched_controls": ["no_memory"],
        "no_memory": previous["no_memory"],
        "environment": previous["environment"],
        "pool_assignment": cfg["pool_spec"]["pool_assignment"],
        "solve_instruction_ref": cfg["pool_spec"]["instruction"],
        "primary_outcome": "Official native retail reward equals 1.",
        "proposed_limits": dict(previous["proposed_limits"], **cfg["pool_spec"]["read_limits"]),
    }
    design["allocation"]["blocks"][0]["solve"] = arms
    design["allocation"]["jobs_sha256"] = digest(canonical(jobs))
    return design, jobs, pools


def prepare(workspace):
    spec = retail.read(ROOT / PROFILE)
    if (
        spec["format"] != "agent-memory-retail-candidate-pools/v1"
        or spec["pool_modes"] != ["own", "mixed"]
        or spec["forms"] != [f + "_original" for f in retail.FORMS]
        or spec["control"] != "no_memory"
        or spec["parent_profile"]["path"] != expansion.PROFILE
    ):
        raise ContractError("Unexpected retail retrieval protocol")
    asset_text(ROOT, spec["parent_profile"])
    instruction = asset_text(ROOT, spec["instruction"])
    cfg, base, conn, cohort, tasks = expansion.prepare(workspace)
    if [spec["seed"]] != cfg["solver_seeds"][:1]:
        raise ContractError("Solver and prior retail customer configuration differ")
    cells = [c for ref in spec["sources"] for c in read_json(asset_text(ROOT, ref).encode())[ref["field"]]]
    sources = {c["task"]: c for c in cells if c["stage"] == "source"}
    banks = {form: [] for form in spec["forms"]}
    rows = [{k: r[k] for k in ("task", "customer", "split", "cohort", "task_sha256")} for r in cohort]
    if len(rows) != 66 or len({r["customer"] for r in rows}) != 49 or set(sources) != set(tasks):
        raise ContractError("Frozen source/target coverage differs")
    writers = [c for c in cells if c["stage"] == "write"]
    seen_versions, seen_runs, seen_snapshots = set(), set(), set()
    for cell in writers:
        form, tid = cell["arm"], cell["task"]
        path = safe_path(workspace, cell["journal"])
        bank = read_form_bank(path.parent, expected_log_sha256=cell["journal_sha256"])
        if (
            (
                bank["run_id"],
                bank["snapshot_id"],
                bank["task_id"],
                bank["arm"],
                bank["status"],
                bank["public_context_sha256"],
                bank["repetition"],
            )
            != (cell["run"], cell["snapshot"], tid, form, cell["status"], cell["public_context_sha256"], 1)
            or cell["source_run"] != sources[tid]["run"]
            or cell["customer"] != sources[tid]["customer"]
            or bank["run_id"] in seen_runs
            or bank["snapshot_id"] in seen_snapshots
        ):
            raise ContractError("Writer descriptor differs from frozen task and journal")
        seen_runs.add(bank["run_id"])
        seen_snapshots.add(bank["snapshot_id"])
        for item in bank["items"]:
            if item["version"] in seen_versions:
                raise ContractError("Independent source writers share a version")
            seen_versions.add(item["version"])
        banks[form].append(dict(bank, journal_sha256=cell["journal_sha256"]))
    order = sorted(tasks, key=lambda t: digest(canonical([spec["name"], "pool", t])))
    for form, entries in banks.items():
        entries.sort(key=lambda b: order.index(b["task_id"]))
        if [b["task_id"] for b in entries] != order:
            raise ContractError("Incomplete or duplicated writer assignment for " + form)
    cfg.update(
        name=spec["name"],
        run_directory=spec["name"],
        profile_path=PROFILE,
        pool_spec=spec,
        total_usd_limit=None,
        record_unfunded_shards=True,
        controls=["no_memory"],
        stages=["retrieval"],
        solver_seeds=[spec["seed"]],
        max_concurrent=spec["max_concurrent"],
        banks=banks,
        episode_admission=spec["episode_admission"],
        unpriced_http=spec["unpriced_http"],
    )
    schedule = []
    for row in rows:
        tid = row["task"]
        cell = sources[tid]
        if cell["customer"] != row["customer"]:
            raise ContractError("Target customer differs from its first pass")
        row.update(
            source_folder=str(Path(cell["journal"]).parents[2]),
            source_run=cell["run"],
            source_log_sha256=cell["journal_sha256"],
        )
        design, jobs, pools = design_for(cfg, base, row)
        target_hashes = {
            b["public_context_sha256"] for pool in pools.values() for b in pool if b["task_id"] == tid
        }
        if len(target_hashes) != 1:
            raise ContractError("Writer conditions did not see the same first-pass experience")
        row["public_context_sha256"] = target_hashes.pop()
        for arm, delivery in design["delivery_arms"].items():
            items, _ = resolve_retrieval_pool(
                delivery, pools[arm], tid, 1, row["public_context_sha256"], spec["forms"]
            )
            sizes = (
                len(items),
                sum(len(canonical({k: i[k] for k in ("title", "body")})) for i in items),
                len(canonical({"items": items})),
            )
            limits = spec["read_limits"]
            if any(
                size > limits[key]
                for size, key in zip(
                    sizes,
                    ("read_bank_items", "read_bank_payload_bytes", "read_bank_return_bytes"),
                    strict=True,
                )
            ):
                raise ContractError("Registered pool exceeds the common limits")
        schedule.append(
            {
                "task": tid,
                "customer": row["customer"],
                "split": row["split"],
                "task_sha256": row["task_sha256"],
                "source_run": row["source_run"],
                "source_log_sha256": row["source_log_sha256"],
                "arms": [j["arm"] for j in jobs],
                "source_bindings": {a: d["sources_sha256"] for a, d in design["delivery_arms"].items()},
            }
        )
    record = {
        "study": cfg["name"],
        "protocol_sha256": digest((ROOT / PROFILE).read_bytes()),
        "native_revision": cfg["benchmark_revision"],
        "instruction_sha256": digest(instruction.encode()),
        "ordered_source_tasks": order,
        "tasks": schedule,
        "task_order": [r["task"] for r in retail.stage_order(cfg, rows, "retrieval")],
        "source_writers": {
            form: [
                {
                    k: b[k]
                    for k in (
                        "task_id",
                        "run_id",
                        "snapshot_id",
                        "journal_sha256",
                        "public_context_sha256",
                        "status",
                    )
                }
                for b in entries
            ]
            for form, entries in banks.items()
        },
        "mixed_items": {f: sum(len(b["items"]) for b in bs) for f, bs in banks.items()},
        "allocations": sum(len(r["arms"]) for r in schedule),
    }
    return cfg, base, conn, rows, tasks, record


def retrieval_stage(args):
    workspace, root, cfg, base, conn, row, task, stage, identity, models, judge, grant = args
    tid = row["task"]
    source = retail.recorded_source(safe_path(workspace, row["source_folder"]), tid)
    if not source or (source.source_run_id, source.source_log_sha256, source.public_context_sha256) != (
        row["source_run"],
        row["source_log_sha256"],
        row["public_context_sha256"],
    ):
        raise ContractError("Target first-pass record differs from all four original writers")
    design, jobs, pools = design_for(cfg, base, row, source)
    dest = root / ("task-" + tid) / stage
    dest.parent.mkdir(exist_ok=True)
    profile = {
        "name": cfg["name"],
        "stage": stage,
        "task": tid,
        "implementation_commit": identity["implementation_commit"],
        "config_sha256": digest((ROOT / PROFILE).read_bytes()),
        "http_timeout_seconds": cfg["http_timeout_seconds"],
        "guards": dict(cfg["pool_spec"]["shard_limits"], max_usd=float(grant)),
    }
    if dest.exists():
        _, control, slots = retail.sealed_rows(dest)
        if (
            control["implementation"] != identity
            or control["profile"] != profile
            or control["design"] != design
            or endpoint_terms(control["model_catalog_entries"]) != endpoint_terms(models)
            or len(slots) != len(jobs)
            or any(any(s[k] != v for k, v in j.items()) for s, j in zip(slots, jobs))
        ):
            raise ContractError("Existing retrieval shard differs from registration")
        return
    plan = {
        "jobs": jobs,
        "sources": {tid: source},
        "connection": conn,
        "tasks": {tid: task},
        "modules": retail.modules_for(workspace),
        "instruction": asset_text(ROOT, cfg["pool_spec"]["instruction"]),
        "retrieval_pools": pools,
        "grader": (dict(cfg["judge"], timeout_seconds=cfg["http_timeout_seconds"]), judge),
        "admission": EpisodeAdmission(root, cfg["episode_admission"], dest),
        "summary": {"jobs_sha256": design["allocation"]["jobs_sha256"]},
    }
    guard = BatchGuard(**profile["guards"])
    reserve = CostReservations(dest, cfg["unpriced_http"], guard, dict(models, grader=judge))
    execute_prepared(profile, ROOT, dest, identity, design, plan, models, guard, close_cost=reserve)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-usd", type=Decimal, help="Authorized aggregate USD ceiling for this run")
    args = parser.parse_args()
    if args.mode == "run" and (args.max_usd is None or not args.max_usd.is_finite() or args.max_usd <= 0):
        parser.error("run requires a positive finite --max-usd for this study")
    cfg, base, conn, rows, tasks, record = prepare(args.workspace.resolve())
    print(
        f"{len(rows)} tasks, {record['allocations']} fresh episodes, mixed pools {record['mixed_items']}",
        flush=True,
    )
    if args.mode == "preflight":
        if args.output:
            retail.fixed(args.output, record)
        return
    if cfg["pool_spec"]["status"] != "ready" or retail.read(ROOT / PREFLIGHT) != record:
        raise ContractError("Live retrieval requires the committed frozen allocation")
    cfg["total_usd_limit"] = str(args.max_usd)
    registration = dict(record, authorized_usd=str(args.max_usd))
    retail.run_study(
        args.workspace.resolve(), cfg, base, conn, rows, tasks, registration, stage_runner=retrieval_stage
    )


if __name__ == "__main__":
    main()
