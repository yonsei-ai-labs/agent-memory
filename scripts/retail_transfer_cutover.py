"""Close a drained original retail wave and transfer the untouched remainder to a separately funded ledger."""

import argparse
import fcntl
import os
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from scripts import run_retail_transfer_revision as study
from scripts.analyze_full_retail import require

AMENDMENT = "protocols/pilots/tau2-retail-transfer-revision-pending-maxima-v1.json"
RECEIPT = "deferred-to-supplement.json"


def inventory(root, cfg, pre, registration):
    cap = Decimal(registration["preflight"]["authorized_usd"])
    waves, stages, deferred = {}, {}, {}
    total = Decimal(0)
    for stage in cfg["stages"]:
        ordered = pre["stage_order"][stage]
        width = cfg["concurrency_by_stage"][stage]
        for begin in range(0, len(ordered), width):
            tasks = ordered[begin : begin + width]
            filename = f"{stage}-wave-{1 + begin // width}.json"
            path = root / filename
            if path.exists():
                wave = study.retail.read(path)
                grant = Decimal(wave["per_shard_grant"])
                require(
                    not deferred
                    and wave["tasks"] == tasks
                    and Decimal(wave["prior_covered"]) == total
                    and grant
                    == ((cap - total) / len(tasks)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN),
                    "Original wave order or grant differs",
                )
                waves[filename] = digest(path.read_bytes())
            else:
                grant = None
            for tid in tasks:
                key = f"task-{tid}/{stage}"
                folder = root / key
                if grant is None:
                    require(
                        stage in study.SOLVES and not folder.exists(), "Deferred position already has a shard"
                    )
                    jobs = pre["targets"][tid]["jobs"][stage]
                    deferred[key] = {"positions": len(jobs), "jobs_sha256": digest(canonical(jobs))}
                    stages[key] = {
                        "status": "not_started",
                        "reason": "deferred_to_supplement",
                        "reported": "0",
                        "held": "0",
                        "spent": "0",
                        "http": 0,
                    }
                elif grant == 0:
                    require(not folder.exists(), "Unfunded shard exists")
                    stages[key] = {
                        "status": "not_started",
                        "reason": "spend_limit",
                        "reported": "0",
                        "held": "0",
                        "spent": "0",
                        "http": 0,
                    }
                else:
                    _, ctl, slots = study.retail.sealed_rows(folder)
                    require(
                        ctl["profile"]["name"] == cfg["name"]
                        and ctl["profile"]["task"] == tid
                        and ctl["profile"]["stage"] == stage
                        and Decimal(str(ctl["profile"]["guards"]["max_usd"])) == grant
                        and ctl["implementation"] == registration["implementation"]
                        and all(s["status"] not in {"started", "planned"} for s in slots),
                        "An original shard is still open or misbound",
                    )
                    stages[key] = study.retail.accounting(folder, wave["per_shard_grant"])
                total += Decimal(stages[key]["spent"])
    require(
        waves.keys() == {p.name for p in root.glob("*-wave-*.json")} and deferred and total <= cap,
        "Unexpected wave or empty handoff",
    )
    receipt = {
        "registration_sha256": digest((root / "registration.json").read_bytes()),
        "pending_protocol_sha256": digest((Path(__file__).resolve().parents[1] / AMENDMENT).read_bytes()),
        "wave_sha256": waves,
        "deferred": deferred,
        "funded_stage_seals": {k: v["seal"] for k, v in stages.items() if "seal" in v},
        "total_covered_usd": str(total),
    }
    return stages, receipt


def verify(root, cfg, pre, registration, end):
    if "supplement_cutover_sha256" not in end:
        require(not end.get("deferred_to_supplement"), "Missing cutover receipt")
        return
    stages, expected = inventory(root, cfg, pre, registration)
    path = root / RECEIPT
    require(
        study.retail.read(path) == expected
        and digest(path.read_bytes()) == end["supplement_cutover_sha256"]
        and end["registration_sha256"] == expected["registration_sha256"]
        and end["total_covered_usd"] == expected["total_covered_usd"]
        and end["stages"] == stages
        and end["deferred_to_supplement"] == sorted(expected["deferred"]),
        "Cutover differs from funded grants, sealed attempts or untouched schedules",
    )


def close(workspace):
    cfg, _, _, _, _, pre = study.prepare(workspace)
    root = workspace / "runs" / cfg["name"]
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reg = study.retail.read(root / "registration.json")
        require(
            reg["preflight"] == dict(pre, authorized_usd=cfg["total_usd_limit"])
            and pre == study.retail.read(ROOT / study.PREFLIGHT)
            and (root / "pacing-activation.json").exists(),
            "Wrong original study or approval",
        )
        stages, receipt = inventory(root, cfg, pre, reg)
        end_path = root / "completion.json"
        if end_path.exists():
            verify(root, cfg, pre, reg, study.retail.read(end_path))
            return
        study.retail.fixed(root / RECEIPT, receipt)
        end = {
            "registration_sha256": receipt["registration_sha256"],
            "stages": stages,
            "total_covered_usd": receipt["total_covered_usd"],
            "deferred_to_supplement": sorted(receipt["deferred"]),
            "supplement_cutover_sha256": digest((root / RECEIPT).read_bytes()),
        }
        study.retail.fixed(end_path, end)
        print(
            sum(d["positions"] for d in receipt["deferred"].values()),
            "positions deferred before allocation",
            receipt["total_covered_usd"],
            "USD retained in original ledger",
        )
    finally:
        os.close(fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    close(parser.parse_args().workspace.resolve())
