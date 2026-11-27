"""Pace an existing experiment process pool using receipts from registered consultations."""

import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import time


TRANSIENT = {429, 502, 503, 504}


class ProviderReceipts:
    def __init__(self, study, role, since):
        self.study = study
        self.role = role
        self.since = since
        self.offsets = {}
        self.starts = {}

    def poll(self):
        receipts = []
        for path in self.study.glob("task-*/solve*/jobs/*/log.jsonl"):
            stat = path.stat()
            offset = self.offsets.get(path, 0)
            if stat.st_mtime < self.since - 1 or stat.st_size == offset:
                continue
            with path.open("rb") as stream:
                stream.seek(offset)
                for line in stream:
                    if not line.endswith(b"\n"):
                        break
                    self.offsets[path] = stream.tell()
                    for event in json.loads(line)["events"]:
                        payload = event["payload"]
                        key = (path, payload.get("request_id"))
                        if event["type"] == "model.request_started" and payload["actor_role"] == self.role:
                            self.starts[key] = datetime.fromisoformat(event["timestamp_utc"]).timestamp()
                        if event["type"] != "model.request_finished" or key not in self.starts:
                            continue
                        started = self.starts.pop(key)
                        if started < self.since or payload["delivery_status"] == "not_submitted":
                            continue
                        finished = datetime.fromisoformat(event["timestamp_utc"]).timestamp()
                        code, retry = None, 0
                        if payload["status"] == "error" and payload.get("response_ref"):
                            error = json.loads((path.parent / payload["response_ref"]["path"]).read_bytes()).get("error", {})
                            value = str(error.get("code", "")).removeprefix("HTTP_")
                            code = int(value) if value.isdigit() else None
                            retry = float(error.get("retry_after_seconds") or 0)
                            if error.get("retry_after_utc"):
                                retry = max(retry, datetime.fromisoformat(error["retry_after_utc"]).timestamp() - finished)
                        receipts.append(dict(start=started, finish=finished, request=key[1], status=payload["status"],
                                             code=code, retry=retry))
        return sorted(receipts, key=lambda r: r["finish"])


def descriptors(proc):
    targets = []
    try:
        entries = list((proc / "fd").iterdir())
    except FileNotFoundError:
        return targets
    for fd in entries:
        try:
            targets.append(fd.readlink())
        except FileNotFoundError:
            pass
    return targets


def process_family(controller):
    table = {}
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            continue
        table[int(proc.name)] = (int(fields[1]), int(fields[19]))
    family = {controller}
    while True:
        children = {pid for pid, (parent, _) in table.items() if parent in family}
        if children <= family:
            break
        family.update(children)
    return dict(controller=controller, process_starts={str(pid): table[pid][1] for pid in sorted(family)})


class RegisteredPool:
    def __init__(self, study, recorded, *, held):
        self.study = study
        self.controller = recorded["controller"]
        self.handles = {}
        if study not in descriptors(Path("/proc") / str(self.controller)):
            raise ValueError("The controller does not hold the registered study directory")
        self.attach(recorded, held=held)

    def attach(self, recorded, *, held):
        parents = {}
        for key, tick in recorded["process_starts"].items():
            pid = int(key)
            proc = Path("/proc") / key
            if pid not in self.handles:
                self.handles[pid] = os.pidfd_open(pid)
            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[19]) != tick or (held and fields[0] not in {"T", "t"}):
                raise ValueError("The recorded process identity or initial hold changed")
            parents[pid] = int(fields[1])
        for pid in parents:
            current = pid
            while current != self.controller:
                if current not in parents:
                    raise ValueError("Unrelated process in the operator hold")
                current = parents[current]

    def hold(self):
        # Freeze admissions before finding workers that started after the monitor attached.
        self.send([self.controller], signal.SIGSTOP)
        self.attach(process_family(self.controller), held=False)
        self.send(self.handles, signal.SIGSTOP)

    def send(self, which, sig):
        for pid in which:
            try:
                signal.pidfd_send_signal(self.handles[pid], sig)
            except ProcessLookupError:
                continue

    def active(self):
        tasks = []
        for pid in self.handles:
            proc = Path("/proc") / str(pid)
            if pid == self.controller or not proc.exists():
                continue
            for path in descriptors(proc):
                if not path.is_relative_to(self.study):
                    continue
                parts = path.relative_to(self.study).parts
                if len(parts) < 3 or not parts[0].startswith("task-") or not parts[1].startswith("solve"):
                    continue
                stage = self.study / parts[0] / parts[1]
                alloc = stage / "allocations.json"
                if alloc.is_file() and any(r["status"] == "started" for r in json.loads(alloc.read_bytes())):
                    tasks.append((stage, pid))
                    break
        return sorted(tasks)

    def close(self):
        for handle in self.handles.values():
            os.close(handle)


def supervise(study, pool, role, report, *, held):
    def note(event, **values):
        row = dict(at=datetime.now(timezone.utc).isoformat(), event=event, **values)
        print(json.dumps(row, sort_keys=True), file=report, flush=True)

    monitor = ProviderReceipts(study, role, time.time())
    delay = (120, 600, 1800)
    streak = 0
    resume_at = 0
    probe = None
    probing_since = 0
    failures = deque()
    state = "hold" if held else "running"
    note("started", role=role, controller=pool.controller, initially_held=held)
    while not (study / "completion.json").is_file():
        if not (Path("/proc") / str(pool.controller)).exists():
            raise RuntimeError("Controller exited before completion")
        now = time.time()
        if state == "hold" and now >= resume_at:
            active = pool.active()
            if active:
                stage, pid = active[0]
                probe = stage
                probing_since = now
                pool.send([pid], signal.SIGCONT)
                state = "probe"
                note("registered_probe", stage=str(stage.relative_to(study)), worker=pid)
            else:
                # Between waves there is no active consultation. Let the existing scheduler
                # admit its next registered group under the same receipt monitor.
                state = "running"
                probing_since = now
                pool.send(pool.handles, signal.SIGCONT)
                note("resumed_at_wave_boundary")
        for rec in monitor.poll():
            if state == "hold" or rec["start"] < probing_since:
                continue
            if rec["code"] in TRANSIENT:
                failures.append(rec)
                while failures and rec["finish"] - failures[0]["finish"] > 30:
                    failures.popleft()
                if state == "probe" or len(failures) >= 2:
                    pool.hold()
                    if now - probing_since > 900:
                        streak = 0
                    seconds = max(delay[min(streak, len(delay) - 1)], *(v["retry"] for v in failures))
                    resume_at = now + seconds
                    streak += 1
                    state = "hold"
                    note("held", seconds=seconds, affected=[v["request"] for v in failures],
                         codes=[v["code"] for v in failures])
                    failures.clear()
            elif state == "probe" and rec["status"] == "response":
                failures.clear()
                pool.send(pool.handles, signal.SIGCONT)
                state = "running"
                note("resumed_after_customer_response", request=rec["request"], stage=str(probe.relative_to(study)))
        if state == "probe" and (probe / "completion.json").is_file():
            pool.hold()
            state = "hold"
            resume_at = now
            note("probe_ended_without_a_fresh_response", stage=str(probe.relative_to(study)))
        time.sleep(0.4 if state != "hold" else min(4, max(0.4, resume_at - time.time())))
    note("completed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", required=True, type=Path)
    origin = parser.add_mutually_exclusive_group(required=True)
    origin.add_argument("--controller", type=int, help="Attach to a running controller with an open study directory.")
    origin.add_argument("--held-processes", type=Path, help="Resume an operator-held tree identified by process start ticks.")
    parser.add_argument("--role", default="user_simulator")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    study = args.study.resolve()
    held = args.held_processes is not None
    recorded = json.loads(args.held_processes.read_bytes()) if held else process_family(args.controller)
    pool = RegisteredPool(study, recorded, held=held)
    try:
        with args.report.open("a") as report:
            supervise(study, pool, args.role, report, held=held)
    except BaseException:
        if not (study / "completion.json").is_file() and (Path("/proc") / str(pool.controller)).exists():
            pool.hold()
        raise
    finally:
        pool.close()


if __name__ == "__main__":
    main()
