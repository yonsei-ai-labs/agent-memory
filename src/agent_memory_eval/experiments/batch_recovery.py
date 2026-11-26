"""Resume the untouched allocations of a registered batch without resubmitting a started job."""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import os
from types import SimpleNamespace

from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path


POLICY = "resume_unstarted/v1"


@contextmanager
def stage_lease(directory):
    """A surviving worker still owns this lock if its parent process disappears."""
    directory.mkdir(exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ContractError("Another worker still owns this task stage") from error
        yield
    finally:
        os.close(fd)


def usage_from_events(events):
    roles, stats = {}, {}
    for event in events:
        pay = event["payload"]
        if event["type"] == "model.request_started":
            roles[pay["request_id"]] = pay["actor_role"]
        elif event["type"] == "model.request_finished":
            role = roles[pay["request_id"]]
            cell = stats.setdefault(role, dict(requests=0, http_calls=0, total_tokens=0, reported_usd=Decimal(0),
                                               unknown_count=0))
            cell["requests"] += 1
            cell["http_calls"] += pay["delivery_status"] != "not_submitted"
            cell["unknown_count"] += pay["cost_status"] == "unknown"
            cell["total_tokens"] += (pay.get("usage") or {}).get("total_tokens") or 0
            if pay["cost_status"] == "observed":
                cell["reported_usd"] += Decimal(str(pay["cost_usd"]))
    for cell in stats.values():
        cell["reported_usd"] = str(cell["reported_usd"])
    return stats


def _restore_result(folder, row):
    """A durable result may precede the allocation update. Partial work keeps its own evidence."""
    log = safe_path(folder, "log.jsonl")
    stored = row.get("result")
    raw = log.read_bytes() if log.is_file() else None
    scan, state, _ = read_experiment(folder) if raw is not None else (None, None, None)
    checksum = digest(raw) if raw is not None else None
    events = list(scan["events"].values()) if scan is not None else []
    if any(e["run_id"] != row["run_id"] for e in events if e["type"].startswith("model.request_")):
        raise ContractError("Interrupted job contains another assignment's requests")
    billing = usage_from_events(events)
    if scan and not scan["tail_bytes"] and any(e["type"] == "run.ended" and e["run_id"] == row["run_id"] for e in events):
        from agent_memory_eval.experiments.forms_solve import recorded_solve_result
        from agent_memory_eval.experiments.forms_write import recorded_writer_result

        journal = SimpleNamespace(data=raw, state=state, events=scan["events"], scan=scan, blobs=BlobStore(folder))
        project = recorded_writer_result if row["phase"] == "write" else recorded_solve_result
        result = project(journal, row["run_id"])
        result.pop("bank", None)
        result.update(log_sha256=checksum, billing_by_role=billing)
        result["unknown_cost"] = bool(result.get("unknown_cost") or any(v["unknown_count"] for v in billing.values()))
        if (any(result[k] != row[k] for k in ("run_id", "task_id", "arm", "repetition"))
                or stored and result != {k: v for k, v in stored.items() if k != "unpriced_reservation"}):
            raise ContractError("Completed allocation differs from its journal")
        return stored or result
    if row["status"] not in {"started", "interrupted"}:
        raise ContractError("Terminal allocation has no verifiable journal or checkpoint")
    pending = scan["pending_requests"] if scan is not None else []
    # A start was durably recorded before transmission. Its billing is unknown until a return is committed.
    for e in events:
        pay = e["payload"]
        if e["type"] == "model.request_started" and pay["request_id"] in pending:
            cell = billing.setdefault(pay["actor_role"], dict(requests=0, http_calls=0, total_tokens=0,
                                                              reported_usd="0", unknown_count=0))
            cell["requests"] += 1
            cell["http_calls"] += 1
            cell["unknown_count"] += 1
    result = dict(run_id=row["run_id"], status="interrupted", error_code="CONTROLLER_INTERRUPTED",
                  billing_by_role=billing, partial_log_sha256=checksum, pending_request_ids=pending,
                  unknown_cost=bool(pending or (scan and scan["tail_bytes"])
                                    or any(v["unknown_count"] for v in billing.values())))
    if stored and stored != result:
        raise ContractError("Interrupted allocation changed since it was recorded")
    return result


def restore_batch(directory, expected, jobs, new_rows, guard, reservations):
    from agent_memory_eval.experiments.forms_batch import endpoint_terms, write_json

    jobs_dir = safe_path(directory, "jobs")
    jobs_dir.mkdir(exist_ok=True)
    ledger = safe_path(directory, "allocations.json")
    control = safe_path(directory, "control.json")
    if not control.exists():
        if ledger.exists() or any(jobs_dir.iterdir()):
            raise ContractError("Interrupted batch has evidence but no registration")
        write_json(control, expected)
    previous = read_json(control.read_bytes())
    ignore = {"started_at", "model_catalog_entries"}
    if ({k: v for k, v in previous.items() if k not in ignore} != {k: v for k, v in expected.items() if k not in ignore}
            or endpoint_terms(previous["model_catalog_entries"]) != endpoint_terms(expected["model_catalog_entries"])):
        raise ContractError("Interrupted batch differs from the registered inputs or prices")
    started = datetime.fromisoformat(previous["started_at"])
    guard.deadline -= max(0, (datetime.now(timezone.utc) - started).total_seconds())
    if not ledger.exists():
        if any(jobs_dir.iterdir()):
            raise ContractError("Job evidence exists without its allocation ledger")
        return new_rows
    rows = read_json(ledger.read_bytes())
    if (len(rows) != len(jobs) or any(not isinstance(r.get("run_id"), str) or not r["run_id"]
            or any(r.get(k) != v for k, v in j.items()) or r.get("directory") != f'jobs/{j["index"]:04d}'
            for r, j in zip(rows, jobs)) or len({r["run_id"] for r in rows}) != len(jobs)
            or {p.name for p in jobs_dir.iterdir()} - {f'{j["index"]:04d}' for j in jobs}):
        raise ContractError("Interrupted allocations differ from the registered schedule")
    confirmed = Decimal(0)
    for row in rows:
        folder = safe_path(directory, row["directory"])
        if row["status"] in {"planned", "not_started"}:
            if folder.exists() or "result" in row:
                raise ContractError("Unstarted allocation already has execution evidence")
            continue
        result = _restore_result(folder, row)
        if row["status"] not in {"started", "interrupted", result["status"]}:
            raise ContractError("Terminal job status differs from its checkpoint")
        row.update(status=result["status"], result=result)
        if row["status"] == "interrupted":
            row["reason"] = "controller_interrupted"
        elif row.get("reason") == "controller_error":
            row.pop("reason")
        for usage in result["billing_by_role"].values():
            guard.calls += usage["http_calls"]
            confirmed += Decimal(usage["reported_usd"])
        if result["unknown_cost"]:
            old = result.pop("unpriced_reservation", None)
            bounded = result["status"] != "interrupted" and reservations and reservations(row)
            if old is not None and result.get("unpriced_reservation") != old:
                raise ContractError("Unpriced request reservation differs from the original ledger")
            if not bounded:
                guard.unknown_cost = True
        if result.get("error_code") in {"FIRST_PASS_MISMATCH", "TASK_START_MISMATCH", "SPEND_LIMIT",
                                        "TIME_LIMIT", "CALL_LIMIT"}:
            guard.denied = result["error_code"]
    if confirmed > Decimal(str(guard.max_usd)):
        raise ContractError("Restored spending exceeds the remaining registered grant")
    guard.reported_usd = float(confirmed)
    return rows
