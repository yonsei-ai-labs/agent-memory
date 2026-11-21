"""Repeat the retail transfer with paired fresh writers and counterbalanced preload notices."""

import argparse
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.memory.context import past_memory_instruction
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path
from scripts import run_retail_related_transfer as transfer
from scripts.analyze_full_retail import require
from scripts.run_retail_related_transfer import retail

PROFILE = "protocols/pilots/tau2-retail-transfer-revision-v1.json"
PREFLIGHT = "artifacts/analyses/20261121-retail-transfer-revision-preflight.json"
SOLVES = ("solve1", "solve2")


def assign_notices(name, targets, versions):
    order = sorted(targets, key=lambda row: digest(canonical([name, "notice-order", row["task"]])))
    for i, row in enumerate(order):
        row["notice_by_stage"] = dict(zip(SOLVES, versions if i % 2 == 0 else versions[::-1], strict=True))
        row["scheduled_stages"] = list(SOLVES)


def prepare(workspace):
    spec = retail.read(ROOT / PROFILE)
    original = read_json(asset_text(ROOT, spec["previous_preflight"]).encode())
    previous = read_json(asset_text(ROOT, spec["previous_protocol"]).encode())
    asset_text(ROOT, spec["previous_audit"])
    cfg, base, conn, targets, tasks, checked = transfer.prepare(workspace)
    require(
        checked == original
        and original["protocol_sha256"] == spec["previous_protocol"]["sha256"]
        and previous["forms"] == [f + "_original" for f in spec["forms"]]
        and spec["variants"] == ["original", "explicit"]
        and spec["stages"] == ["write", *SOLVES]
        and spec["notices"] == ["legacy-id-v1", "domain-auth-v2"]
        and spec["controls"] == previous["controls"]
        and spec["status"] == "ready",
        "Retest differs from its pinned cohort or conditions",
    )
    raw = retail.read(workspace / "data/tau2-bench/data/tau2/domains/retail/tasks.json")
    source_tasks = {t["id"]: t for t in raw if t["id"] in cfg["donors"]}
    require(not source_tasks.keys() & tasks.keys(), "Source and target tasks overlap")
    tasks.update(source_tasks)
    lines = {
        f"{form}_{variant}": base["writing"]["lines"][form]
        + ("\n" + spec["explicit_suffix"] if variant == "explicit" else "")
        for form in spec["forms"]
        for variant in spec["variants"]
    }
    cfg.update(
        name=spec["name"],
        run_directory=spec["name"],
        profile_path=PROFILE,
        stages=spec["stages"],
        guards=spec["guards"],
        max_concurrent=spec["max_concurrent"],
        concurrency_by_stage=spec["concurrency_by_stage"],
        forms=spec["forms"],
        conditions=spec["variants"],
        writing_lines=lines,
        controls=spec["controls"],
        transfer_spec={"forms": list(lines)},
        total_usd_limit=spec["total_usd_limit"],
    )
    # Evaluation banks must come from this study's writing phase.
    cfg["donors"] = {tid: dict(d, banks={}) for tid, d in original["donors"].items()}
    common = asset_text(ROOT, base["writing"]["common_instruction_ref"]).rstrip("\r\n")
    instruction = asset_text(ROOT, base["second_pass"]["solve_instruction_ref"])
    sources, writer_plans = [], {}
    for tid, donor in cfg["donors"].items():
        path = Path(donor["source_folder"])
        row = {
            "task": tid,
            "customer": donor["source_group"],
            "split": donor["split"],
            "source_study": path.parts[1],
            "cohort": "revision_sources",
            "scheduled_stages": ["write"],
        }
        folder = retail.bank_parent(workspace, cfg, row) / "source"
        require(folder == safe_path(workspace, str(path)), "Writer source path differs")
        source = retail.recorded_source(folder, tid)
        require(
            source
            and (source.source_run_id, source.source_log_sha256, source.public_context_sha256)
            == (donor["run_id"], donor["journal_sha256"], donor["public_context_sha256"]),
            "Wrong original experience",
        )
        design, jobs = retail.full_design(cfg, base, row, "write", source)
        sizes = retail.preflight_sizes(jobs, design, {tid: source}, tasks, conn, instruction, common)
        writer_plans[tid] = {
            "source_run": source.source_run_id,
            "public_sha256": source.public_context_sha256,
            "source_journal_sha256": source.source_log_sha256,
            "jobs": jobs,
            "sizes": sizes,
        }
        sources.append(row)
    assign_notices(cfg["name"], targets, spec["notices"])
    rows = sources + targets
    target_plans = {
        r["task"]: {
            "customer": r["customer"],
            "task_sha256": r["task_sha256"],
            "pairs": r["pairs"],
            "notices": r["notice_by_stage"],
            "jobs": {s: transfer.schedule(cfg, r, s) for s in SOLVES},
        }
        for r in targets
    }
    record = {
        "study": cfg["name"],
        "protocol_sha256": digest((ROOT / PROFILE).read_bytes()),
        "previous_preflight_sha256": spec["previous_preflight"]["sha256"],
        "benchmark_revision": cfg["benchmark_revision"],
        "instruction_sha256": digest(instruction.encode()),
        "writer_instruction_sha256": {
            a: digest((common + "\n" + line).encode()) for a, line in lines.items()
        },
        "notice_instruction_sha256": {
            v: digest(past_memory_instruction(instruction, notice_version=v).encode())
            for v in spec["notices"]
        },
        "writers": writer_plans,
        "targets": target_plans,
        "stage_order": {s: [r["task"] for r in retail.stage_order(cfg, rows, s)] for s in cfg["stages"]},
        "writer_allocations": sum(len(p["jobs"]) for p in writer_plans.values()),
        "solve_allocations": sum(len(j) for p in target_plans.values() for j in p["jobs"].values()),
    }
    require(
        (len(sources), len(targets), record["writer_allocations"], record["solve_allocations"])
        == (22, 24, 176, 608),
        "Unexpected retest allocation",
    )
    return cfg, base, conn, rows, tasks, record


def new_banks(workspace, root, cfg, source_id):
    """Locate only sealed fresh writes; a failed writer cannot silently supply an empty control."""
    folder = root / ("task-" + source_id) / "write"
    if not folder.exists():
        waves = [retail.read(path) for path in root.glob("write-wave-*.json")]
        grants = [Decimal(w["per_shard_grant"]) for w in waves if source_id in w["tasks"]]
        require(grants == [0], "Missing a funded writer shard")
        return {arm: {"status": "not_started"} for arm in cfg["writing_lines"]}
    _, ctl, slots = retail.sealed_rows(folder)
    require(
        ctl["profile"]["name"] == cfg["name"]
        and ctl["profile"]["stage"] == "write"
        and ctl["profile"]["config_sha256"] == digest((ROOT / PROFILE).read_bytes())
        and len(slots) == len(cfg["writing_lines"])
        and {s["arm"] for s in slots} == set(cfg["writing_lines"]),
        "Wrong fresh writer shard",
    )
    banks = {}
    for slot in slots:
        arm = slot["arm"]
        require(
            (slot["phase"], slot["task_id"], slot["repetition"]) == ("write", source_id, 1),
            "Wrong writer identity",
        )
        desc = {"status": slot["status"]}
        if slot["status"] in ("completed", "empty"):
            res = slot["result"]
            journal = safe_path(folder, slot["directory"]) / "log.jsonl"
            require(
                res["run_id"] == slot["run_id"]
                and res["arm"] == arm
                and res["status"] == slot["status"]
                and res["task_id"] == source_id,
                "Writer result is misbound",
            )
            desc.update(
                journal=str(journal.relative_to(workspace)),
                journal_sha256=res["log_sha256"],
                run_id=res["run_id"],
                snapshot_id=res["snapshot_id"],
                public_context_sha256=cfg["donors"][source_id]["public_context_sha256"],
            )
        else:
            require(slot["status"] in ("error", "not_started"), "Unfinished writer shard")
        banks[arm] = desc
    return banks


def revision_stage(args):
    workspace, root, cfg, base, conn, row, task, stage, identity, models, judge, grant = args
    if stage == "write":
        return retail.stage_job(args)
    donors = {
        sid: dict(cfg["donors"][sid], banks=new_banks(workspace, root, cfg, sid)) for sid in row["pairs"]
    }
    return transfer.transfer_stage(
        (
            workspace,
            root,
            dict(cfg, donors=donors),
            base,
            conn,
            row,
            task,
            stage,
            identity,
            models,
            judge,
            grant,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-usd", type=Decimal)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    cfg, base, conn, rows, tasks, record = prepare(workspace)
    if args.mode == "preflight":
        if args.output:
            retail.fixed(args.output, record)
        print(record["writer_allocations"], "writes,", record["solve_allocations"], "native solves")
        return
    cap = Decimal(cfg["total_usd_limit"])
    if args.max_usd is None or not args.max_usd.is_finite() or not 0 < args.max_usd <= cap:
        raise ContractError(f"The run requires a positive approved total at or below {cap} USD")
    require(record == retail.read(ROOT / PREFLIGHT), "Commit the verified allocation before live calls")
    cfg["total_usd_limit"] = str(args.max_usd)
    retail.run_study(
        workspace,
        cfg,
        base,
        conn,
        rows,
        tasks,
        dict(record, authorized_usd=str(args.max_usd)),
        stage_runner=revision_stage,
    )


if __name__ == "__main__":
    main()
