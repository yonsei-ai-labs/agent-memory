"""Run approved BFCL reader slots against their original sealed writers and native journals."""

from collections import Counter
from decimal import Decimal
from pathlib import Path
import time

from agent_memory_eval.analysis.recorded_decisions import require, sealed_bytes
from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.experiments.forms_batch import endpoint_terms
from agent_memory_eval.memory.writing import WriterFeedback, _experience, writer_inputs
from agent_memory_eval.models.expense_ledger import ExpenseLedger, read_run_reservations
from agent_memory_eval.models.openrouter import OpenRouterClient, request_reservation_usd
from agent_memory_eval.recording.journal import BlobStore, Journal, read_experiment
from agent_memory_eval.study.adapters.bfcl_transfer import (
    audited_reader, audited_writer, bounded_reader_batches, cohort_checkpoint, collect_reader, hosted_model,
    interrupted_reader_reason, job_sources, last_run, native_request_limit, reader_attempts, reader_config,
    reader_retry_wire, require_credentials, run_events, run_key, seal_interrupted_reader, submitted_or_ended, terminal,
)
from agent_memory_eval.study.cohort import is_cohort
from agent_memory_eval.study.followup_budget import approval_amount
from agent_memory_eval.study.reader_stage import reader_input_digest
from agent_memory_eval.study.records import fixed, read


class ReaderExpenseLedger(ExpenseLedger):
    """Keep global reservation sequence numbers and enforce each reader batch's independent sublimit."""

    def __init__(self, path, cap, max_calls, prefix_rows, budgets, batch_id, model):
        self.prefix_rows = prefix_rows
        self.budgets, self.batch_id, self.model = budgets, batch_id, model
        self.owner = {}
        self.reported, self.pending, self.requests = Counter(), Counter(), Counter()
        self.prior_covered = Decimal(0)
        super().__init__(path, cap, max_calls, float("inf"))
        require(self.prefix_rows == 0, "Incomplete BFCL writer expense checkpoint")

    def apply(self, row):
        if self.prefix_rows:
            require("reserved" not in row or (row["stage"] in {"source", "write"} and "reader_batch" not in row),
                    "Reader expense inside BFCL writer checkpoint")
            super().apply(row)
            self.prefix_rows -= 1
            if not self.prefix_rows:
                self.prior_covered = self.spent + self.held
            return
        seq = row["seq"]
        if "reserved" in row:
            batch = row.get("reader_batch")
            require(batch in self.budgets and row["stage"] == "solve" and row["model"] == self.model,
                    "Unregistered BFCL reader expense")
            self.owner[seq] = batch
            self.requests[batch] += 1
            self.pending[batch] += Decimal(row["reserved"])
        else:
            require(seq in self.owner and seq in self.open, "BFCL reader receipt has no open reader reservation")
            batch = self.owner[seq]
            self.pending[batch] -= self.open[seq]
            self.reported[batch] += Decimal(row["cost"])
        super().apply(row)
        cap, calls = self.budgets[batch]
        require(self.reported[batch] + self.pending[batch] <= cap and self.requests[batch] <= calls,
                "BFCL reader batch exceeds authorization")

    def can_reserve(self, maximum):
        batch = self.batch_id
        cap, calls = self.budgets[batch]
        return (super().can_reserve(maximum) and self.requests[batch] < calls
                and self.reported[batch] + self.pending[batch] + maximum <= cap)

    def append(self, row):
        if "reserved" in row:
            require(self.stage == "solve", "A reader batch can reserve only solver requests")
            row = dict(row, reader_batch=self.batch_id)
        super().append(row)


def _reader_seal(directory, dest, journals, tasks, *, create=False):
    """Bind the completed native byte prefixes; later disjoint batches may append to the same files."""
    marker = directory / "bfcl-checkpoint.json"
    receipt = directory / "result.json"
    if not marker.exists() and not create:
        require(not receipt.exists(), "Completed BFCL reader batch lost its native checkpoint")
        return None
    files = {"expenses.jsonl", *(f"{name}/log.jsonl" for name, groups in journals.items()
                                 if any(task in tasks for task, _, _ in groups))}
    registration = digest((directory / "registration.json").read_bytes())
    if marker.exists():
        checkpoint = read(marker)
        require(checkpoint["registration_sha256"] == registration and set(checkpoint["files"]) == files,
                "Changed BFCL reader checkpoint registration or journals")
        for name, ref in checkpoint["files"].items():
            sealed_bytes(dest / name, ref["sha256"], ref["bytes"])
    else:
        require(not receipt.exists(), "Completed BFCL reader batch lost its native checkpoint")
        refs = {}
        for name in sorted(files):
            raw = (dest / name).read_bytes()
            refs[name] = dict(bytes=len(raw), sha256=digest(raw))
        fixed(marker, dict(registration_sha256=registration, files=refs))
    seal = digest(marker.read_bytes())
    if receipt.exists():
        require(read(receipt)["native_checkpoint_sha256"] == seal, "Changed sealed BFCL reader checkpoint")
    return seal


def _batch_scopes(directory, batch, resolved, plan):
    require(read(directory / "registration.json") == batch and directory.name == batch["id"],
            "BFCL reader batch differs from its registration")
    limits = resolved["cfg"]["limits"]
    attempts = 1 + len(plan["retry"]["wait_seconds"])
    calls_by_task = {
        target: sum(limits[plan["reading"][job["reader"]]["read_mode"] + "_calls"] * attempts for job in jobs)
        for target, _, jobs in resolved["groups"]
    }
    owners, budgets, backoffs = {}, {}, {}
    study_cap = approval_amount(plan["budget"]["max_usd"])
    for record, waits in bounded_reader_batches(directory.parent.parent, calls_by_task.keys()):
        key, tasks = record["id"], record["tasks"]
        _reader_seal(directory.parent / key, directory.parent.parent, resolved["journals"], tasks)
        owners.update(dict.fromkeys(tasks, key))
        backoffs[key] = waits
        budgets[key] = approval_amount(record["max_usd"]), sum(calls_by_task[t] for t in tasks)
        study_cap = max(study_cap, approval_amount(record["cumulative_max_usd"]))
    return owners, budgets, backoffs, study_cap


def _slot_state(scan, study, job, retry, reserved, waits, blobs):
    """Follow only unsubmitted replacements and registered HTTP retries, including interrupted paid attempts."""
    runs, pending_wire = [], None
    retryable = {f"HTTP_{code}" for code in retry["http_codes"]}
    for attempt in reader_attempts(retry, waits):
        run = run_key(study, job, attempt)
        if run not in scan["starts"]:
            return runs, False, pending_wire
        while True:
            runs.append(run)
            events = run_events(scan, run)
            end = terminal(events)
            submitted = submitted_or_ended(events, end, run in reserved)
            later = run_key(study, run, "unsubmitted")
            if later not in scan["starts"]:
                break
            require(not submitted, "BFCL reader replaced a submitted or terminal session")
            run = later
        if not submitted:
            deferred = (end and end["termination_reason"] == "STUDY_LIMIT") or any(
                e["type"] == "runtime.error" and e["payload"].get("code") == "STUDY_LIMIT" for e in events)
            if deferred:
                last = next(e for e in reversed(events) if e["type"] == "model.request_started")
                return runs, False, last["payload"]["request_ref"]
            return runs, False, None
        if not end:
            if waits and interrupted_reader_reason(events, blobs, retry):
                return runs, False, None
            break
        reason = end["termination_reason"]
        if reason not in retryable and not (waits and reason == "STUDY_LIMIT"):
            break
        if waits:
            pending_wire = reader_retry_wire(events, reason)
    return runs, True, None


def _writer_notes(folder, evidence, workspace, root, plan, resolved, groups, sources, expenses, cutoff):
    scan, _, _ = evidence
    blobs = BlobStore(folder)
    ends = {event["run_id"]: (event["payload"], row["record_sha256"]) for row in scan["records"][:cutoff]
            for event in row["events"] if event["type"] == "run.ended"}
    notes = {}
    for task, draw, _ in groups:
        release = sources[task]
        source = last_run(scan, plan["id"], run_key(plan["id"], task, draw, "source"))
        require(source == release["run_id"] and source in ends and ends[source][1] == release["seal"],
                f"Changed sealed BFCL source for {task}")
        if release["success"] is None:
            require(ends[source][0]["grading_status"] != "graded", f"Missing BFCL source outcome for {task}")
            continue
        exp = _experience(blobs, scan, source)
        context, _ = writer_inputs(blobs, scan, exp, WriterFeedback(plan["writing"]["feedback"]))
        require(scan["events"][exp["outcome_event_id"]]["payload"]["official_success"] is release["success"]
                and digest(context["content"].encode()) == release["public_context_sha256"],
                f"Changed released BFCL experience for {task}")
        for form in resolved["forms"]:
            writer = last_run(scan, plan["id"], run_key(plan["id"], task, draw, "write", form))
            if writer in ends and ends[writer][0]["termination_reason"] == "memory_write_completed":
                notes[task, draw, form] = audited_writer(
                    folder, evidence, plan, root, workspace, resolved["cfg"], task, form, source, writer,
                    ends[writer][0], expenses,
                )
    return notes


def _reader_jobs(plan, resolved, dest, workspace, root, sources, refs, owners, backoffs, batch, expenses):
    """Check each appended session against its batch before opening any new journal for writing."""
    pending, seen, selected_notes = {}, {}, {}
    assigned = eligible = settled = 0
    study, batch_id = plan["id"], batch["id"]
    for name, groups in resolved["journals"].items():
        folder = dest / name
        log = folder / "log.jsonl"
        ref = refs[f"{name}/log.jsonl"]
        changed = log.stat().st_size != ref["bytes"]
        if not changed and not any(owners.get(task) == batch_id for task, _, _ in groups):
            continue
        evidence = read_experiment(folder)
        scan, _, experiment = evidence
        blobs = BlobStore(folder)
        require(experiment == study and not scan["tail_bytes"], "Invalid BFCL reader journal")
        cutoff = sealed_bytes(log, ref["sha256"], ref["bytes"]).count(b"\n")
        suffix = [e for row in scan["records"][cutoff:] for e in row["events"]]
        suffix_runs = {e["run_id"] for e in suffix if e["run_id"] is not None}
        notes = _writer_notes(folder, evidence, workspace, root, plan, resolved, groups, sources, expenses, cutoff)
        if any(owners.get(task) == batch_id for task, _, _ in groups):
            selected_notes.update(notes)
        authorized_runs, mixes = set(), set()
        for task, draw, jobs in groups:
            owner = owners.get(task)
            if owner is None:
                continue
            waits_for_owner = backoffs[owner]
            for job in jobs:
                runs, done, deferred = _slot_state(scan, study, job, plan["retry"], expenses, waits_for_owner, blobs)
                members = job_sources(resolved, job)
                available = all((member, draw, job["form"]) in notes for member in members)
                require(available or not runs, "Started BFCL reader has an unavailable writer")
                if available:
                    bindings = [notes[member, draw, job["form"]] for member in members]
                    if job["bank"] == "mixed":
                        mixes.add(tuple(note["snapshot"] for note in bindings))
                    cfg, mode = reader_config(resolved, plan, job, root)
                    for run in runs:
                        require(run in suffix_runs and run not in seen
                                and all(e.get("reader_batch") == owner for e in expenses.get(run, [])),
                                "BFCL reader run or reservation belongs to another batch")
                        requests = [e for e in run_events(scan, run) if e["type"] == "model.request_started"]
                        require(scan["starts"][run]["record_kind"] == "observed"
                                and len(requests) <= cfg["limits"][mode + "_calls"]
                                and all(e["payload"]["actor_role"] == "worker" for e in requests),
                                "Unexpected BFCL reader role or request count")
                        audited_reader(folder, evidence, run, job, cfg, mode, bindings, resolved["cases"][task],
                                       resolved["is_empty"], resolved["starts"][task], expenses.get(run, []))
                        seen[run] = owner
                        authorized_runs.add(run)
                if owner == batch_id:
                    assigned += 1
                    eligible += available
                    settled += done
                    if available and not done:
                        cost = request_reservation_usd(blobs.get(deferred), resolved["terms"]) if deferred else None
                        repair = (runs[-1] if waits_for_owner and runs and runs[-1] in expenses
                                  and runs[-1] in scan["incomplete_runs"] else None)
                        pending.setdefault(name, []).append((job, notes, cost, repair))
        require(suffix_runs == authorized_runs and all(
            event["run_id"] is not None or (event["type"] == "bank.snapshot_created"
                                            and tuple(event["payload"]["parent_snapshot_ids"]) in mixes)
            for event in suffix), "Unregistered activity after BFCL writer checkpoint")
    require(all(seen.get(run) == e["reader_batch"] for run, entries in expenses.items() for e in entries
                if e["stage"] == "solve"), "BFCL reader reservation lacks its registered native session")

    def lookup(task, form):
        note = selected_notes.get((task, 1, form))
        if note is None:
            return None
        item, = note["items"]
        return dict(source_run=note["source_run"], writer_run=note["writer_run"], snapshot=note["snapshot"],
                    title_sha256=digest(item["title"].encode()), body_sha256=digest(item["body"].encode()))

    require(assigned == batch["assigned_readers"] and eligible == batch["eligible_readers"]
            and reader_input_digest(plan, batch["tasks"], lookup) == batch["reader_inputs_sha256"],
            "Actual BFCL reader inputs differ from the approved native writers or assignments")
    return pending, assigned, eligible, settled


def run_readers(adapter, plan, workspace, root, *, resolved, registration, sources, batch, directory,
                progress=None, model_resolver=hosted_model, client_factory=OpenRouterClient, sleep=time.sleep):
    """Caller holds the registration lease and has frozen and authorized this disjoint reader batch."""
    workspace, root, directory = map(Path, (workspace, root, directory))
    dest = workspace / "runs" / plan["id"]
    require(directory.parent == dest / "reader-batches" and is_cohort(plan) and plan["first_pass"]["draws"] == 1
            and set(sources) == set(plan["benchmark"]["tasks"]), "Expected a released single-draw BFCL reader batch")
    prepared = adapter.resolve(plan, workspace, root, checkout=resolved["checkout"])
    require(read(dest / "registration.json") == registration and registration["approval"].get("through_stage") == "source"
            and registration["preflight"] == prepared["record"] == resolved["record"]
            and registration["worker"] == prepared["terms"] and (dest / "write-continuation.json").is_file()
            and not (dest / "completion.json").exists(), "BFCL reader batch differs from its native registration")
    require(cohort_checkpoint(dest, prepared["journals"], "source") is not None, "Missing BFCL source checkpoint")
    refs = cohort_checkpoint(dest, prepared["journals"], "write")
    require(refs is not None and (dest / "registration.json").stat().st_size == refs["registration.json"]["bytes"],
            "Missing BFCL writer checkpoint or changed registration")
    writer = read(dest / "write-continuation.json")
    require(writer["stage"] == "write" and digest(canonical(sources)) == writer["effective_sources_sha256"]
            == batch["effective_sources_sha256"] and writer["source_registration_sha256"] == refs["registration.json"]["sha256"]
            == batch["source_registration_sha256"] and digest((dest / "through-source.json").read_bytes())
            == writer["source_checkpoint_sha256"] and digest((dest / "through-write.json").read_bytes())
            == batch["writer_checkpoint_sha256"], "The released BFCL registration or writer checkpoint changed")
    owners, budgets, backoffs, study_cap = _batch_scopes(directory, batch, prepared, plan)
    path = dest / "expenses.jsonl"
    ref = refs[path.name]
    prefix_rows = sealed_bytes(path, ref["sha256"], ref["bytes"]).count(b"\n") if ref["bytes"] else 0
    authorization = read(dest / "authorization.json")
    require({k: v for k, v in authorization.items() if k != "max_requests"} == registration["approval"]
            and authorization["max_requests"] == native_request_limit(prepared["record"]["assigned"],
                                                                      prepared["cfg"]["limits"], plan["retry"]),
            "Original BFCL authorization changed")
    guard = ReaderExpenseLedger(path, study_cap, authorization["max_requests"], prefix_rows, budgets, batch["id"],
                                prepared["cfg"]["model"])
    require(guard.prior_covered == Decimal(batch["prior_source_writer_covered_usd"]),
            "BFCL source and writer spending differs from its release")
    pending, assigned, eligible, settled = _reader_jobs(
        plan, prepared, dest, workspace, root, sources, refs, owners, backoffs, batch, read_run_reservations(path))
    key = batch["id"]
    waits = backoffs[key]
    client = None
    for name, entries in pending.items():
        with Journal(dest / name, plan["id"], contract_version="1.2") as journal:
            for job, notes, deferred, repair in entries:
                if repair:
                    end = seal_interrupted_reader(journal, repair, plan["retry"])
                    require(end is not None, "BFCL reader recovery lost its recorded grade or HTTP receipt")
                    if end["termination_reason"] == "business_decision":
                        settled += 1
                        if progress:
                            progress("solve", settled, eligible, guard.reported[key])
                        continue
                    wire = reader_retry_wire(run_events(journal.scan, repair), end["termination_reason"])
                    deferred = request_reservation_usd(journal.blobs.get(wire), prepared["terms"])
                if (deferred is not None or waits) and not guard.can_reserve(deferred or Decimal(0)):
                    guard.denied = True
                    break
                if client is None:
                    if model_resolver is hosted_model or client_factory is OpenRouterClient:
                        require_credentials()
                    model = model_resolver(prepared["worker"])
                    require(endpoint_terms(dict(worker=model))["worker"] == prepared["terms"]
                            and model["canonical_slug"] == prepared["cfg"]["model_revision"],
                            "Hosted BFCL worker revision or endpoint terms changed")
                    client = client_factory(model, guard, route=prepared["worker"]["provider"],
                                            max_input_bytes=plan.get("limits", {}).get("max_input_bytes", 128000))
                    if isinstance(client, OpenRouterClient):
                        require_credentials()
                collect_reader(journal, guard, plan, root, prepared, job, notes, client, sleep, waits=waits)
                _, done, _ = _slot_state(journal.scan, plan["id"], job, plan["retry"], guard.reserved_runs,
                                         waits, journal.blobs)
                require(done or guard.denied, "BFCL reader returned without a disposition")
                settled += done
                if progress:
                    progress("solve", settled, eligible, guard.reported[key])
                if guard.denied:
                    break
        if guard.denied:
            break
    complete = settled == eligible
    seal = _reader_seal(directory, dest, prepared["journals"], batch["tasks"], create=True) if complete else None
    return dict(complete=complete, completed_through="solve" if complete else None, budget_deferred=guard.denied and not complete,
                assigned_readers=assigned, eligible_readers=eligible, settled_readers=settled,
                unavailable_readers=assigned - eligible, reported_usd=str(guard.reported[key] or 0),
                held_usd=str(guard.pending[key] or 0), requests=guard.requests[key], native_checkpoint_sha256=seal)
