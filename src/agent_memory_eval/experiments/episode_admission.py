"""Pause new whole episodes after routing errors and admit one untouched allocation to test recovery."""

import fcntl
import time
from datetime import datetime
from pathlib import Path

from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.journal import BlobStore, safe_path


class EpisodeAdmission:
    def __init__(self, root, settings, shard=None, *, now=time.time, sleep=time.sleep):
        self.root = Path(root)
        self.settings = settings
        self.shard = shard
        self.now = now
        self.sleep = sleep

    def retry_after(self, row, moment):
        if self.shard is None:
            return 0
        folder = safe_path(self.shard, row["directory"])
        blobs = BlobStore(folder)
        for line in reversed((folder / "log.jsonl").read_bytes().splitlines()):
            for event in reversed(read_json(line)["events"]):
                data = event["payload"]
                if event["type"] != "model.request_finished" or not data.get(
                    "response_ref"
                ):
                    continue
                error = blobs.get(data["response_ref"]).get("error", {})
                if "retry_after_seconds" in error:
                    return error["retry_after_seconds"]
                if "retry_after_utc" in error:
                    return max(
                        0,
                        datetime.fromisoformat(error["retry_after_utc"]).timestamp()
                        - moment,
                    )
                return 0
        return 0

    def run(self, execute, row, writer):
        lock = self.root / "admission.lock"
        path = self.root / "admission.json"
        with lock.open("a+b") as stream:
            while True:
                fcntl.flock(stream, fcntl.LOCK_EX)
                state = (
                    read_json(path.read_bytes())
                    if path.exists()
                    else {"failures": 1, "until": 0, "events": []}
                )
                if state["failures"] == 0:
                    fcntl.flock(stream, fcntl.LOCK_UN)
                    probe = False
                    break
                delay = state["until"] - self.now()
                if delay <= 0:
                    # The lock spans just this recovery episode. Other episodes wait before their native clock starts.
                    probe = True
                    break
                fcntl.flock(stream, fcntl.LOCK_UN)
                self.sleep(min(delay, 30))
            result = execute(row, writer)
            code = result.get("error_code")
            if not probe:
                if code not in self.settings["codes"]:
                    return result
                fcntl.flock(stream, fcntl.LOCK_EX)
                state = read_json(path.read_bytes())
                if state["failures"]:
                    return result  # Another in-flight episode has already closed admission.
            failed = code in self.settings["codes"]
            count = state["failures"] + 1 if failed and probe else int(failed)
            intervals = self.settings["seconds"]
            delay = intervals[min(count - 1, len(intervals) - 1)] if count else 0
            moment = self.now()
            if failed:
                delay = max(delay, self.retry_after(row, moment))
            state.update(failures=count, until=moment + delay)
            state["events"].append(
                {
                    "at": moment,
                    "run": row["run_id"],
                    "probe": probe,
                    "routing_error": code if failed else None,
                    "wait_seconds": delay,
                }
            )
            write_json(path, state)
            return result
