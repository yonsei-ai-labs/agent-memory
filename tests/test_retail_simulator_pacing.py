import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, Request, build_opener
from urllib.response import addinfourl

from agent_memory_eval.models.openrouter import API_ROOT
from scripts.pace_retail_transfer_revision import (
    SharedPacer,
    SimulatorPacingHandler,
    retry_seconds,
)


class RetailSimulatorPacingTests(unittest.TestCase):
    def test_fresh_urllib_request_passes_once_and_two_instances_share_the_deadline(self):
        now = [100.0]
        calls = []
        statuses = [200, 429, 200, 200]

        class Fixture(HTTPSHandler):
            def https_open(self, request):
                calls.append((request, request.data, now[0]))
                headers = Message()
                headers["Retry-After"] = "31"
                status = statuses[len(calls) - 1]
                response = addinfourl(io.BytesIO(b"{}"), headers, request.full_url, status)
                response.msg = "fixture"
                return response

        def advance(seconds):
            now[0] += seconds

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)

            def opened():
                pacer = SharedPacer(path, now=lambda: now[0], sleep=advance)
                return build_opener(Fixture(), SimulatorPacingHandler("sim", pacer))

            models = ["sim", "sim", "worker", "sim"]
            for i, model in enumerate(models):
                body = json.dumps(
                    {"model": model, "messages": [{"role": "user", "content": "same"}]}
                ).encode()
                req = Request(API_ROOT + "/chat/completions", data=body, method="POST")
                if i == 1:
                    with self.assertRaises(HTTPError) as receipt:
                        opened().open(req)
                    self.assertEqual(receipt.exception.code, 429)
                else:
                    with opened().open(req) as result:
                        self.assertEqual(result.read(), b"{}")
                self.assertIs(calls[-1][0], req)
                self.assertEqual(calls[-1][1], body)
            self.assertEqual([c[2] for c in calls], [100, 104, 104, 135])
            state = json.loads((path / "pacing-state.json").read_text())
            self.assertEqual(
                (state["sent"], state["responses"], state["limited"], state["gap_ms"]), (3, 3, 1, 8000)
            )

    def test_response_streak_and_http_date(self):
        with tempfile.TemporaryDirectory() as folder:
            p = SharedPacer(Path(folder), now=lambda: 0)
            for _ in range(64):
                p.update(200)
            self.assertEqual(json.loads((Path(folder) / "pacing-state.json").read_text())["gap_ms"], 3000)
        self.assertEqual(retry_seconds("Thu, 01 Jan 1970 00:01:00 GMT", 10), 50)
        self.assertEqual(retry_seconds(None, 1), 30)
