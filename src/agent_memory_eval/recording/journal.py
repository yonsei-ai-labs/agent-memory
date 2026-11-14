"""One local writer, immutable blobs, checksummed commits, and disposable views."""

from copy import deepcopy
import fcntl
import os
from pathlib import Path
import tempfile
import threading

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import inspect_journal, read_public_prefix, record_hash, validators


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def safe_path(root, relative):
    name = Path(relative)
    if name.is_absolute() or ".." in name.parts:
        raise ContractError("Path outside experiment")
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ContractError("Path outside experiment")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ContractError("Symlinks are not experiment artifacts")
        if part == root:
            break
    return path


class BlobStore:
    def __init__(self, root):
        self.root = Path(root).absolute()

    def mkdir(self, path):
        if path.exists():
            if path.is_symlink() or not path.is_dir():
                raise ContractError("Unsafe artifact directory")
            return
        self.mkdir(path.parent)
        path.mkdir()
        sync_directory(path.parent)

    def put_bytes(self, data, media_type="application/json"):
        checksum = digest(data)
        name = "blobs/sha256/" + checksum
        path = safe_path(self.root, name)
        self.mkdir(path.parent)
        if path.exists():
            if path.read_bytes() != data:
                raise ContractError("Existing immutable blob is corrupt")
            return dict(path=name, sha256=checksum, media_type=media_type)
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # link publishes without replacing any existing immutable file.
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise ContractError("Immutable blob collision")
            sync_directory(path.parent)
        finally:
            os.unlink(temporary)
        return dict(path=name, sha256=checksum, media_type=media_type)

    def put(self, value):
        return self.put_bytes(canonical(value))

    def get(self, ref):
        data = safe_path(self.root, ref["path"]).read_bytes()
        if digest(data) != ref["sha256"]:
            raise ContractError("Artifact hash mismatch")
        return read_json(data)

    def verify(self, value, seen=None):
        seen = set() if seen is None else seen
        if isinstance(value, dict):
            if {"path", "sha256", "media_type"} <= value.keys():
                identity = (value["path"], value["sha256"])
                if identity not in seen:
                    seen.add(identity)
                    self.verify(self.get(value), seen)
            else:
                for child in value.values():
                    self.verify(child, seen)
        elif isinstance(value, list):
            for child in value:
                self.verify(child, seen)

    def write_view(self, name, data):
        path = safe_path(self.root, "views/" + name)
        self.mkdir(path.parent)
        fd, temporary = tempfile.mkstemp(prefix=".view-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_directory(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def empty_state():
    return {"banks": {}, "versions": {}, "snapshots": {}, "cursors": {},
            "return_bytes": {}, "provenance": {}, "assignments": {}}


def read_experiment(root):
    """Read and verify committed evidence, keeping an unconfirmed tail untouched."""
    root = Path(root).absolute()
    blobs = BlobStore(root)
    scan = inspect_journal(safe_path(root, "log.jsonl").read_bytes())
    state, experiment, verified = empty_state(), None, set()
    for row in scan["records"]:
        if row["journal_version"] != "1.1":
            raise ContractError("Runtime evidence requires journal v1.1")
        experiment = experiment or row["experiment_id"]
        if row["experiment_id"] != experiment:
            raise ContractError("Experiment identity mismatch")
        blobs.verify(row, verified)
        state = blobs.get(row["state_ref"])
        for event in row["events"]:
            if event["type"] == "memory.stored":
                payload = event["payload"]
                memory = blobs.get(payload["body_ref"])
                if digest(memory["body"].encode()) != payload["body_sha256"]:
                    raise ContractError("Stored body checksum mismatch")
                read_public_prefix(root, payload["public_context_prefix"], scan["events"], event["event_seq"])
    return scan, state, experiment


class Journal:
    def __init__(self, root, experiment_id, fault=None):
        self.root = Path(root).absolute()
        if self.root.resolve() != self.root:
            raise ContractError("Experiment root must not traverse a symlink")
        self.blobs = BlobStore(self.root)
        self.blobs.mkdir(self.root)
        self.lock = threading.RLock()
        self.experiment_id = experiment_id
        self.fault = fault or (lambda point: None)
        self.poisoned = False
        self._lease = None
        lease_path = safe_path(self.root, "writer.lock")
        fd = os.open(lease_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self._lease = fd
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.path = safe_path(self.root, "log.jsonl")
            if not self.path.exists():
                log_fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(log_fd)
                sync_directory(self.root)
            self.data = self.path.read_bytes()
            self.checks = validators()
            self.scan = inspect_journal(self.data, self.checks)
            if self.scan["tail_bytes"]:
                raise ContractError("Unconfirmed journal tail preserved; automatic append refused")
            self.state = empty_state()
            verified = set()
            for row in self.scan["records"]:
                if row["journal_version"] != "1.1" or row["experiment_id"] != experiment_id:
                    raise ContractError("Unsupported journal profile or experiment identity")
                self.blobs.verify(row, verified)
                self.state = self.blobs.get(row["state_ref"])
            for event in self.events.values():
                if event["type"] == "memory.stored":
                    read_public_prefix(self.root, event["payload"]["public_context_prefix"],
                                       self.events, event["event_seq"])
            self.operations = {row["operation"]["operation_id"]: row for row in self.scan["records"]
                               if row["operation"] is not None}
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._lease is not None:
            os.close(self._lease)
            self._lease = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def events(self):
        return self.scan["events"]

    def append(self, events, *, state=None, operation=None):
        with self.lock:
            if self._lease is None or self.poisoned:
                raise ContractError("Writer is closed or requires recovery")
            next_state = deepcopy(self.state if state is None else state)
            events = deepcopy(events)
            for seq, event in enumerate(events, len(self.events) + 1):
                event["event_seq"] = seq
            row = dict(journal_version="1.1", experiment_id=self.experiment_id,
                       commit_seq=len(self.scan["records"]) + 1,
                       prev_sha256=self.scan["records"][-1]["record_sha256"] if self.scan["records"] else None,
                       record_sha256="0" * 64, operation=deepcopy(operation), events=events,
                       state_ref=self.blobs.put(next_state))
            row["record_sha256"] = record_hash(row)
            line = canonical(row) + b"\n"
            scan = inspect_journal(self.data + line, self.checks, validated_prefix=len(self.scan["records"]))
            self.blobs.verify(row)
            # A failed write/fsync has unknown durability. Do not reuse this writer.
            try:
                self.fault("before_log")
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
                try:
                    remaining = memoryview(line)
                    while remaining:
                        count = os.write(fd, remaining)
                        if count == 0:
                            raise OSError("Short journal write")
                        remaining = remaining[count:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self.fault("after_log")
            except BaseException:
                self.poisoned = True
                raise
            self.data += line
            self.scan, self.state = scan, next_state
            if operation is not None:
                self.operations[operation["operation_id"]] = row
            return deepcopy(row)

    def export_views(self):
        with self.lock:
            self.blobs.write_view("events.jsonl", b"".join(canonical(e) + b"\n" for e in self.events.values()))
            for bank, value in self.state["banks"].items():
                self.blobs.write_view(f"banks/{bank}.json", canonical(value))
            for snapshot, value in self.state["snapshots"].items():
                self.blobs.write_view(f"snapshots/{snapshot}.json", canonical(value))

    def public_prefix(self, ctx):
        entries = []
        cutoff = 0
        for event in self.events.values():
            if event["run_id"] == ctx.run_id and event["type"] == "business.transition":
                ref = event["payload"].get("public_message_ref")
                if ref is not None:
                    cutoff = event["event_seq"]
                    entries.append(dict(seq=len(entries) + 1, run_id=ctx.run_id,
                                        event_id=event["event_id"], message=self.blobs.get(ref)))
        data = b"".join(canonical(e) + b"\n" for e in entries)
        ref = self.blobs.put_bytes(data, "application/x-ndjson")
        return dict(path=ref["path"], run_id=ctx.run_id, entry_count=len(entries),
                    through_event_seq=cutoff, prefix_sha256=ref["sha256"])
