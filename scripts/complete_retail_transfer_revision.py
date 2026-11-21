"""Try only untouched assignments after the frozen retail retest, under its remaining aggregate budget."""

import argparse
from decimal import Decimal
from pathlib import Path
from urllib.request import build_opener, install_opener

from agent_memory_eval.contracts.core import digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.journal import safe_path
from scripts import pace_retail_transfer_revision as pacing
from scripts import run_retail_transfer_revision as study
from scripts.analyze_full_retail import require
from scripts.audit_retail_revision_reservations import cost_basis

PROFILE = "protocols/pilots/tau2-retail-transfer-revision-pending-v1.json"
AMENDMENT = "protocols/pilots/tau2-retail-transfer-revision-pending-maxima-v1.json"
PREFLIGHT = "artifacts/analyses/20261121-retail-transfer-revision-pending-preflight.json"


def untouched_jobs(jobs, slots, folder):
    require(len(jobs) == len(slots), "Incomplete original allocation list")
    selected = []
    for job, slot in zip(jobs, slots, strict=True):
        require(
            all(slot[k] == v for k, v in job.items())
            and job.get("source_task_id") == slot.get("source_task_id")
            and slot["directory"] == f"jobs/{job['index']:04d}",
            "Original job differs from registration",
        )
        path = safe_path(folder, slot["directory"])
        if slot["status"] == "not_started":
            require("result" not in slot and not path.exists(), "Untouched assignment has a journal")
            selected.append({"job": job, "original_run": slot["run_id"], "reason": slot["reason"]})
        else:
            require(
                "result" in slot
                and slot["status"] in {"completed", "error", "interrupted", "incomplete"}
                and path.is_dir(),
                "Original assignment has not finished",
            )
    return selected


def prepare(workspace):
    cfg, base, conn, all_rows, tasks, original = study.prepare(workspace)
    root = workspace / "runs" / cfg["name"]
    end = study.retail.read(root / "completion.json")
    reg = study.transfer.expansion.verified(root / "registration.json", end["registration_sha256"])
    cap = reg["preflight"]["authorized_usd"]
    covered = sum((Decimal(s["spent"]) for s in end["stages"].values()), Decimal(0))
    require(
        original == study.retail.read(ROOT / study.PREFLIGHT)
        and reg["preflight"] == dict(original, authorized_usd=cap)
        and covered == Decimal(end["total_covered_usd"]) <= Decimal(cap) <= Decimal(cfg["total_usd_limit"]),
        "Parent cohort or aggregate spending differs",
    )
    source_root = Path(__file__).resolve().parents[1]
    amendment = study.retail.read(source_root / AMENDMENT)
    require(
        amendment["previous_protocol"]
        == {"path": PROFILE, "sha256": digest((source_root / PROFILE).read_bytes())},
        "Request-maxima amendment does not refer to the original pending rules",
    )
    receipt = cost_basis(root, end, reg, cfg["unpriced_http"])
    covered = Decimal(receipt["covered"])
    require(
        0 <= covered <= Decimal(end["total_covered_usd"]), "Reconciled parent cost exceeds the sealed total"
    )
    pending, rows = {}, []
    for row in (r for r in all_rows if "pairs" in r):
        tid = row["task"]
        stages = {}
        for stage in study.SOLVES:
            jobs = original["targets"][tid]["jobs"][stage]
            key = f"task-{tid}/{stage}"
            seal = end["stages"][key]
            folder = root / key
            if seal.get("status") == "not_started":
                require(
                    not folder.exists() and Decimal(seal["spent"]) == 0, "Unfunded shard contains attempts"
                )
                entries = [{"job": j, "original_run": None, "reason": seal["reason"]} for j in jobs]
            else:
                _, ctl, slots = study.retail.sealed_rows(folder)
                require(
                    study.retail.matches_accounting_seal(folder, ctl["profile"]["guards"]["max_usd"], seal),
                    "Original funding or shard seal differs",
                )
                entries = untouched_jobs(jobs, slots, folder)
            if entries:
                pending[key] = entries
                stages[stage] = [e["job"] for e in entries]
        if stages:
            rows.append(dict(row, scheduled_stages=list(stages), unstarted_jobs=stages))
    for tid, donor in cfg["donors"].items():
        donor["banks"] = study.new_banks(workspace, root, cfg, tid)
        require(
            all(b["status"] in {"completed", "empty"} for b in donor["banks"].values()),
            "An original writer is unavailable",
        )
    # Every pending episode can terminate in at most one qualifying unpriced request.
    maximum = max(
        [cfg["unpriced_http"]["maximum_per_stage"], *(len(entries) for entries in pending.values())]
    )
    cfg["unpriced_http"] = dict(cfg["unpriced_http"], maximum_per_stage=maximum)
    cfg.update(
        run_directory=cfg["name"] + "-pending",
        stages=list(study.SOLVES),
        total_usd_limit=cap,
        prior_covered_usd=str(covered),
        max_concurrent=pacing.LIMITS["max_workers"],
        concurrency_by_stage=dict.fromkeys(study.SOLVES, pacing.LIMITS["max_workers"]),
        expected_endpoint_terms=reg["endpoint_terms"],
    )
    record = dict(
        original,
        authorized_usd=cap,
        pending_protocol_sha256=digest((source_root / AMENDMENT).read_bytes()),
        parent_cost_reconciliation=receipt,
        unpriced_receipts_by_shard=maximum,
        parent_registration_sha256=end["registration_sha256"],
        parent_completion_sha256=digest((root / "completion.json").read_bytes()),
        prior_covered_usd=str(covered),
        remaining_usd=str(Decimal(cap) - covered),
        unstarted=pending,
        pending_stage_order={
            s: [r["task"] for r in study.retail.stage_order(cfg, rows, s)] for s in study.SOLVES
        },
        pacing=pacing.LIMITS,
    )
    return cfg, base, conn, rows, tasks, record


def pending_stage(args):
    _, root, _, _, _, _, _, _, _, models, _, _ = args
    handler = pacing.SimulatorPacingHandler(models["user_simulator"]["id"], pacing.SharedPacer(root))
    install_opener(build_opener(handler))
    return study.transfer.transfer_stage(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-usd", type=Decimal)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    cfg, base, conn, rows, tasks, record = prepare(workspace)
    count = sum(len(v) for v in record["unstarted"].values())
    print(
        f"{count} untouched consultations; USD {record['remaining_usd']} available after checking original request maxima",
        flush=True,
    )
    if args.mode == "preflight":
        if args.output:
            study.retail.fixed(args.output, record)
        return
    require(
        count
        and args.max_usd == Decimal(record["authorized_usd"])
        and record == study.retail.read(ROOT / PREFLIGHT),
        "Freeze the untouched assignments and preserve the original aggregate cap before dispatch",
    )
    study.retail.run_study(workspace, cfg, base, conn, rows, tasks, record, stage_runner=pending_stage)


if __name__ == "__main__":
    main()
