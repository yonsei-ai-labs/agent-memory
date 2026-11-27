"""Only fresh registered requests from the affected model role establish route health."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from scripts.watch_registered_provider import ProviderReceipts, RegisteredPool, TRANSIENT, process_family


class ReceiptWatchTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Uses Linux process descriptors")
    def test_active_controller_also_holds_a_later_worker(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            program = """
import os, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
fd = os.open(root, os.O_DIRECTORY)
print('ready', flush=True)
sys.stdin.readline()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
print(child.pid, flush=True)
sys.stdin.readline()
child.terminate()
child.wait()
os.close(fd)
"""
            with subprocess.Popen([sys.executable, "-c", program, str(root)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True) as proc:
                self.assertEqual(proc.stdout.readline().strip(), "ready")
                pool = RegisteredPool(root, process_family(proc.pid), held=False)
                try:
                    proc.stdin.write("\n")
                    proc.stdin.flush()
                    child = int(proc.stdout.readline())
                    pool.hold()
                    self.assertIn(child, pool.handles)
                    for _ in range(100):
                        states = [(Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0]
                                  for pid in (proc.pid, child)]
                        if states == ["T", "T"]:
                            break
                        time.sleep(0.01)
                    self.assertEqual(states, ["T", "T"])
                finally:
                    pool.send(pool.handles, signal.SIGCONT)
                    pool.close()
                    proc.stdin.write("\n")
                    proc.stdin.flush()
                self.assertEqual(proc.wait(timeout=5), 0)

    def test_stale_worker_and_partial_journal_events_do_not_release_a_probe(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = root / "task-example/solve-2/jobs/0001"
            job.mkdir(parents=True)
            log = job / "log.jsonl"
            now = datetime.now(timezone.utc)
            watcher = ProviderReceipts(root, "user_simulator", now.timestamp())

            def event(kind, seconds, request, **data):
                return dict(type=kind, timestamp_utc=(now + timedelta(seconds=seconds)).isoformat(),
                            payload=dict(request_id=request, **data))

            def start(seconds, request, role="user_simulator"):
                return event("model.request_started", seconds, request, actor_role=role)

            def finish(seconds, request, status, **data):
                return event("model.request_finished", seconds, request,
                             delivery_status="response_received", status=status, **data)

            first = [start(-1, "stale"), finish(1, "stale", "response"), start(1, "worker", "worker"),
                     finish(2, "worker", "response"), start(2, "customer"), start(3, "error")]
            log.write_text(json.dumps(dict(events=first)) + "\n")
            self.assertEqual(watcher.poll(), [])
            error = dict(code=502, retry_after_seconds=5, retry_after_utc=(now+timedelta(seconds=26)).isoformat())
            (job / "error.json").write_text(json.dumps(dict(error=error)))
            second = [finish(4, "customer", "response"), finish(6, "error", "error", response_ref=dict(path="error.json"))]
            with log.open("a") as handle:
                handle.write(json.dumps(dict(events=second)))
            self.assertEqual(watcher.poll(), [])
            with log.open("a") as handle:
                handle.write("\n")
            rows = watcher.poll()
            self.assertEqual([(r["request"], r["status"], r["code"], r["retry"]) for r in rows],
                             [("customer", "response", None, 0), ("error", "error", 502, 20)])
            self.assertTrue({429, 502, 503, 504} <= TRANSIENT)
            self.assertEqual(watcher.poll(), [])
