"""Independent role caps and input-byte rejection before any transport call."""

from tempfile import TemporaryDirectory
import unittest
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import ContractError, RunContext
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.recording.provenance import InputSizeLimit, RecordingGateway, RoleRequestLimit, SourceRegistry


class Client:
    offline = True
    def __init__(self):
        self.calls = 0
    def respond(self, wire):
        self.calls += 1
        return dict(choices=[dict(message=dict(role="assistant", content="Fixture response"))])


class ExecutionLimitTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.journal = Journal(self.folder.name, "role-limits")
        self.addCleanup(self.journal.close)
        self.ctx = RunContext("role-limits", "run", "session", "fixture", "diagnostic", "solve")
        task = self.journal.blobs.put(dict(role="user", content="Fixture task"))
        self.journal.append([self.ctx.at("prepare").event("run.started", dict(bank_id=None, start_snapshot_id=None,
            fresh_session=True, source_run_ids=[], task_ref=task))])
        self.registry, self.client = SourceRegistry(self.journal, self.ctx), Client()

    def call(self, actor, limits):
        template = self.registry.template(dict(model="synthetic-offline", system=dict(role="system", content="fixture")),
                                          roles=(actor,))
        return RecordingGateway(self.journal).call(self.ctx, self.registry, actor, template,
            [(template, "/system")], self.client, max_requests=None, execution_limits=limits)

    def end(self):
        self.journal.append([self.ctx.at("seal").event("run.ended", dict(termination_reason="fixture",
            grading_status="not_available", end_snapshot_id=None))])

    def test_user_requests_do_not_consume_worker_allowance(self):
        root = Path(__file__).resolve().parents[1]
        fixture = json.loads((root / "tests/fixtures/execution-limits-v1.json").read_text())
        schema = json.loads((root / "schemas/execution-limits.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture["limits"])
        limits = ExecutionLimits(**fixture["limits"])
        self.call("user_simulator", limits)
        self.call("user_simulator", limits)
        self.call("worker", limits)
        self.call("writer", limits)
        for actor in ("worker", "writer", "user_simulator"):
            with self.subTest(actor=actor), self.assertRaises(RoleRequestLimit):
                self.call(actor, limits)
        self.assertEqual(self.client.calls, fixture["expected_calls"])
        self.end()
        self.assertEqual(audit_requests(self.folder.name)["requests_by_role"], fixture["expected_requests_by_role"])

    def test_oversize_request_retains_evidence_without_submission(self):
        limits = ExecutionLimits(worker_requests=1, writer_requests=0, user_simulator_requests=0, max_input_bytes=1)
        with self.assertRaises(InputSizeLimit):
            self.call("worker", limits)
        self.assertEqual(self.client.calls, 0)
        finish = next(e["payload"] for e in self.journal.events.values() if e["type"] == "model.request_finished")
        self.assertEqual((finish["delivery_status"], finish["cost_status"]), ("not_submitted", "not_applicable"))
        self.assertIsNone(finish["response_ref"])
        self.end()
        self.assertEqual(audit_requests(self.folder.name)["unknown_cost_requests"], 0)

    def test_limits_have_no_silent_role_or_input_defaults(self):
        for args in ((True, 1, 1, 1), (-1, 1, 1, 1), (1, None, 1, 1), (1, 1, 1, 0)):
            with self.subTest(args=args), self.assertRaises(ContractError):
                ExecutionLimits(*args)
        with self.assertRaises(ContractError):
            ExecutionLimits(1, 1, 1, 1000).cap("unknown")


if __name__ == "__main__":
    unittest.main()
