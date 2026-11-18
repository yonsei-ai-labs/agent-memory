from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.experiments.forms_batch import BatchGuard, DESIGN, ordered_jobs, request_usage, run_jobs
from agent_memory_eval.experiments.forms_write import load_form_sources, run_form_writer
from agent_memory_eval.models.openrouter import OpenRouterClient, ProviderError
from agent_memory_eval.recording.journal import Journal
from test_forms_write import source_fixture
from test_openrouter import Reply


class FormsBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.design = read_json((ROOT / DESIGN).read_bytes())

    def test_exact_schedule_and_rejected_change(self):
        rows = ordered_jobs(self.design)
        self.assertEqual([r["phase"] for r in rows], ["write"] * 232 + ["solve"] * 352)
        altered = deepcopy(self.design)
        altered["allocation"]["blocks"][0]["solve"].reverse()
        with self.assertRaises(ContractError):
            ordered_jobs(altered)

    def test_empty_writer_runs_own_solver_and_errors_skip_only_dependents(self):
        rows = ordered_jobs(self.design)
        seen = []
        def execute(row, writer):
            if row["phase"] == "write":
                status = "error" if row["arm"] == "lesson" else "empty"
            else:
                status = "completed"
                if row["arm"] == "fact":
                    self.assertEqual(writer["status"], "empty")
                    self.assertEqual((row["task_id"], row["repetition"]), (writer["task_id"], writer["repetition"]))
            seen.append(row["index"])
            return dict(status=status, unknown_cost=False)
        guard = BatchGuard(max_calls=100, max_usd=1, max_seconds=300)
        self.assertIsNone(run_jobs(rows, execute, guard, lambda _: None))
        self.assertEqual(len(seen), 584 - 56)
        self.assertEqual([r["reason"] for r in rows if r["status"] == "not_started"], ["writer_incomplete"] * 56)

    def test_unknown_cost_preserves_full_unstarted_assignment_and_useful_empty(self):
        rows = ordered_jobs(self.design)
        states = []
        def persist(arr):
            states.append((len(arr), arr[0].get("status"), arr[-1].get("status")))
        guard = BatchGuard(max_calls=100, max_usd=1, max_seconds=300)
        self.assertEqual(run_jobs(rows, lambda *_: dict(status="error", unknown_cost=True), guard, persist), "unknown_cost")
        self.assertEqual(states[1], (584, "started", None))
        self.assertEqual(sum(r["status"] == "not_started" for r in rows), 583)
        self.assertEqual(rows[-1]["reason"], "unknown_cost")

    def test_reservation_denial_below_cap_stops_weaker_later_reservations(self):
        guard = BatchGuard(max_calls=10, max_usd=1, max_seconds=100)
        with self.assertRaises(ProviderError):
            guard.before(dict(messages=[], max_tokens=100), dict(pricing=dict(prompt=1, completion=1)))
        self.assertEqual(guard.stop(), "spend_limit")
        self.assertEqual(guard.calls, 0)

    def test_charged_error_envelope_and_unpriced_error_reconcile_with_transport(self):
        with TemporaryDirectory() as folder, patch.dict("os.environ", OPENROUTER_API_KEY="local-test-transport"):
            root = Path(folder)
            design = source_fixture(root)
            source = load_form_sources(design, root)["3"]
            route = dict(only=["openai"], allow_fallbacks=False, require_parameters=True)
            model = dict(id="openai/gpt-6-luna", selected_endpoint=dict(provider_name="OpenAI"),
                         pricing=dict(prompt=0, completion=0))
            for name, usage in (("charged", {"cost": 0.0001}), ("unpriced", {})):
                guard = BatchGuard(max_calls=4, max_usd=1, max_seconds=10)
                error = dict(error={"message": "local fixture"}, usage=usage)
                client = OpenRouterClient(model, guard, route=route, opener=lambda *args, **kwargs: Reply(error),
                                          max_input_bytes=128000)
                with Journal(root / name, name, contract_version="1.2") as journal:
                    result = run_form_writer(journal, design, source, repetition=1, arm="fact", client=client,
                                             guard=guard, root=ROOT)
                    ledger = request_usage(journal)["writer"]
                self.assertEqual(result["error_code"], "PROVIDER_RESPONSE_ERROR")
                self.assertEqual(ledger["unknown_count"], guard.unknown_cost)
                self.assertEqual(float(ledger["reported_usd"]), guard.reported_usd)
                self.assertEqual(result["unknown_cost"], name == "unpriced")
                self.assertEqual(audit_requests(root / name)["unknown_cost_requests"], ledger["unknown_count"])
