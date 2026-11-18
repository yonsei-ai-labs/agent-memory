"""Small, verifiable capsules of recorded worker inputs, separate from runtime state."""

from contextlib import contextmanager
from functools import lru_cache
import fcntl
import os
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, RunContext, canonical, digest
from agent_memory_eval.contracts.validation import inspect_journal
from agent_memory_eval.recording.journal import read_experiment, safe_path
from agent_memory_eval.recording.provenance import public_assistant_message


def public_text_message(value):
    if value.get("role") == "assistant":
        return public_assistant_message(value)
    if value.get("role") not in {"system", "user", "tool"}:
        raise ContractError("Unsupported public context role")
    message = dict(role=value["role"], content=value.get("content"))
    if message["content"] is not None and not isinstance(message["content"], str):
        raise ContractError("Recorded experience supports text contexts only")
    if message["role"] == "tool":
        message["tool_call_id"] = value["tool_call_id"]
    return message


def _closed_run(scan, run_id):
    start = scan["starts"][run_id]
    events = [e for e in scan["events"].values() if e["run_id"] == run_id]
    ends = [e for e in events if e["type"] == "run.ended"]
    outcomes = [e for e in events if e["type"] == "outcome.scored"]
    if (len(ends) != 1 or len(outcomes) != 1 or outcomes[0]["payload"]["grading_status"] != "graded"
            or type(outcomes[0]["payload"]["official_success"]) is not bool
            or ends[0]["payload"]["grading_status"] != "graded"):
        raise ContractError("Recorded experience requires a closed, graded run")
    if any(e["type"].startswith("memory.") for e in events):
        raise ContractError("The initial recorded-experience adapter accepts no-memory sources only")
    return start, ends[0], outcomes[0]


def _pair(scan, run_id, request_id=None):
    requests = [e for e in scan["events"].values() if e["run_id"] == run_id
                and e["type"] == "model.request_started" and e["payload"]["actor_role"] == "worker"]
    if not requests:
        raise ContractError("Source has no worker request")
    request = requests[-1] if request_id is None else next(
        (e for e in requests if e["payload"]["request_id"] == request_id), None)
    if request is None:
        raise ContractError("Decision request does not belong to this source worker")
    finishes = [e for e in scan["events"].values() if e["type"] == "model.request_finished"
                and e["payload"]["request_id"] == request["payload"]["request_id"]]
    if len(finishes) != 1 or finishes[0]["payload"]["status"] != "response":
        raise ContractError("Selected worker request has no normal recorded reply")
    return request, finishes[0]


@contextmanager
def _source(root, expected_log_sha256):
    root = Path(root).absolute()
    fd = os.open(safe_path(root, "writer.lock"), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ContractError("Cannot capture experience from an active writer") from None
        data = safe_path(root, "log.jsonl").read_bytes()
        if digest(data) != expected_log_sha256:
            raise ContractError("Source log differs from the selected immutable input")
        scan, state, experiment = read_experiment(root)
        if scan["tail_bytes"] or scan["pending_requests"] or scan["incomplete_runs"]:
            raise ContractError("Source journal is incomplete")
        from agent_memory_eval.recording.journal import BlobStore
        yield scan, BlobStore(root), data, experiment
        if safe_path(root, "log.jsonl").read_bytes() != data:
            raise ContractError("Source log changed during capture")
    finally:
        os.close(fd)


def capture_recorded_context(journal, source_root, *, source_run_id, log_sha256, request_id=None):
    """Copy selected evidence, never the complete source blob/state store."""
    return capture_recorded_contexts(journal, source_root, [(source_run_id, request_id)], log_sha256=log_sha256)[0]


def capture_recorded_contexts(journal, source_root, selections, *, log_sha256):
    """Read the same locked source once for all the selected decisions."""
    refs = []
    with _source(source_root, log_sha256) as (scan, blobs, data, experiment):
        log_ref = journal.blobs.put(data.decode("utf-8"))
        for source_run_id, request_id in selections:
            start, end, outcome = _closed_run(scan, source_run_id)
            request, finish = _pair(scan, source_run_id, request_id)
            request_ref, response_ref = request["payload"]["request_ref"], finish["payload"]["response_ref"]
            for ref in (request_ref, response_ref, outcome["payload"]["official_artifact_ref"]):
                if ref is None or journal.blobs.put(blobs.get(ref)) != ref:
                    raise ContractError("Selected source artifact is missing or changed")
            wire = blobs.get(request_ref)
            if not wire["messages"] or wire["messages"][0]["role"] != "system":
                raise ContractError("Source worker context has no leading policy")
            task_ref = journal.blobs.put(public_text_message(wire["messages"][0]))
            archive = dict(format="agent-memory-recorded-context/v1", source_path=str(source_root),
                source_experiment_id=experiment, source_run_id=source_run_id, record_kind=start["record_kind"],
                source_log_ref=log_ref, source_log_sha256=log_sha256,
                source_end_event_id=end["event_id"], outcome_event_id=outcome["event_id"],
                outcome_artifact_ref=outcome["payload"]["official_artifact_ref"],
                request_event_id=request["event_id"], response_event_id=finish["event_id"],
                request_ref=request_ref, response_ref=response_ref, task_ref=task_ref)
            ref = journal.blobs.put(archive)
            _read_recorded_context(journal.blobs, ref, lambda _: scan)
            refs.append(ref)
    return refs


def read_recorded_context(blobs, ref):
    return _read_recorded_context(blobs, ref, inspect_journal)


class ReplayContextReader:
    """Reuse one parse of identical log bytes. Every capsule and blob is still hash-checked."""

    def __init__(self):
        self._inspect = lru_cache(maxsize=1)(inspect_journal)

    def read(self, blobs, ref):
        value = _read_recorded_context(blobs, ref, self._inspect)
        # Callers never receive the cached mutable scan.
        return {key: value[key] for key in ("wire", "archive", "response")}


def _read_recorded_context(blobs, ref, inspect):
    value = blobs.get(ref)
    if value["format"] != "agent-memory-recorded-context/v1":
        raise ContractError("Unknown recorded context capsule")
    data = blobs.get(value["source_log_ref"]).encode("utf-8")
    if digest(data) != value["source_log_sha256"]:
        raise ContractError("Archived source log hash differs")
    scan = inspect(data)
    if scan["tail_bytes"] or scan["pending_requests"] or scan["incomplete_runs"]:
        raise ContractError("Archived source log is incomplete")
    start, end, outcome = _closed_run(scan, value["source_run_id"])
    request = scan["events"][value["request_event_id"]]
    began, finish = _pair(scan, value["source_run_id"], request["payload"]["request_id"])
    if (start["experiment_id"] != value["source_experiment_id"] or start["record_kind"] != value["record_kind"]
            or end["event_id"] != value["source_end_event_id"] or outcome["event_id"] != value["outcome_event_id"]
            or began["event_id"] != request["event_id"] or finish["event_id"] != value["response_event_id"]
            or began["payload"]["request_ref"] != value["request_ref"]
            or finish["payload"]["response_ref"] != value["response_ref"]
            or outcome["payload"]["official_artifact_ref"] != value["outcome_artifact_ref"]):
        raise ContractError("Context capsule differs from its recorded producer events")
    wire, response = blobs.get(value["request_ref"]), blobs.get(value["response_ref"])
    if (wire["messages"][0]["role"] != "system"
            or any(m["role"] == "system" for m in wire["messages"][1:])
            or canonical(blobs.get(value["task_ref"])) != canonical(public_text_message(wire["messages"][0]))):
        raise ContractError("Archived public policy differs from its worker input")
    blobs.get(value["outcome_artifact_ref"])
    return dict(archive=value, scan=scan, wire=wire, response=response, outcome=outcome["payload"])


def import_recorded_experience(journal, source_root, *, source_run_id, log_sha256):
    archive_ref = capture_recorded_context(journal, source_root, source_run_id=source_run_id, log_sha256=log_sha256)
    source = read_recorded_context(journal.blobs, archive_ref)
    event = RunContext(journal.experiment_id, record_kind=source["archive"]["record_kind"]).event(
        "business.transition", dict(origin="experience.imported", archive_ref=archive_ref))
    journal.append([event])
    return journal.blobs.put(dict(format="agent-memory-public-experience/v2",
                                  archive_ref=archive_ref, import_event_id=event["event_id"]))


def read_imported_experience(blobs, scan, value):
    event = scan["events"][value["import_event_id"]]
    if (event["type"] != "business.transition" or event["run_id"] is not None
            or event["payload"] != dict(origin="experience.imported", archive_ref=value["archive_ref"])):
        raise ContractError("Experience import has no matching prior producer")
    source = read_recorded_context(blobs, value["archive_ref"])
    a = source["archive"]
    if event["record_kind"] != a["record_kind"]:
        raise ContractError("Experience import changed the source record kind")
    last, _ = _pair(source["scan"], a["source_run_id"])
    if last["event_id"] != a["request_event_id"]:
        raise ContractError("Writing experience must use the final worker context")
    messages = source["wire"]["messages"][1:] + [source["response"]["choices"][0]["message"]]
    return dict(format="agent-memory-public-experience/v2", source_run_id=a["source_run_id"],
        record_kind=a["record_kind"], task_ref=a["task_ref"], source_end_event_id=a["source_end_event_id"],
        source_end_seq=source["scan"]["events"][a["source_end_event_id"]]["event_seq"],
        local_ready_seq=event["event_seq"], archive_ref=value["archive_ref"],
        recorded_outcome=source["outcome"],
        observations=[dict(message=public_text_message(m)) for m in messages])
