"""Repeat the sealed retail banks and collect complete two-pass measurements on new customers."""

import argparse
from collections import Counter
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.experiments.forms_solve import _prepared_for_job
from agent_memory_eval.runner.tau2_baseline import build_baseline_orchestrator

if __package__:
    from . import run_full_retail as retail
else:
    import run_full_retail as retail

PROFILE = "protocols/pilots/tau2-retail-sample-expansion-v1.json"


def verified(path, sha):
    raw = path.read_bytes()
    if digest(raw) != sha:
        raise ContractError(f"Unexpected bytes in {path.name}")
    return read_json(raw)


def select_new(tasks, split, db, previous):
    selected = []
    for tid in split:
        owners = retail.owner_ids(tasks[tid], db)
        if len(owners) == 1 and owners.isdisjoint(previous):
            selected.append({"task": tid, "customer": owners.pop(), "split": "train"})
    return selected


def imported_bank_info(parent, task, completion, cells):
    bank = parent / ("task-" + task)
    info = {}
    for stage in ("source", "write"):
        folder = bank / stage
        seal = completion["stages"][str(folder.relative_to(parent))]["seal"]
        verified(folder / "completion.json", seal)
        _, _, slots = retail.sealed_rows(folder)
        checked = []
        for slot in slots:
            original = cells[task, stage, slot["arm"]]
            if (original["run"], original["status"], original["journal_sha256"]) != (
                slot["run_id"],
                slot["status"],
                slot["result"]["log_sha256"],
            ):
                raise ContractError(
                    "Imported source or writer differs from the frozen study"
                )
            journal = folder / slot["directory"] / "log.jsonl"
            if digest(journal.read_bytes()) != original["journal_sha256"]:
                raise ContractError("Imported source or writer journal was modified")
            checked.append(
                {
                    "arm": slot["arm"],
                    "run": slot["run_id"],
                    "journal_sha256": original["journal_sha256"],
                    "snapshot": original.get("snapshot"),
                }
            )
        info[stage] = {"seal": seal, "runs": checked}
    return info


def prepare(workspace):
    spec = retail.read(ROOT / PROFILE)
    original_cfg, base, conn, rows, tasks = retail.prepare(workspace)
    parent_cfg = read_json(asset_text(ROOT, spec["parent_protocol"]).encode())
    if parent_cfg != original_cfg or spec["status"] != "ready":
        raise ContractError("Unknown retail sample expansion settings")
    parent = workspace / "runs" / parent_cfg["name"]
    completion = verified(parent / "completion.json", spec["parent_completion_sha256"])
    reg = verified(parent / "registration.json", completion["registration_sha256"])
    detail = read_json(asset_text(ROOT, spec["parent_cells"]).encode())
    past = read_json(asset_text(ROOT, spec["previous_customers"]).encode())
    if (
        detail["completion_sha256"] != spec["parent_completion_sha256"]
        or detail["rows"] != rows
    ):
        raise ContractError("Original task cohort no longer reproduces")
    known = {row["customer"] for row in rows} | {
        r["customer"] for batch in past["rounds"].values() for r in batch
    }
    cfg = dict(
        parent_cfg,
        **{k: spec[k] for k in ("name", "stages", "solver_seeds", "total_usd_limit")},
        profile_path=PROFILE,
        run_directory=spec["name"] + "-r1",
        expected_endpoint_terms=reg["endpoint_terms"],
    )
    cfg["guards"] = {
        stage: parent_cfg["guards"]["solve1" if stage.startswith("solve") else stage]
        for stage in cfg["stages"]
    }
    cells = {(c["task"], c["stage"], c["arm"]): c for c in detail["cells"]}
    for row in rows:
        row.update(
            cohort="repeat_test",
            source_study=parent_cfg["name"],
            scheduled_stages=spec["repeat_stages"],
            imported=imported_bank_info(parent, row["task"], completion, cells),
        )
    data = workspace / "data/tau2-bench/data/tau2/domains/retail"
    raw = {t["id"]: t for t in retail.read(data / "tasks.json")}
    fresh = select_new(
        raw,
        retail.read(data / "split_tasks.json")["train"],
        retail.read(data / "db.json"),
        known,
    )
    if [r["task"] for r in fresh] != spec["new_tasks"] or len(
        {r["customer"] for r in fresh}
    ) != spec["new_customers"]:
        raise ContractError("New-customer selection differs from the registered cohort")
    for row in fresh:
        row.update(cohort="new_train", scheduled_stages=spec["stages"])
        tasks[row["task"]] = retail.bind_task(cfg, base, conn, row, raw[row["task"]])
    rows.extend(fresh)
    return cfg, base, conn, rows, tasks


def stopped_registration(root):
    reg = retail.read(root / "registration.json")
    wave = retail.read(root / "source-wave-1.json")
    if (
        set(wave["tasks"]) != {p.name[5:] for p in root.glob("task-*")}
        or (root / "completion.json").exists()
    ):
        raise ContractError(
            "Interrupted launch contains an unexpected task or completion"
        )
    hashes = {
        str(p.relative_to(root)): digest(p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "writer.lock"
    }
    slots = []
    for tid in wave["tasks"]:
        stage = root / ("task-" + tid) / "source"
        (slot,) = retail.read(stage / "allocations.json")
        journal = stage / slot["directory"] / "log.jsonl"
        if (
            slot["status"] != "interrupted"
            or "result" in slot
            or journal.read_bytes()
            or (stage / "completion.json").exists()
        ):
            raise ContractError("The initial launch may have made a model request")
        slots.append({"task": tid, "run": slot["run_id"]})
    return {
        "initial_directory": root.name,
        "implementation": reg["implementation"]["implementation_commit"],
        "reason": "full retail grading previously required test split at the native allocation check, before run.started or model requests",
        "allocations": slots,
        "http": 0,
        "reported_usd": "0",
        "hashes": hashes,
    }


def check_runtime(workspace, cfg, base, conn, rows, tasks):
    example = next(row for row in rows if "source_study" in row)
    path = retail.bank_parent(workspace, cfg, example) / "source"
    _, control, _ = retail.sealed_rows(path)
    modules = retail.modules_for(workspace)
    count = 0
    for row in rows:
        stages = ["source", "solve1"] if row["split"] == "train" else ["solve2"]
        for stage in stages:
            design, jobs = retail.full_design(cfg, base, row, stage, None)
            sample = next(j for j in jobs if j["arm"] == "no_memory")
            settings = retail.solve_connection(
                conn, design, sample, cfg["total_usd_limit"]
            )
            orch, native = build_baseline_orchestrator(settings, tasks[row["task"]])
            _prepared_for_job(
                settings,
                (orch, native, control["model_catalog_entries"], modules),
                design["second_pass"]["proposed_limits"],
                row["task"],
                row["split"],
                "retail",
                True,
            )
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["preflight", "run"])
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    cfg, base, conn, rows, tasks = prepare(workspace)
    record = retail.preflight(cfg, base, rows)
    stopped = workspace / "runs" / cfg["name"]
    if stopped.exists() and stopped_registration(stopped) != retail.read(
        ROOT / "artifacts/analyses/20261119-retail-expansion-prewire.json"
    ):
        raise ContractError(
            "Initial no-request launch differs from its reconciled record"
        )
    checks = check_runtime(workspace, cfg, base, conn, rows, tasks)
    print(f"{checks} native task/seed/split bindings verified", flush=True)
    counts = Counter(
        j["phase"]
        for r in record["rows"]
        for stage in r["stages"].values()
        for j in stage
    )
    print(
        f"{len(rows)} tasks, {len({r['customer'] for r in rows})} customers, {sum(counts.values())} fresh allocations",
        flush=True,
    )
    if args.mode == "preflight":
        if args.output:
            retail.fixed(args.output, record)
    else:
        frozen = (
            ROOT / "artifacts/analyses/20261119-retail-sample-expansion-preflight.json"
        )
        if retail.read(frozen) != record:
            raise ContractError(
                "Live allocations differ from the precommitted expansion"
            )
        retail.run_study(workspace, cfg, base, conn, rows, tasks, record)


if __name__ == "__main__":
    main()
