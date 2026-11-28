"""Coordinate whole τ² reader episodes during a provider outage without changing their native inputs."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from pathlib import Path
import time

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.experiments.forms_batch import BatchGuard, write_json
from agent_memory_eval.experiments.forms_supplement import cooldown_seconds
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.study.records import read


class ReaderAdmission:
    """After a transient, only one new conversation probes recovery. Already running conversations may finish."""

    def __init__(self, directory, codes, intervals, *, now=time.time, sleep=time.sleep):
        self.directory = Path(directory)
        self.codes = set(codes)
        self.intervals = intervals
        self.now, self.sleep = now, sleep
        self.probe = None
        self.probe_at = 0
        self.failed = False
        self.received = False

    @contextmanager
    def locked(self):
        with (self.directory / "provider-admission.lock").open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    @property
    def state_path(self):
        return self.directory / "provider-admission.json"

    def state(self):
        return read(self.state_path) if self.state_path.exists() else dict(failures=0, until=0)

    def before_episode(self):
        self.failed = self.received = False
        while True:
            waiting_probe = None
            with self.locked():
                state = self.state()
                now = self.now()
                wait = state["until"] - now
                if wait <= 0:
                    if not state["failures"]:
                        return
                    stream = (self.directory / "provider-probe.lock").open("a+b")
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        waiting_probe = stream
                    else:
                        self.probe, self.probe_at = stream, now
                        return
            if waiting_probe is None:
                self.sleep(min(wait, 30))
            else:
                # A late in-flight 503 still has immediate access to the separate cooldown state lock.
                with waiting_probe:
                    fcntl.flock(waiting_probe, fcntl.LOCK_EX)

    def response(self, data):
        self.received = True
        error = data.get("error") if isinstance(data, dict) else None
        if not isinstance(error, dict) or error.get("code") not in self.codes:
            return
        with self.locked():
            state = self.state()
            # Concurrent errors from the same outage can extend Retry-After. Only a failed probe advances backoff.
            count = max(1, state["failures"] + int(self.probe is not None and not self.failed))
            now = self.now()
            delay = cooldown_seconds(error, self.intervals[min(count, len(self.intervals))-1],
                                     datetime.fromtimestamp(now, timezone.utc))
            write_json(self.state_path, dict(failures=count, until=max(state["until"], now + delay)))
            self.failed = True

    def run(self, execute, row, writer):
        try:
            result = execute(row, writer)
            if self.probe is not None and self.received and not self.failed:
                with self.locked():
                    if self.state()["until"] <= self.probe_at:
                        write_json(self.state_path, dict(failures=0, until=0))
            return result
        finally:
            self.close()

    def close(self):
        if self.probe is not None:
            self.probe.close()
            self.probe = None


class ReaderGuard(BatchGuard):
    def __init__(self, admission, **limits):
        super().__init__(**limits)
        self.admission = admission
        self.max_seconds = limits["max_seconds"]

    def before_episode(self):
        before = time.monotonic()
        self.admission.before_episode()
        self.deadline += time.monotonic() - before

    def after(self, response):
        super().after(response)
        self.admission.response(response)

    def restore_native_clock(self, directory, rows):
        # This grant limits active native work. A process outage or shared provider wait has no active episode clock.
        used = 0
        for row in rows:
            result = row.get("result") or {}
            if not (result.get("log_sha256") or result.get("partial_log_sha256")):
                continue
            folder = safe_path(directory, row["directory"])
            timestamps = []
            for raw in (folder / "log.jsonl").read_bytes().splitlines(keepends=True):
                if raw.endswith(b"\n"):
                    timestamps.extend(e["timestamp_utc"] for e in read_json(raw)["events"] if e["run_id"] == row["run_id"])
            if timestamps:
                used += max(0, (datetime.fromisoformat(timestamps[-1]) - datetime.fromisoformat(timestamps[0])).total_seconds())
        self.deadline = time.monotonic() + self.max_seconds - used


def confirmed_transient(shard, row):
    """Only a received HTTP failure can start a new episode for a previously dispatched slot."""
    from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions

    result = row["result"]
    log = RecordedDecisions(safe_path(shard, row["directory"]), row["run_id"], result["log_sha256"])
    last = log.of_type("model.request_finished")[-1]["payload"]
    require(last["status"] == "error" and last["delivery_status"] == "response_received" and last.get("response_ref"),
            "A transient reader retry has no received provider error")
    error = log.blobs.get(last["response_ref"]).get("error") or {}
    require(result["error_code"] == f"HTTP_{error.get('code')}", "A reader retry differs from its provider response")


def repair_arms(shard, certificate):
    """Allow only the independently verified wire failure and its eligible, never-dispatched suffix."""
    if certificate is None:
        return set()
    from agent_memory_eval.study.reader_wire_evidence import verified_recovery
    require(verified_recovery(shard, expected_seal=certificate["seal"]) == certificate,
            "Wire-repair slots differ from the sealed input-validation evidence")
    return certificate["failed"].keys() | certificate["unstarted"].keys()


def retry_rows(shard, rows, codes, certificate=None, *, verify_http=True):
    repaired = repair_arms(shard, certificate)
    selected = []
    for row in rows:
        result = row.get("result") or {}
        if row["status"] == "error" and result.get("official_success") is None and result.get("error_code") in codes:
            if verify_http:
                confirmed_transient(shard, row)
            selected.append(row)
        elif row["arm"] in repaired:
            selected.append(row)
    return selected
