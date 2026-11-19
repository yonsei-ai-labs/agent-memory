"""Reconcile every received 503 at its maximum price and resume only untouched retail allocations."""

import argparse
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.models.openrouter import request_reservation_usd
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path

if __package__:
    from . import run_retail_sample_expansion as expansion
else:
    import run_retail_sample_expansion as expansion

retail = expansion.retail
PROFILE = "protocols/pilots/tau2-retail-sample-supplement-v1.json"
RECEIPT = "artifacts/analyses/20261119-retail-sample-interrupted.json"
ALLOCATION = "artifacts/analyses/20261119-retail-sample-supplement-preflight.json"


def check(truth, explanation):
    if not truth:
        raise ContractError(explanation)


def reconcile_error(directory, row, models):
    result = row["result"]
    journal = directory / "log.jsonl"
    check(
        digest(journal.read_bytes()) == result["log_sha256"],
        "Error journal differs from sealed allocation",
    )
    scan, _, _ = read_experiment(directory)
    blobs = BlobStore(directory)
    events = list(scan["events"].values())
    unknown = [
        e["payload"]
        for e in events
        if e["type"] == "model.request_finished"
        and e["payload"]["cost_status"] == "unknown"
    ]
    check(len(unknown) == 1, "Unreconciled costs beyond one terminating request")
    end = unknown[0]
    starts = [
        e["payload"]
        for e in events
        if e["type"] == "model.request_started"
        and e["payload"]["request_id"] == end["request_id"]
    ]
    (start,) = starts
    raw = blobs.get(end["response_ref"])
    check(
        (
            end["status"],
            end["delivery_status"],
            start["actor_role"],
            result["error_code"],
        )
        == ("error", "response_received", "user_simulator", "HTTP_503")
        and raw.get("error", {}).get("code") == 503
        and not raw.get("id")
        and result["official_success"] is None,
        "Unsupported original receipt",
    )
    wire = blobs.get(start["request_ref"])
    maximum = request_reservation_usd(wire, models[start["actor_role"]])
    original = result.get("unpriced_reservation")
    if original:
        check(
            original["request_id"] == start["request_id"]
            and Decimal(original["usd"]) == maximum,
            "Original per-call reservation mismatch",
        )
    return {
        "task": row["task_id"],
        "arm": row["arm"],
        "run": row["run_id"],
        "request": start["request_id"],
        "actor": start["actor_role"],
        "request_sha256": start["request_ref"]["sha256"],
        "response_sha256": end["response_ref"]["sha256"],
        "error": raw["error"],
        "maximum_usd": str(maximum),
        "originally_reserved_per_request": bool(original),
    }


def interrupted(workspace, spec, cfg, base, rows):
    root = workspace / "runs" / spec["original_directory"]
    reg = expansion.verified(
        root / "registration.json", spec["original_registration_sha256"]
    )
    check(
        reg["preflight"] == retail.preflight(cfg, base, rows)
        and not (root / "completion.json").exists(),
        "Unexpected initial registration or completed run",
    )
    total = Decimal(0)
    reported = Decimal(0)
    errors = []
    stages = {}
    pending = {}
    attempted = set()
    seen = set()
    for stage in ("source", "write", "solve1"):
        ordered = retail.stage_order(cfg, rows, stage)
        width = cfg["max_concurrent"]
        for i in range(0, len(ordered), width):
            batch = ordered[i : i + width]
            wave = retail.read(root / f"{stage}-wave-{i // width + 1}.json")
            grant = Decimal(wave["per_shard_grant"])
            check(
                wave["tasks"] == [r["task"] for r in batch]
                and Decimal(wave["prior_covered"]) == total
                and total + grant * len(batch) <= Decimal(str(cfg["total_usd_limit"])),
                "Initial grant schedule mismatch",
            )
            for row in batch:
                tid = row["task"]
                name = f"task-{tid}/{stage}"
                folder = root / name
                _, ctl, slots = retail.sealed_rows(folder)
                entry = retail.accounting(folder, grant)
                check(
                    Decimal(str(ctl["profile"]["guards"]["max_usd"])) == grant,
                    "Initial grant changed",
                )
                jobs = retail.full_design(cfg, base, row, stage, None)[1]
                check(len(slots) == len(jobs), "Initial allocation count mismatch")
                for old, job in zip(slots, jobs, strict=True):
                    check(
                        all(old[k] == v for k, v in job.items())
                        and old["run_id"] not in seen,
                        "Initial allocation identity changed",
                    )
                    seen.add(old["run_id"])
                    j = safe_path(folder, old["directory"])
                    if old["status"] == "not_started":
                        check(
                            stage == "solve1"
                            and old["reason"] == "unknown_cost"
                            and not j.exists()
                            and "result" not in old,
                            "Scheduled tail may already have received a request",
                        )
                        pending.setdefault(tid, []).append(
                            {
                                "index": old["index"],
                                "arm": old["arm"],
                                "unstarted_run": old["run_id"],
                            }
                        )
                    else:
                        attempted.add(old["run_id"])
                        if old["result"].get("unknown_cost"):
                            errors.append(
                                reconcile_error(j, old, ctl["model_catalog_entries"])
                            )
                stages[name] = entry
                total += Decimal(entry["spent"])
                reported += Decimal(entry["reported"])
    check(
        len(errors) == 24
        and len([e for e in errors if not e["originally_reserved_per_request"]]) == 6
        and sum(map(len, pending.values())) == 12
        and len(stages) == 78
        and len(attempted) == 274,
        "Unexpected incomplete cohort",
    )
    check(
        not list(root.glob("task-*/solve[23]"))
        and not list(root.glob("solve[23]-wave-*.json")),
        "Later repeats already started",
    )
    maximum = sum(Decimal(e["maximum_usd"]) for e in errors)
    return {
        "study": cfg["name"],
        "registration_sha256": spec["original_registration_sha256"],
        "stages": stages,
        "prior_shard_cover_usd": str(total),
        "reported_usd": str(reported),
        "receipt_maxima_usd": str(maximum),
        "covered_usd": str(reported + maximum),
        "receipts": errors,
        "unstarted": pending,
    }


def prepare(workspace):
    spec = retail.read(ROOT / PROFILE)
    cfg, base, conn, rows, tasks = expansion.prepare(workspace)
    check(
        digest((ROOT / expansion.PROFILE).read_bytes())
        == spec["expansion_protocol_sha256"],
        "Expansion protocol changed",
    )
    receipt = interrupted(workspace, spec, cfg, base, rows)
    cfg = deepcopy(cfg)
    cfg.update(
        profile_path=PROFILE,
        run_directory=spec["directory"],
        stages=spec["stages"],
        prior_covered_usd=receipt["covered_usd"],
        concurrency_by_stage=spec["concurrency_by_stage"],
        episode_admission=spec["episode_admission"],
    )
    check(cfg["total_usd_limit"] == spec["total_usd_limit"], "Cumulative cap changed")
    cfg["unpriced_http"]["maximum_per_stage"] = spec["maximum_unpriced_per_shard"]
    for row in rows:
        if row["cohort"] == "new_train":
            tid = row["task"]
            row["source_study"] = spec["original_directory"]
            row["scheduled_stages"] = ["solve2", "solve3"]
            if tid in receipt["unstarted"]:
                slots = receipt["unstarted"][tid]
                row["scheduled_stages"] = ["solve1", "solve2", "solve3"]
                row["selected_slots"] = {"solve1": [s["index"] for s in slots]}
                row["unstarted_lineage"] = slots
    record = retail.preflight(cfg, base, rows)
    check(
        sum(len(s) for row in record["rows"] for s in row["stages"].values()) == 804,
        "Follow-on must contain exactly the 804 untouched slots",
    )
    return cfg, base, conn, rows, tasks, record, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["preflight", "run"])
    parser.add_argument("--workspace", type=Path, default=ROOT)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    cfg, base, conn, rows, tasks, record, receipt = prepare(workspace)
    if args.mode == "preflight":
        retail.fixed(ROOT / RECEIPT, receipt)
        retail.fixed(ROOT / ALLOCATION, record)
    else:
        check(
            retail.read(ROOT / RECEIPT) == receipt
            and retail.read(ROOT / ALLOCATION) == record,
            "Follow-on receipt or allocation changed",
        )
        retail.run_study(workspace, cfg, base, conn, rows, tasks, record)
    print(
        "804 untouched allocations, original conservative coverage USD",
        receipt["covered_usd"],
        flush=True,
    )


if __name__ == "__main__":
    main()
