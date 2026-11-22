"""Recover only sealed upstream provider failures and never-submitted decisions under the original cap."""

import argparse
import fcntl
import json
import os
import subprocess
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import (
    endpoint_terms,
    live_models,
    write_json,
)
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.reproduction.freeze import implementation_identity
from scripts.analyze_full_retail import require
from scripts.audit_retail_order_choice import publish
from scripts.run_recheck_rounds import fixed
from scripts.run_retail_order_choice_replay import prices
from scripts.run_retail_two_visit import (
    PREFLIGHT,
    ExpenseLedger,
    execute,
    key,
    prepare,
    read,
    read_receipt,
)

PROFILE = "protocols/pilots/tau2-retail-two-visit-recovery-v1.json"
PREFLIGHT_RECOVERY = "artifacts/analyses/20261122-retail-two-visit-recovery-preflight.json"


def attempt(job, number, name):
    origin = job.get("original_run", job["run"])
    return {
        **job,
        "run": "r_" + key(name, origin, number)[:32],
        "original_run": origin,
        "attempt": number,
    }


def unopened(folder, row, stage, reserved):
    path = folder / row["run"]
    value = read_receipt(path, stage)
    require(
        value
        and value["status"]
        == {"writer": "source_missing", "decision": "bank_missing", "continuation": "decision_missing"}[stage]
        and value["journal_bytes"] == 0
        and value["journal_sha256"] == digest(b"")
        and not (path / "log.jsonl").exists()
        and (stage, row["run"]) not in reserved,
        "A recovery slot has a previous journal, reservation or conflicting receipt",
    )


def eligible(folder, protocol, seal, *, closed):
    reg = read(folder / "registration.json")
    jobs = reg["jobs"]
    require(
        digest((folder / "registration.json").read_bytes()) == seal["main_registration_sha256"]
        and seal["observed_second_visits"] == 0
        and key(jobs) == reg["preflight"]["schedule_sha256"]
        and reg["continuation"] == "all",
        "Wrong original cohort",
    )
    reserved = set()
    with (folder / "expense.jsonl").open("rb") as stream:
        for raw in stream:
            require(raw.endswith(b"\n"), "Original expense ledger tail is incomplete")
            entry = read_json(raw)
            if "reserved" in entry:
                reserved.add((entry["stage"], entry["run"]))
    failed = {s["original_run"]: s for s in seal["source_errors"]}
    selected = {stage: [] for stage in jobs}
    for j in jobs["source"]:
        receipt = read_receipt(folder / j["run"], "source")
        if receipt["status"] == "completed":
            require(j["run"] not in failed, "Sealed source completed normally")
            continue
        ref = failed.get(j["run"])
        require(
            ref
            and ref["task"] == j["task"]
            and ref["customer"] == j["customer"]
            and receipt["status"] == "error"
            and receipt.get("provider_error") == ref["provider_error"] in protocol["transient_errors"]
            and digest((folder / j["run"] / "source.json").read_bytes()) == ref["receipt_sha256"],
            "Source is not a sealed provider failure",
        )
        selected["source"].append(j)
    require({j["run"] for j in selected["source"]} == set(failed), "Missing sealed source")
    tasks = {j["task"] for j in selected["source"]}
    for j in jobs["writer"]:
        if j["task"] in tasks:
            unopened(folder, j, "writer", reserved)
            selected["writer"].append(j)
        else:
            require(
                read_receipt(folder / j["run"], "writer")["status"] in {"empty", "completed"},
                "Unexpected original writer failure",
            )
    for stage in ("decision", "continuation"):
        for j in jobs[stage]:
            if j["source"] in tasks and j["arm"] in {"preload", "query"}:
                if closed or stage == "decision":
                    unopened(folder, j, stage, reserved)
                selected[stage].append(j)
    require(
        {s: len(rows) for s, rows in selected.items()} == protocol["counts"], "Recovery schedule has changed"
    )
    require(
        {j["run"] for j in selected["decision"]} == {j["run"] for j in selected["continuation"]},
        "Continuation differs from its first decision",
    )
    # Stages were sequential. Locate the first actually submitted target in registered order.
    first = next(j for j in jobs["decision"] if ("decision", j["run"]) in reserved)
    with (folder / first["run"] / "log.jsonl").open("rb") as stream:
        start = next(
            e for raw in stream for e in read_json(raw)["events"] if e["type"] == "model.request_started"
        )
    require(
        datetime.fromisoformat(seal["registered_utc"]) < datetime.fromisoformat(start["timestamp_utc"]),
        "Eligibility was sealed after exposure",
    )
    recovery = {stage: [attempt(j, 1, protocol["name"]) for j in rows] for stage, rows in selected.items()}
    require(
        not (
            {j["run"] for rows in recovery.values() for j in rows}
            & {j["run"] for rows in jobs.values() for j in rows}
        ),
        "Duplicate original run ID",
    )
    return recovery


def preflight(jobs, profile, seal):
    return {
        "profile_sha256": digest(canonical(profile)),
        "schedule_sha256": key(jobs),
        "original_registration_sha256": seal["main_registration_sha256"],
        "eligible_source_ids": [j["original_run"] for j in jobs["source"]],
        "counts": {s: len(rows) for s, rows in jobs.items()},
        "model_requests_sent": 0,
    }


def available(main, profile):
    grant = read(main / "authorization.json")
    reg = read(main / "registration.json")
    end = read(main / "summary.json")
    require(
        grant["max_usd"] == profile["authorization"]["combined_max_usd"]
        and set(end["stages"]) == set(reg["jobs"])
        and all(
            sum(end["stages"][s].values()) == len(rows) and "interrupted" not in end["stages"][s]
            for s, rows in reg["jobs"].items()
        ),
        "Original schedule is still open",
    )
    ledger = ExpenseLedger(main / "expense.jsonl", grant["max_usd"], grant["max_requests"], time.monotonic())
    require(
        (ledger.spent, ledger.held, ledger.calls)
        == (Decimal(end["confirmed_usd"]), Decimal(end["held_usd"]), end["reserved_calls"]),
        "Original budget does not reconcile",
    )
    return {
        "max_usd": str(Decimal(str(grant["max_usd"])) - ledger.spent - ledger.held),
        "combined_max_usd": grant["max_usd"],
        "max_requests": grant["max_requests"] - ledger.calls,
        "deadline_unix": read(main / "start.json")["unix_time"] + grant["max_seconds"],
        "original_confirmed_usd": str(ledger.spent),
        "original_held_usd": str(ledger.held),
        "original_ledger_sha256": digest((main / "expense.jsonl").read_bytes()),
    }


def verified_replacements(main, supplement):
    """Map eligible original slots to their separate raw journals for the augmented analysis."""
    protocol = read(ROOT / PROFILE)
    seal = read_json(asset_text(ROOT, protocol["eligibility"]).encode())
    expected = eligible(main, protocol, seal, closed=True)
    actual = read(supplement / "registration.json")
    require(
        actual["format"] == protocol["format"]
        and actual["profile_sha256"] == digest(canonical(protocol))
        and actual["original_registration_sha256"] == seal["main_registration_sha256"]
        and actual["jobs"] == expected
        and preflight(expected, protocol, seal) == read(ROOT / PREFLIGHT_RECOVERY),
        "Supplement is not the sealed technical recovery",
    )
    return {j["original_run"]: j for j in expected["decision"]}


def upstream(stage, jobs, sources, study, runtime, dest, profile):
    """Fresh attempts are permitted only after a recorded eligible upstream provider error."""
    active = jobs
    selected = {}
    submitted = []
    wave = 1
    guard = runtime["guard"]
    while active:
        current = [j if wave == 1 else attempt(j, wave, profile["name"]) for j in active]
        stages = {"source": sources, "writer": [], "decision": [], "continuation": [], stage: current}
        execute({**study, "jobs": stages}, runtime, through=stage, continuation="all", destination=dest)
        retry = []
        for j in current:
            submitted.append(j)
            selected[j["original_run"]] = j
            receipt = read_receipt(dest / j["run"], stage)
            if (
                receipt
                and receipt["status"] == "error"
                and receipt.get("provider_error") in profile["transient_errors"]
            ):
                retry.append(j)
        if not retry or guard.denied or time.monotonic() >= guard.deadline:
            break
        wave += 1
        delay = 120 if wave == 2 else 600 if wave == 3 else 1800
        # The cooldown survives a restart and starts when the last error receipt was published.
        last = max((dest / j["run"] / (stage + ".json")).stat().st_mtime for j in retry)
        time.sleep(max(0, min(last + delay - time.time(), guard.deadline - time.monotonic())))
        if time.monotonic() >= guard.deadline:
            break
        active = retry
    return list(selected.values()), submitted


def live(root, profile, original, seal, runs):
    require(
        profile["status"] == "ready" and os.getenv("OPENROUTER_API_KEY"),
        "Recovery needs the authorized profile and credentials",
    )
    main = runs / original["name"]
    dest = runs / profile["name"]
    dest.mkdir(exist_ok=True)
    with (main / "controller.lock").open("a+b") as parent_lock, (dest / "controller.lock").open("a+b") as lock:
        for fp in (parent_lock, lock):
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        jobs = eligible(main, profile, seal, closed=True)
        require(
            preflight(jobs, profile, seal) == read(root / PREFLIGHT_RECOVERY),
            "Eligibility differs from fixed preflight",
        )
        auth = available(main, profile)
        require(
            Decimal(auth["max_usd"]) > 0 and auth["max_requests"] > 0 and time.time() < auth["deadline_unix"],
            "Shared original allowance is exhausted",
        )
        if (dest / "summary.json").exists():
            require(read(dest / "authorization.json") == auth, "Original expenses changed after recovery")
            return read(dest / "summary.json")
        study = prepare(root, original)
        reg = read(main / "registration.json")
        require(
            study["record"] == reg["preflight"] == read(root / PREFLIGHT),
            "Original public inputs or tasks changed",
        )
        identity = implementation_identity(root)
        subprocess.run(
            [
                "git",
                "diff",
                "--quiet",
                reg["freeze"]["implementation_commit"],
                identity["implementation_commit"],
                "--",
                "src",
                "scripts/run_retail_two_visit.py",
                "scripts/retail_two_visit_episode.py",
                "uv.lock",
            ],
            cwd=root,
            check=True,
        )
        models = live_models(study["connection"])
        frozen = study["models"]
        require(
            endpoint_terms(models) == endpoint_terms(frozen)
            and all(models[k]["canonical_slug"] == frozen[k]["canonical_slug"] for k in models),
            "Endpoint terms changed",
        )
        for k, m in models.items():
            m["reservation_pricing"] = prices(endpoint_terms(frozen)[k])
        require(
            endpoint_terms(models) == reg["endpoints"], "Recovery routes or prices differ from the original"
        )
        fixed(dest / "authorization.json", auth)
        fixed(
            dest / "registration.json",
            {
                "format": profile["format"],
                "profile_sha256": digest(canonical(profile)),
                "original_registration_sha256": seal["main_registration_sha256"],
                "design": reg["design"],
                "freeze": identity,
                "preflight": study["record"],
                "jobs": jobs,
                "endpoints": reg["endpoints"],
                "continuation": "all",
            },
        )
        guard = ExpenseLedger(
            dest / "expense.jsonl",
            auth["max_usd"],
            auth["max_requests"],
            time.monotonic() + auth["deadline_unix"] - time.time(),
        )
        clients = {
            r: OpenRouterClient(
                m,
                guard,
                route=study["connection"][r]["provider"],
                max_input_bytes=original["limits"]["max_input_bytes"],
                timeout_seconds=original["request_timeout_seconds"],
            )
            for r, m in models.items()
        }
        runtime = {**study, "guard": guard, "clients": clients, "models": models}
        sources, source_attempts = upstream("source", jobs["source"], [], study, runtime, dest, profile)
        writers, writer_attempts = upstream("writer", jobs["writer"], sources, study, runtime, dest, profile)
        fixed(
            dest / "upstreams.json",
            {
                "selected": {"source": sources, "writer": writers},
                "attempts": {"source": source_attempts, "writer": writer_attempts},
            },
        )
        totals = execute(
            {**study, "jobs": {**jobs, "source": sources, "writer": writers}},
            runtime,
            through="continuation",
            continuation="all",
            destination=dest,
        )
        closure = {
            "stages": totals,
            "reserved_calls": guard.calls,
            "confirmed_usd": str(guard.spent),
            "held_usd": str(guard.held),
            "stopped_at_limit": guard.denied,
            "combined_confirmed_usd": str(Decimal(auth["original_confirmed_usd"]) + guard.spent),
            "combined_held_usd": str(Decimal(auth["original_held_usd"]) + guard.held),
        }
        write_json(dest / "summary.json", closure)
        return closure


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "live"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runs", type=Path, default=ROOT / "runs", help="Original and recovery journals shared by pinned checkouts")
    args = parser.parse_args()
    profile = read(ROOT / PROFILE)
    original = read_json(asset_text(ROOT, profile["original"]).encode())
    seal = read_json(asset_text(ROOT, profile["eligibility"]).encode())
    if args.mode == "live":
        output = live(ROOT, profile, original, seal, args.runs.resolve())
    else:
        jobs = eligible(args.runs / original["name"], profile, seal, closed=False)
        output = preflight(jobs, profile, seal)
    if args.output:
        publish(args.output, output)
    print(json.dumps(output, indent=2))
