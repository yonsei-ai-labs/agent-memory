"""Only fresh registered requests from the affected model role establish route health."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.watch_registered_provider import ProviderReceipts, TRANSIENT


class ReceiptWatchTests(unittest.TestCase):
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
