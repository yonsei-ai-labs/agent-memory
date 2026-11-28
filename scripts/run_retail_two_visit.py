"""Preflight, register and execute the prespecified derived retail two-visit cohort."""

import argparse
import fcntl
import json
import os
import time
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import (
    endpoint_terms,
    live_models,
    write_json,
)
from agent_memory_eval.experiments.forms_supplement import cooldown_seconds
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.models.openrouter import (
    OpenRouterClient,
    ProviderError,
    request_reservation_usd,
)
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, Journal, read_experiment
from agent_memory_eval.reproduction.freeze import implementation_identity
from scripts.analyze_full_retail import require
from scripts.audit_retail_order_choice import publish
from scripts.prepare_retail_order_pairs import prepare as prepare_pairs
from scripts.retail_two_visit_episode import (
    continue_decision,
    first_decision,
    run_source,
    run_writer,
)
from scripts.run_full_retail import modules_for
from scripts.run_recheck_rounds import fixed
from scripts.run_retail_order_choice_replay import prices

DRAFT = "protocols/drafts/tau2-retail-two-visit-v1.json"
PREFLIGHT = "artifacts/analyses/20261122-retail-two-visit-preflight.json"
STAGES = ("source", "writer", "decision", "continuation")


def read(path):
    return read_json(path.read_bytes())


def key(*parts):
    return digest(canonical(parts))


def prepare(root, protocol, *, workspace=None):
    workspace = root if workspace is None else workspace
    design = {k: v for k, v in protocol.items() if k != "authorization"} | {"status": "draft"}
    require(design == read(root / DRAFT), "Ready profile differs from the studied design")
    diagnostic = read_json(asset_text(root, protocol["diagnostic"]).encode())
    cohort = read_json(asset_text(root, protocol["cohort"]).encode())
    retail = read_json(asset_text(root, protocol["retail"]).encode())
    replay = read_json(asset_text(root, protocol["replay"]).encode())
    base = read_json(asset_text(root, retail["base_design"]).encode())
    connection = read_json(asset_text(root, base["model"]["connection_profile_ref"]).encode())
    modules = modules_for(workspace)
    checked, tasks, public = prepare_pairs(workspace, diagnostic)
    require(
        read_json(
            canonical(dict(protocol_sha256=protocol["diagnostic"]["sha256"], model_requests_sent=0, **checked))
        )
        == cohort,
        "Native derived tasks or public prefixes changed",
    )
    db = read_json(asset_text(workspace, diagnostic["inputs"]["db"]).encode())
    profiles = {digest(u.encode()): usr["orders"] for u, usr in db["users"].items()}
    groups = {"includes_profile_first": [], "both_later": []}
    for u in cohort["reserved"]:
        first = profiles[u["customer"]][0]
        groups["includes_profile_first" if first in u["order_pair"] else "both_later"].append(u["customer"])
    sample = set()
    for group, customers in groups.items():
        order = sorted(customers, key=lambda u: key(protocol["salt"], "continuation", u))
        sample.update(order[: protocol["continuation_sample"][group]])
    jobs = {s: [] for s in STAGES}
    sources = set()

    def job(stage, task, arm, **details):
        return dict(
            stage=stage, task=task, arm=arm, run="r_" + key(protocol["salt"], stage, task, arm)[:32], **details
        )

    for u in cohort["reserved"]:
        customer = u["customer"]
        for c in u["cases"]:
            src = c["source"]
            if src not in sources:
                sources.add(src)
                jobs["source"].append(job("source", src, "no_memory", customer=customer))
                for form in protocol["forms"]:
                    jobs["writer"].append(job("writer", src, form, customer=customer))
            for a in protocol["arms"]:
                row = job(
                    "decision",
                    c["task"],
                    a,
                    customer=customer,
                    source=src,
                    mode=c["mode"],
                    prefix=c["public_prefix_sha256"],
                    position=profiles[customer].index(c["target"]) + 1,
                    target=c["target"],
                    in_sample=customer in sample,
                )
                jobs["decision"].append(row)
                jobs["continuation"].append(row)
    for stage in STAGES:
        jobs[stage].sort(key=lambda r: key(protocol["salt"], stage, r["run"]))
    counts = {s: len(rows) for s, rows in jobs.items()}
    require(
        all(len({row["run"] for row in rows}) == counts[stage] for stage, rows in jobs.items()),
        "Duplicate allocation",
    )
    require(
        counts == {"source": 154, "writer": 616, "decision": 2310, "continuation": 2310}
        and len(sample) == 26
        and sum(r["in_sample"] for r in jobs["decision"]) == 780,
        "Incomplete two-visit allocation",
    )
    common = asset_text(root, base["writing"]["common_instruction_ref"]).rstrip("\r\n")
    instructions = {n: asset_text(root, replay[n + "_instruction"]) for n in ("query", "preload")}
    instructions.update({f: common + "\n" + base["writing"]["lines"][f] for f in protocol["forms"]})
    endpoints = read_json(asset_text(root, retail["endpoints"]).encode())["models"]
    record = {
        "study": protocol["name"],
        "protocol_sha256": digest(canonical(design)),
        "cohort_sha256": protocol["cohort"]["sha256"],
        "native_tasks_sha256": cohort["native_tasks_sha256"],
        "public_prefixes_sha256": cohort["public_prefixes_sha256"],
        "schedule_sha256": key(jobs),
        "counts": counts,
        "continuation_sample": sorted(sample),
        "sample_full_episodes": 780,
        "all_full_episodes": 2310,
        "positions": {
            mode: dict(
                Counter(
                    "first" if r["position"] == 1 else "later"
                    for r in jobs["decision"]
                    if r["arm"] == "no_memory" and r["mode"] == mode
                )
            )
            for mode in protocol["modes"]
        },
        "model_requests_sent": 0,
    }
    return {
        "protocol": protocol,
        "connection": connection,
        "instructions": instructions,
        "models": endpoints,
        "modules": modules,
        "jobs": jobs,
        "tasks": tasks,
        "public": public,
        "record": record,
    }


class ExpenseLedger:
    """Persist the full request reservation before HTTP. Unreconciled requests retain it after restart."""

    def __init__(self, path, cap, max_calls, deadline):
        self.path, self.cap, self.max_calls, self.deadline = path, Decimal(str(cap)), max_calls, deadline
        self.calls, self.spent, self.held, self.open = 0, Decimal(0), Decimal(0), {}
        self.reserved_runs = set()
        self.denied = self.unknown_cost = False
        self.stage = self.run_id = None
        self.retry_after = 0
        if path.exists():
            for line in path.read_bytes().splitlines(keepends=True):
                require(line.endswith(b"\n"), "Unreconciled expense ledger tail")
                self.apply(read_json(line))

    def apply(self, row):
        seq = row["seq"]
        if "reserved" in row:
            maximum = Decimal(row["reserved"])
            require(
                type(seq) is int and seq == self.calls + 1 and maximum.is_finite() and maximum >= 0,
                "Out-of-order reservation",
            )
            self.calls, self.open[seq] = seq, maximum
            self.reserved_runs.add(row.get("run"))
            self.held += maximum
        else:
            cost = Decimal(row["cost"])
            require(
                seq in self.open and cost.is_finite() and 0 <= cost <= self.open[seq],
                "Invalid receipt or pricing exceeded reservation",
            )
            self.held -= self.open.pop(seq)
            self.spent += cost
        require(
            self.spent + self.held <= self.cap and self.calls <= self.max_calls,
            "Expense ledger exceeds authorization",
        )

    def append(self, row):
        with self.path.open("ab") as stream:
            stream.write(canonical(row) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.apply(row)

    def can_reserve(self, maximum):
        return (self.calls < self.max_calls and self.spent + self.held + maximum <= self.cap
                and time.monotonic() < self.deadline)

    def before(self, wire, model):
        maximum = request_reservation_usd(wire, model)
        if not self.can_reserve(maximum):
            self.denied = True
            raise ProviderError("STUDY_LIMIT")
        if self.unknown_cost:
            raise ProviderError("UNPRICED_SLOT")
        self.append(
            {
                "seq": self.calls + 1,
                "reserved": str(maximum),
                "wire_sha256": digest(canonical(wire)),
                "model": model["id"],
                "stage": self.stage,
                "run": self.run_id,
            }
        )

    def after(self, response):
        error = response.get("error") if isinstance(response, dict) else None
        if isinstance(error, dict) and (error.get("retry_after_seconds") or error.get("retry_after_utc")):
            self.retry_after = cooldown_seconds(error, 0, datetime.now(UTC))
        usage = response.get("usage") if isinstance(response, dict) else None
        cost = usage.get("cost") if isinstance(usage, dict) else None
        if type(cost) not in (float, int) or not Decimal(str(cost)).is_finite() or cost < 0:
            self.unknown_cost = True
            return
        self.append({"seq": self.calls, "cost": str(cost), "generation": response.get("id")})


def read_receipt(folder, stage):
    path = folder / (stage + ".json")
    if not path.exists():
        return None
    value = read(path)
    size = value["journal_bytes"]
    if size:
        with (folder / "log.jsonl").open("rb") as fp:
            prefix = fp.read(size)
        require(len(prefix) == size and digest(prefix) == value["journal_sha256"], "Changed stage journal")
    return value


def sealed_bank(folder, status, expected_run):
    require(status["run"] == expected_run, "Writer identity differs from schedule")
    scan, state, _ = read_experiment(folder)
    (end,) = [e for e in scan["events"].values() if e["type"] == "run.ended"]
    require(
        end["run_id"] == expected_run
        and end["payload"]["end_snapshot_id"] == status["snapshot"]
        and end["payload"]["termination_reason"] == "memory_write_" + status["status"],
        "Writer snapshot is not a normal completion or voluntary empty",
    )
    return snapshot_items(BlobStore(folder), state, status["snapshot"])


def recover_finished(folder, stage, job, keep, *, evidence=None):
    """Recover solely from durable terminal evidence when a process died before its receipt."""
    if not (folder / "log.jsonl").exists():
        return None
    scan, _, experiment = evidence if evidence is not None else read_experiment(folder)
    require(experiment == job["run"], "Attempted session identity changed")
    if scan["pending_requests"] or scan["tail_bytes"]:
        return None
    events = [e for e in scan["events"].values() if e["run_id"] == job["run"]]
    ended = next((e["payload"] for e in reversed(events) if e["type"] == "run.ended"), None)
    blobs = BlobStore(folder)
    if stage == "decision":
        if ended and ended["termination_reason"] == "private_read_limit":
            return {"status": "private_read_limit", "run": job["run"]}
        marker = next(
            (e["payload"] for e in events if e["payload"].get("origin") == "revisit.first_decision"), None
        )
        if marker and (
            (keep and events[-1]["payload"] == marker)
            or (not keep and ended and ended["termination_reason"] == "first_decision")
        ):
            ref = marker["checkpoint_ref"]
            return {
                "status": "checkpoint" if keep else "decision",
                "run": job["run"],
                "checkpoint": ref,
                "request": blobs.get(ref)["request_id"],
            }
    if (
        ended
        and stage == "writer"
        and ended["termination_reason"] in {"memory_write_empty", "memory_write_completed"}
    ):
        return {
            "status": ended["termination_reason"].removeprefix("memory_write_"),
            "run": job["run"],
            "snapshot": ended["end_snapshot_id"],
            "provider_error": None,
        }
    if ended and ended["grading_status"] == "not_available" and stage == "continuation":
        terminal = [e["payload"]["result"] for e in events
                    if e["payload"].get("origin") == "revisit.continuation_outcome"]
        if terminal:
            result, = terminal
            reason = result.get("termination") or result.get("provider_error") or result.get("reason")
            require(result["run"] == job["run"] and result["status"] in {"incomplete", "error", "budget_deferred"}
                    and reason == ended["termination_reason"], "Continuation termination differs from the journal")
            if "actor_role" in result:
                actors = [e["payload"]["actor_role"] for e in events if e["type"] == "model.request_started"
                          or (e["type"] == "runtime.error" and e["payload"].get("code") == reason)]
                require(reason in {"ROLE_REQUEST_LIMIT", "INPUT_SIZE_LIMIT"} and actors
                        and result["actor_role"] == actors[-1], "Limit actor differs from the native request")
            return result
    if ended and ended["grading_status"] == "graded":
        if stage == "source":
            return {"status": "completed", "run": job["run"]}
        if stage == "continuation":
            (outcome,) = [e["payload"] for e in events if e["type"] == "outcome.scored"]
            grade = blobs.get(outcome["official_artifact_ref"])
            require(ended["termination_reason"] in {"agent_stop", "user_stop"}
                    and grade["reward"] == outcome["official_score"], "Continuation native grade changed")
            return {
                "status": "completed",
                "run": job["run"],
                "termination": ended["termination_reason"],
                "trajectory": grade["trajectory_ref"],
                "db_reward": grade["reward"],
            }
    return None


def verify_continuation_receipt(folder, job, receipt, *, evidence=None):
    marker = folder / "continuation.started"
    require(marker.is_file() and read(marker) == {"stage": "continuation", "run": job["run"]},
            "Continuation lacks its original attempt marker")
    require((folder / "log.jsonl").stat().st_size == receipt["journal_bytes"], "Unsealed follow-up journal suffix")
    actual = recover_finished(folder, "continuation", job, True, evidence=evidence)
    expected = {k: v for k, v in receipt.items() if k not in {"journal_bytes", "journal_sha256"}}
    require(actual and actual == expected, "Full outcome differs from its native journal")


def execute(study, runtime, *, through, continuation, destination, pause_after_transient=None, admit_continuation=None,
            progress=None):
    jobs, tasks, prefixes = study["jobs"], study["tasks"], study["public"]
    known_sources = {j["task"]: j for j in jobs["source"]}
    writers = {(j["task"], j["arm"]): j for j in jobs["writer"]}
    totals = {}
    for stage in STAGES[: STAGES.index(through) + 1]:
        counts = Counter()
        consecutive_errors = 0
        for j in jobs[stage]:
            keep = continuation == "all" or (continuation == "sample" and j.get("in_sample", False))
            if stage == "continuation" and not keep:
                continue
            folder = destination / j["run"]
            previous = read_receipt(folder, stage)
            if previous:
                if stage == "continuation" and runtime.get("seal_continuation"):
                    verify_continuation_receipt(folder, j, previous)
                counts[previous["status"]] += 1
                if (admit_continuation and previous.get("provider_error") == "STUDY_LIMIT"
                        and Decimal(previous["ceiling_usd"]) >= runtime["guard"].cap):
                    runtime["guard"].denied = True
                    runtime["guard"].pause_reason = "study_limit"
                    break
                continue
            folder.mkdir(exist_ok=True)
            marker = folder / (stage + ".started")
            if marker.exists():
                result = recover_finished(folder, stage, j, keep)
                if result is None:
                    counts["interrupted"] += 1
                    continue
            else:
                if runtime["guard"].denied:
                    break
                if stage == "continuation" and admit_continuation and not admit_continuation(j, folder):
                    runtime["guard"].denied = True
                    runtime["guard"].pause_reason = "study_limit"
                    break
                fixed(marker, {"stage": stage, "run": j["run"]})
                runtime["guard"].unknown_cost = False
                runtime["guard"].retry_after = 0
                runtime["guard"].stage, runtime["guard"].run_id = stage, j["run"]
                result = run_job(
                    stage, j, runtime, tasks, prefixes, folder, destination, known_sources, writers, keep
                )
            journal = folder / "log.jsonl"
            raw = journal.read_bytes() if journal.exists() else b""
            value = dict(**result, journal_bytes=len(raw), journal_sha256=digest(raw))
            fixed(folder / (stage + ".json"), value)
            counts[result["status"]] += 1
            guard = runtime["guard"]
            if progress:
                progress(stage, sum(counts.values()), len(jobs[stage]), str(guard.spent))
            if admit_continuation and result.get("provider_error") == "STUDY_LIMIT":
                guard.denied = True
            write_json(
                destination / "progress.json",
                {
                    "stage": stage,
                    "counts": counts,
                    "reserved_calls": guard.calls,
                    "confirmed_usd": str(guard.spent),
                    "held_usd": str(guard.held),
                },
            )
            if pause_after_transient:
                code = result.get("provider_error") or ""
                status = code.removeprefix("HTTP_") if code.startswith("HTTP_") else str(result.get("http_status", ""))
                transient = (code == "TRANSPORT_UNKNOWN" or status in {"408", "429"}
                             or len(status) == 3 and status.isdigit() and status.startswith("5"))
                consecutive_errors = consecutive_errors + 1 if transient else 0
                if consecutive_errors >= pause_after_transient:
                    totals[stage] = dict(counts)
                    guard.pause_reason = "consecutive_transient_errors"
                    return totals
            if result.get("provider_error") in {"HTTP_429", "HTTP_503", "TRANSPORT_UNKNOWN"}:
                time.sleep(min(max(12, guard.retry_after), max(0, guard.deadline - time.monotonic())))
            if result.get("provider_error") in {
                "HTTP_400",
                "HTTP_401",
                "HTTP_402",
                "HTTP_403",
                "HTTP_404",
                "MODEL_MISMATCH",
                "WRITER_MODEL_MISMATCH",
                "UNPINNED_ROUTE",
                "CREDENTIAL_IN_RESPONSE",
            }:
                if runtime.get("seal_continuation"):
                    totals[stage] = dict(counts)
                    guard.pause_reason = result["provider_error"]
                    return totals
                raise ContractError("Provider rejected the frozen execution: " + result["provider_error"])
        totals[stage] = dict(counts)
        if runtime["guard"].denied:
            if admit_continuation:
                runtime["guard"].pause_reason = "study_limit"
            break
    return totals


def run_job(stage, job, runtime, tasks, prefixes, folder, dest, sources, writers, keep):
    tid, arm = job["task"], job["arm"]
    items, lineage = [], []
    if stage == "writer":
        src = sources[tid]
        upstream = read_receipt(dest / src["run"], "source")
        if not upstream or upstream["status"] != "completed":
            return {"status": "source_missing"}
    if stage == "decision" and job.get("native_arm") == "query":
        items, lineage = runtime["source_banks"][job["source"]]
    elif stage == "decision" and arm in {"query", "preload"}:
        for form in runtime["protocol"]["forms"]:
            w = writers[job["source"], form]
            wr = read_receipt(dest / w["run"], "writer")
            if not wr or wr["status"] not in {"completed", "empty"}:
                return {"status": "bank_missing"}
            items.extend(sealed_bank(dest / w["run"], wr, w["run"]))
            lineage.append(w["run"])
    if stage == "continuation":
        first = read_receipt(folder, "decision")
        if not first or first["status"] != "checkpoint":
            return {"status": "decision_missing"}
    with Journal(folder, job["run"], contract_version="1.2") as journal:
        try:
            if stage == "source":
                return run_source(journal, job, tasks[tid], runtime)
            if stage == "writer":
                return run_writer(
                    journal,
                    job,
                    runtime,
                    dest / src["run"],
                    src["run"],
                    upstream["journal_sha256"],
                    runtime["instructions"][arm],
                )
            if stage == "decision":
                return first_decision(
                    journal, job, tasks[tid], prefixes[job["prefix"]], runtime, items, lineage, keep=keep
                )
            return continue_decision(journal, job, tasks[tid], prefixes[job["prefix"]], runtime, first)
        except ProviderError as error:
            return {"status": "error", "provider_error": error.code}
        except ContractError as error:
            if str(error) == "FIRST_DECISION_LIMIT" or getattr(error, "code", "") in {
                "ROLE_REQUEST_LIMIT",
                "INPUT_SIZE_LIMIT",
            }:
                return {"status": "error", "reason": getattr(error, "code", str(error))}
            raise


def live(root, study, *, design_path=DRAFT, preflight_path=PREFLIGHT):
    p = study["protocol"]
    require(
        p.get("status") == "ready"
        and isinstance(p.get("authorization"), dict)
        and os.getenv("OPENROUTER_API_KEY"),
        "A funded ready profile and credentials are required",
    )
    auth = p["authorization"]
    require(
        set(auth) == {"through", "continuation", "max_usd", "max_requests", "max_seconds"}
        and auth["through"] in {"decision", "continuation"}
        and auth["continuation"] in {"sample", "all"}
        and Decimal(str(auth["max_usd"])).is_finite()
        and 0 < Decimal(str(auth["max_usd"]))
        and type(auth["max_requests"]) is int
        and auth["max_requests"] > 0
        and type(auth["max_seconds"]) is int
        and auth["max_seconds"] > 0,
        "Invalid authorization",
    )
    require(read(root / preflight_path) == study["record"], "Live allocation differs from the published preflight")
    freeze = implementation_identity(root)
    frozen = study["models"]
    models = live_models(study["connection"])
    require(
        endpoint_terms(models) == endpoint_terms(frozen)
        and all(models[k]["canonical_slug"] == frozen[k]["canonical_slug"] for k in models),
        "Endpoint terms changed",
    )
    for k, m in models.items():
        m["reservation_pricing"] = prices(endpoint_terms(frozen)[k])
    dest = root / "runs" / p["name"]
    dest.mkdir(exist_ok=True)
    with (dest / "controller.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fixed(
            dest / "registration.json",
            {
                "design": read(root / design_path),
                "freeze": freeze,
                "preflight": study["record"],
                "jobs": study["jobs"],
                "continuation": auth["continuation"],
                "endpoints": endpoint_terms(models),
            },
        )
        # A later grant can extend the originally named set on the original checkout.
        # Its dollar, call and wall-clock limits remain cumulative from the first start.
        grant_path = dest / "authorization.json"
        if grant_path.exists():
            old = read(grant_path)
            require(
                old["continuation"] == auth["continuation"]
                and STAGES.index(old["through"]) <= STAGES.index(auth["through"])
                and all(
                    Decimal(str(old[k])) <= Decimal(str(auth[k]))
                    for k in ("max_usd", "max_requests", "max_seconds")
                ),
                "Later grant would change the sample or decrease a cumulative limit",
            )
        fixed(dest / ("grant-" + key(auth) + ".json"), auth)
        write_json(grant_path, auth)
        if not (dest / "start.json").exists():
            fixed(dest / "start.json", {"unix_time": time.time()})
        remaining = read(dest / "start.json")["unix_time"] + auth["max_seconds"] - time.time()
        guard = ExpenseLedger(
            dest / "expense.jsonl", auth["max_usd"], auth["max_requests"], time.monotonic() + remaining
        )
        episode = study.get("episode_protocol", p)
        clients = {
            r: OpenRouterClient(
                m,
                guard,
                route=study["connection"][r]["provider"],
                max_input_bytes=episode["limits"]["max_input_bytes"],
                timeout_seconds=episode["request_timeout_seconds"],
            )
            for r, m in models.items()
        }
        totals = execute(
            study,
            dict(study, protocol=episode, models=models, guard=guard, clients=clients),
            through=auth["through"],
            continuation=auth["continuation"],
            destination=dest,
        )
        completion = {
            "stages": totals,
            "reserved_calls": guard.calls,
            "confirmed_usd": str(guard.spent),
            "held_usd": str(guard.held),
            "stopped_at_limit": guard.denied,
        }
        write_json(dest / "summary.json", completion)
        return completion


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "live"))
    parser.add_argument("--profile", type=Path, default=ROOT / DRAFT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    protocol = read(args.profile)
    if args.mode == "live":
        require(
            protocol.get("status") == "ready" and "authorization" in protocol, "Draft has no live authorization"
        )
    study = prepare(ROOT, protocol)
    result = study["record"] if args.mode == "preflight" else live(ROOT, study)
    if args.output:
        publish(args.output, result)
    print(json.dumps(result, indent=2))
