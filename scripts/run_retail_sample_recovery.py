"""Continue the frozen retail expansion after the host restart, using untouched slots."""

import argparse
import fcntl
import os
from collections import Counter, defaultdict
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.models.openrouter import request_reservation_usd
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path

if __package__:
    from . import run_retail_sample_supplement as supplement
else:
    import run_retail_sample_supplement as supplement

retail = supplement.retail
check = supplement.check
verified = supplement.expansion.verified
PROFILE = "protocols/pilots/tau2-retail-sample-recovery-v1.json"
RECEIPT = "artifacts/analyses/20261120-retail-sample-recovery-receipt.json"
ALLOCATION = "artifacts/analyses/20261120-retail-sample-recovery-preflight.json"


def recorded_cost(slot):
    return sum(
        (
            Decimal(role["reported_usd"])
            for role in slot["result"]["billing_by_role"].values()
        ),
        Decimal(0),
    )


def unresolved(job, slot, models, open_sha):
    """Accept only one identifiable uncertain worker request in an otherwise priced run."""
    is_open = slot["status"] == "started"
    expected = open_sha if is_open else slot["result"]["log_sha256"]
    check(
        digest((job / "log.jsonl").read_bytes()) == expected,
        "Uncertain journal changed",
    )
    scan, _, _ = read_experiment(job)
    events = list(scan["events"].values())
    check(
        set(scan["starts"]) == {slot["run_id"]}
        and all(
            e["run_id"] == slot["run_id"]
            for e in events
            if e["type"].startswith("model.")
        ),
        "Mixed run in journal",
    )
    starts = {
        e["payload"]["request_id"]: e["payload"]
        for e in events
        if e["type"] == "model.request_started"
    }
    ends = {
        e["payload"]["request_id"]: e["payload"]
        for e in events
        if e["type"] == "model.request_finished"
    }
    check(ends.keys() <= starts.keys(), "Orphan request finish")
    uncertain = [
        rid
        for rid in starts
        if rid not in ends or ends[rid]["cost_status"] == "unknown"
    ]
    check(len(uncertain) == 1, "Expected one uncertain request")
    rid = uncertain[0]
    end = ends.get(rid)
    if is_open:
        check(
            end is None
            and events[-1]["payload"].get("request_id") == rid
            and len(starts) == 30
            and len(ends) == 29
            and not any(e["type"] == "run.ended" for e in events),
            "Unexpected open request",
        )
    else:
        check(
            slot["result"]["error_code"] == "TRANSPORT_UNKNOWN"
            and slot["result"]["official_success"] is None
            and len(ends) == len(starts)
            and end["status"] == "error"
            and end["delivery_status"] == "unknown"
            and end["response_ref"] is None,
            "Unrecognized transport failure",
        )
    blobs = BlobStore(job)
    known = Decimal(0)
    for request_id, reply in ends.items():
        if request_id == rid:
            continue
        check(
            reply["status"] == "response" and reply["cost_status"] == "observed",
            "Additional unpriced or exceptional request",
        )
        raw = blobs.get(reply["response_ref"])
        amount = Decimal(str(reply["cost_usd"]))
        check(
            raw.get("id") and Decimal(str(raw["usage"]["cost"])) == amount,
            "Reported cost differs from provider response",
        )
        known += amount
    start = starts[rid]
    wire = blobs.get(start["request_ref"])
    model = models["worker"]
    check(
        start["actor_role"] == "worker"
        and wire["model"] == model["id"]
        and wire["stream"] is False
        and wire["provider"]
        == {
            "only": [model["selected_endpoint"]["tag"]],
            "require_parameters": True,
            "allow_fallbacks": False,
        },
        "Unrecognized worker route",
    )
    if not is_open:
        check(known == recorded_cost(slot), "Uncertain job's priced responses differ")
    return {
        "run": slot["run_id"],
        "request": rid,
        "actor": "worker",
        "task": slot["task_id"],
        "arm": slot["arm"],
        "status": "open" if is_open else "transport_unknown",
        "journal_sha256": expected,
        "wire_sha256": start["request_ref"]["sha256"],
        "reported_usd": str(known),
        "maximum_usd": str(request_reservation_usd(wire, model)),
    }


def reconcile(workspace, spec, cfg, rows, previous_record, first_receipt):
    root = workspace / "runs" / spec["previous_directory"]
    reg = verified(root / "registration.json", spec["previous_registration_sha256"])
    check(
        reg["preflight"] == previous_record
        and reg["endpoint_terms"] == cfg["expected_endpoint_terms"]
        and not (root / "completion.json").exists(),
        "Unexpected supplement registration",
    )
    registered = {r["task"]: r["stages"] for r in previous_record["rows"]}
    old_total = Decimal(first_receipt["covered_usd"])
    observed = Decimal(first_receipt["reported_usd"])
    prior_max = Decimal(first_receipt["receipt_maxima_usd"])
    missing, uncertain, old_stages = [], [], {}
    dispositions = Counter()
    seen_runs = set()
    seen_waves = set()
    passed_last_wave = False
    for stage in cfg["stages"]:
        ordered = retail.stage_order(cfg, rows, stage)
        width = cfg["concurrency_by_stage"].get(stage, cfg["max_concurrent"])
        for start in range(0, len(ordered), width):
            batch = ordered[start : start + width]
            wave_path = root / f"{stage}-wave-{start // width + 1}.json"
            if wave_path.exists():
                check(
                    not passed_last_wave,
                    "Unexpected wave following missing or open wave",
                )
                seen_waves.add(wave_path)
                wave = retail.read(wave_path)
                grant = Decimal(wave["per_shard_grant"])
                check(
                    wave["tasks"] == [r["task"] for r in batch]
                    and Decimal(wave["prior_covered"]) == old_total
                    and old_total + grant * len(batch)
                    <= Decimal(str(cfg["total_usd_limit"])),
                    "Original wave or grant changed",
                )
            else:
                passed_last_wave = True
            for row in batch:
                tid = row["task"]
                name = f"task-{tid}/{stage}"
                folder = root / name
                jobs = registered[tid][stage]
                if not wave_path.exists():
                    check(
                        not folder.exists(),
                        "Unregistered execution in an unstarted wave",
                    )
                    missing.extend(
                        {
                            "task": tid,
                            "stage": stage,
                            "index": j["index"],
                            "arm": j["arm"],
                            "previous_run": None,
                        }
                        for j in jobs
                    )
                    continue
                if name == spec["open_stage"]:
                    check(
                        not (folder / "completion.json").exists(),
                        "Open shard is now sealed",
                    )
                    ctl = verified(folder / "control.json", spec["open_control_sha256"])
                    slots = verified(
                        folder / "allocations.json", spec["open_allocations_sha256"]
                    )
                    check(
                        [s["status"] for s in slots]
                        == ["completed"] * 4 + ["started", "planned"],
                        "Open shard no longer matches the checkpoint",
                    )
                    entry = {
                        "spent": str(grant),
                        "control_sha256": spec["open_control_sha256"],
                        "allocation_sha256": spec["open_allocations_sha256"],
                        "unsealed": True,
                    }
                    passed_last_wave = True
                else:
                    _, ctl, slots = retail.sealed_rows(folder)
                    entry = retail.accounting(folder, grant)
                check(
                    ctl["implementation"] == reg["implementation"]
                    and ctl["profile"]["config_sha256"]
                    == previous_record["protocol_sha256"]
                    and Decimal(str(ctl["profile"]["guards"]["max_usd"])) == grant
                    and retail.endpoint_terms(ctl["model_catalog_entries"])
                    == {
                        k: v for k, v in reg["endpoint_terms"].items() if k != "grader"
                    },
                    "Frozen shard settings differ",
                )
                old_stages[name] = entry
                old_total += Decimal(entry["spent"])
                check(len(slots) == len(jobs), "Missing previous allocation")
                started_dirs = set()
                for slot, job in zip(slots, jobs, strict=True):
                    check(
                        all(slot[k] == v for k, v in job.items())
                        and slot["directory"] == f"jobs/{job['index']:04d}"
                        and slot["run_id"] not in seen_runs,
                        "Changed allocation or duplicate run",
                    )
                    seen_runs.add(slot["run_id"])
                    path = safe_path(folder, slot["directory"])
                    status = slot["status"]
                    dispositions[stage, status] += 1
                    if status in {"not_started", "planned"}:
                        check(
                            "result" not in slot and not path.exists(),
                            "Untouched slot has execution evidence",
                        )
                        missing.append(
                            {
                                "task": tid,
                                "stage": stage,
                                "index": job["index"],
                                "arm": job["arm"],
                                "previous_run": slot["run_id"],
                            }
                        )
                        continue
                    started_dirs.add(path)
                    if status == "started" or slot["result"].get("unknown_cost"):
                        item = unresolved(
                            path,
                            slot,
                            ctl["model_catalog_entries"],
                            spec["open_journal_sha256"],
                        )
                        uncertain.append(item)
                        observed += Decimal(item["reported_usd"])
                    else:
                        check(
                            status in {"completed", "error"}
                            and slot["result"]["run_id"] == slot["run_id"]
                            and digest((path / "log.jsonl").read_bytes())
                            == slot["result"]["log_sha256"],
                            "Previous result or journal changed",
                        )
                        observed += recorded_cost(slot)
                check(
                    set((folder / "jobs").iterdir()) == started_dirs,
                    "Unexpected old job directory",
                )
    check(
        set(root.glob("task-*/*")) == {root / name for name in old_stages}
        and set(root.glob("*-wave-*.json")) == seen_waves,
        "Unexpected previous shard or wave",
    )
    maxima = sum(Decimal(x["maximum_usd"]) for x in uncertain)
    check(
        len(old_stages) == 66
        and len(uncertain) == 10
        and len({x["request"] for x in uncertain}) == 10
        and Counter(x["stage"] for x in missing) == spec["remaining_allocations"]
        and len({(x["task"], x["stage"], x["arm"]) for x in missing}) == len(missing)
        and observed == Decimal(spec["previous_observed_usd"])
        and maxima == Decimal(spec["ten_worker_request_maxima_usd"])
        and prior_max == Decimal(spec["first_wave_503_maxima_usd"]),
        "Recovery differs from approved checkpoint",
    )
    return {
        "previous_registration_sha256": spec["previous_registration_sha256"],
        "initial_receipt_sha256": digest((ROOT / supplement.RECEIPT).read_bytes()),
        "stages": old_stages,
        "previous_full_grant_usd": str(old_total),
        "reported_usd": str(observed),
        "initial_503_maxima_usd": str(prior_max),
        "uncertain": uncertain,
        "worker_maxima_usd": str(maxima),
        "covered_usd": str(observed + prior_max + maxima),
        "untouched": missing,
        "dispositions": {f"{s}/{k}": v for (s, k), v in sorted(dispositions.items())},
    }


def prepare(workspace):
    spec = retail.read(ROOT / PROFILE)
    check(
        spec["status"] == "ready"
        and spec["previous_profile_sha256"]
        == digest((ROOT / supplement.PROFILE).read_bytes()),
        "Previous supplement profile changed",
    )
    cfg, base, conn, rows, tasks, previous, first = supplement.prepare(workspace)
    check(
        previous == retail.read(ROOT / supplement.ALLOCATION)
        and first == retail.read(ROOT / supplement.RECEIPT)
        and cfg["run_directory"] == spec["previous_directory"]
        and cfg["total_usd_limit"] == spec["total_usd_limit"],
        "Different starting budget, sources or schedule",
    )
    receipt = reconcile(workspace, spec, cfg, rows, previous, first)
    cfg.update(
        profile_path=PROFILE,
        run_directory=spec["directory"],
        prior_covered_usd=receipt["covered_usd"],
        stages=list(spec["remaining_allocations"]),
        concurrency_by_stage={},
    )
    cfg["episode_admission"]["codes"] = [
        *cfg["episode_admission"]["codes"],
        "TRANSPORT_UNKNOWN",
    ]
    selected = defaultdict(lambda: defaultdict(list))
    for x in receipt["untouched"]:
        selected[x["task"]][x["stage"]].append(x["index"])
    for row in rows:
        row["selected_slots"] = dict(selected[row["task"]])
        row["scheduled_stages"] = [
            s for s in cfg["stages"] if s in row["selected_slots"]
        ]
        row.pop("unstarted_lineage", None)
    record = retail.preflight(cfg, base, rows)
    targets = {
        (x["task"], x["stage"], x["index"], x["arm"]) for x in receipt["untouched"]
    }
    actual = {
        (r["task"], stage, j["index"], j["arm"])
        for r in record["rows"]
        for stage, jobs in r["stages"].items()
        for j in jobs
    }
    check(
        targets == actual and len(targets) == 471,
        "New allocation does not equal untouched set",
    )
    return cfg, base, conn, rows, tasks, record, receipt


@contextmanager
def previous_controller_lock(workspace):
    previous = retail.read(ROOT / PROFILE)["previous_directory"]
    fd = os.open(workspace / "runs" / previous, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    with previous_controller_lock(workspace):
        cfg, base, conn, rows, tasks, record, receipt = prepare(workspace)
        if args.mode == "preflight":
            retail.fixed(ROOT / RECEIPT, receipt)
            retail.fixed(ROOT / ALLOCATION, record)
        else:
            check(
                receipt == retail.read(ROOT / RECEIPT)
                and record == retail.read(ROOT / ALLOCATION),
                "Frozen recovery inputs changed",
            )
            retail.run_study(workspace, cfg, base, conn, rows, tasks, record)
    print(
        f"471 untouched slots, prior coverage USD {receipt['covered_usd']}", flush=True
    )


if __name__ == "__main__":
    main()
