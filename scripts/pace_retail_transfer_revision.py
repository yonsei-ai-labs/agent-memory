"""Resume a sealed wave of the frozen retail retest with paced, byte-identical simulator requests."""

import argparse
import fcntl
import json
import math
import os
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import BaseHandler, build_opener, install_opener

from agent_memory_eval.contracts.core import digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.models.openrouter import API_ROOT
from scripts import run_retail_transfer_revision as study
from scripts.analyze_full_retail import require

LIMITS = {
    "max_workers": 3,
    "initial_gap_ms": 4000,
    "min_gap_ms": 1500,
    "max_gap_ms": 10000,
    "accepted_before_speedup": 64,
}
ORIGINAL = "aedf12590b5179c0672eb1213450b9d49d16d2ca"


def retry_seconds(header, now):
    if header:
        if header.isdecimal():
            return int(header)
        try:
            at = parsedate_to_datetime(header)
            return max(0, math.ceil(at.timestamp() - now))
        except (TypeError, ValueError, OverflowError):
            pass
    return 30


class SharedPacer:
    def __init__(self, directory, now=time.time, sleep=time.sleep):
        self.directory = directory
        self.now = now
        self.sleep = sleep

    def update(self, status=None, retry_after=None):
        path = self.directory / "pacing-state.json"
        with (self.directory / "pacing.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = (
                study.retail.read(path)
                if path.exists()
                else {
                    "gap_ms": LIMITS["initial_gap_ms"],
                    "next_start": 0,
                    "due_to_limit": 0,
                    "sent": 0,
                    "responses": 0,
                    "limited": 0,
                    "accepted_streak": 0,
                }
            )
            now = self.now()
            if status is None:
                wait = max(state["next_start"], state["due_to_limit"]) - now
                if wait > 0:
                    return wait
                state["sent"] += 1
                state["next_start"] = now + state["gap_ms"] / 1000
            else:
                state["responses"] += 1
                if status == 429:
                    state["limited"] += 1
                    state["accepted_streak"] = 0
                    state["gap_ms"] = min(LIMITS["max_gap_ms"], state["gap_ms"] * 2)
                    state["due_to_limit"] = max(state["due_to_limit"], now + retry_seconds(retry_after, now))
                elif 200 <= status < 300:
                    state["accepted_streak"] += 1
                    if state["accepted_streak"] >= LIMITS["accepted_before_speedup"]:
                        state["accepted_streak"] = 0
                        state["gap_ms"] = max(LIMITS["min_gap_ms"], state["gap_ms"] * 3 // 4)
                else:
                    state["accepted_streak"] = 0
            write_json(path, state)
            return 0

    def admit(self):
        while delay := self.update():
            self.sleep(min(delay, 10))


class SimulatorPacingHandler(BaseHandler):
    """Use urllib's transport hooks. Forward the original request and response objects."""

    handler_order = 490

    def __init__(self, model, pacer):
        self.model = model
        self.pacer = pacer

    def applies(self, request):
        return (
            request.full_url == API_ROOT + "/chat/completions"
            and request.get_method() == "POST"
            and json.loads(request.data).get("model") == self.model
        )

    def https_request(self, request):
        if self.applies(request):
            self.pacer.admit()
        return request

    def https_response(self, request, response):
        if self.applies(request):
            self.pacer.update(response.status, response.headers.get("Retry-After"))
        return response


def paced_stage(args):
    _, root, _, _, _, _, _, stage, _, models, _, _ = args
    if stage in study.SOLVES:
        handler = SimulatorPacingHandler(models["user_simulator"]["id"], SharedPacer(root))
        install_opener(build_opener(handler))
    return study.revision_stage(args)


def sealed_prefix(root, cfg, pre):
    """Require complete, contiguous waves; refuse a partially written allocation."""
    seals, errors = {}, Counter()
    total = Decimal(0)
    gap = False
    for stage in cfg["stages"]:
        tasks = pre["stage_order"][stage]
        width = cfg["concurrency_by_stage"][stage]
        for start in range(0, len(tasks), width):
            name = f"{stage}-wave-{1 + start // width}.json"
            path = root / name
            if not path.exists():
                gap = True
                continue
            wave = study.retail.read(path)
            require(
                not gap
                and wave["tasks"] == tasks[start : start + width]
                and Decimal(wave["prior_covered"]) == total,
                "Incomplete or reordered wave prefix",
            )
            for tid in wave["tasks"]:
                key = f"task-{tid}/{stage}"
                end, _, slots = study.retail.sealed_rows(root / key)
                prior = study.retail.accounting(root / key, wave["per_shard_grant"])
                total += Decimal(prior["spent"])
                seals[key] = prior["seal"]
                require(
                    end["allocation_sha256"] == digest((root / key / "allocations.json").read_bytes()),
                    "Allocation seal differs",
                )
                for slot in slots:
                    res = slot.get("result", {})
                    if res.get("error_code") == "HTTP_429":
                        roles = [
                            role for role, bill in res["billing_by_role"].items() if bill["unknown_count"]
                        ]
                        require(len(roles) == 1, "Cannot attribute the recorded provider limit")
                        errors[roles[0]] += 1
    folders = {str(p.parent.relative_to(root)) for p in root.glob("task-*/*/allocations.json")}
    require(seals.keys() == folders and "user_simulator" in errors, "Checkpoint does not match closed shards")
    return {"stage_seals": seals, "covered_usd": str(total), "prior_http_429": dict(errors)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--max-usd", type=Decimal, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    cfg, base, conn, rows, tasks, pre = study.prepare(workspace)
    root = workspace / "runs" / cfg["name"]
    reg = study.retail.read(root / "registration.json")
    cap = reg["preflight"]["authorized_usd"]
    require(
        args.max_usd == Decimal(cap) <= Decimal(cfg["total_usd_limit"])
        and pre == study.retail.read(ROOT / study.PREFLIGHT)
        and reg["preflight"] == dict(pre, authorized_usd=cap)
        and reg["implementation"] == study.retail.implementation_identity(ROOT)
        and reg["implementation"]["implementation_commit"] == ORIGINAL
        and Path(study.__file__).resolve() == ROOT / "scripts/run_retail_transfer_revision.py",
        "Run the original native implementation and retain its registered aggregate cap",
    )
    source = Path(__file__).resolve()
    source_root = source.parents[1]
    head = subprocess.check_output(["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True).strip()
    pinned = subprocess.check_output(["git", "-C", str(source_root), "show", f"{head}:scripts/{source.name}"])
    require(pinned == source.read_bytes(), "The pacing implementation must be committed before dispatch")
    record = {
        "study": cfg["name"],
        "registration_sha256": digest((root / "registration.json").read_bytes()),
        "native_commit": ORIGINAL,
        "pacing_commit": head,
        "pacing_sha256": digest(pinned),
        "approved_total_usd": cap,
        "limits": LIMITS,
    }
    control = root / "pacing-activation.json"
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(not (root / "completion.json").exists(), "The entire study is already sealed")
        prefix = sealed_prefix(root, cfg, pre)
        if control.exists():
            existing = study.retail.read(control)
            require(
                all(existing[k] == v for k, v in record.items())
                and all(prefix["stage_seals"].get(k) == v for k, v in existing["stage_seals"].items()),
                "Different pacing rule or original sealed wave",
            )
        else:
            record.update(prefix, activated_utc=datetime.now(UTC).isoformat())
            study.retail.fixed(control, record)
    finally:
        os.close(fd)
    cfg["total_usd_limit"] = cap
    cfg["max_concurrent"] = LIMITS["max_workers"]
    study.retail.run_study(
        workspace, cfg, base, conn, rows, tasks, reg["preflight"], stage_runner=paced_stage
    )


if __name__ == "__main__":
    main()
