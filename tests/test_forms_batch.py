from copy import deepcopy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from agent_memory_eval.contracts.core import ContractError, canonical, read_json
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

    def test_first_pass_mismatch_stops_remaining_assignments(self):
        rows = [dict(phase="solve", task_id="23", repetition=1, arm=a) for a in ("a", "b")]
        def execute(row, writer):
            self.assertEqual(row["arm"], "a")
            return dict(status="error", error_code="FIRST_PASS_MISMATCH", unknown_cost=False)
        guard = BatchGuard(max_calls=5, max_usd=1, max_seconds=10)
        self.assertEqual(run_jobs(rows, execute, guard, lambda _: None), "first_pass_mismatch")
        self.assertEqual(rows[1]["status"], "not_started")

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

    def test_unavailable_transfer_source_marks_its_target_slot_before_sending(self):
        rows = [dict(phase="solve", task_id="26", source_task_id="0", repetition=1, arm="lesson")]
        guard = BatchGuard(max_calls=10, max_usd=1, max_seconds=30)
        run_jobs(rows, lambda *_: self.fail("sent without source"), guard, lambda _: None, missing_sources={"0"})
        self.assertEqual((rows[0]["status"], rows[0]["reason"]), ("not_started", "first_pass_ungraded"))

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
            cases = (("charged", {"cost": 0.0001}, 200), ("unpriced", {}, 200),
                     ("charged_429", {"cost": 0.0001}, 429), ("unpriced_429", {}, 429))
            for name, usage, status in cases:
                guard = BatchGuard(max_calls=4, max_usd=1, max_seconds=10)
                error = dict(error={"message": "local fixture"}, usage=usage)
                def opener(*args, **kwargs):
                    if status == 429:
                        raise HTTPError("https://openrouter.ai", status, "rate limited", {}, BytesIO(canonical(error)))
                    return Reply(error)
                client = OpenRouterClient(model, guard, route=route, opener=opener,
                                          max_input_bytes=128000)
                with Journal(root / name, name, contract_version="1.2") as journal:
                    result = run_form_writer(journal, design, source, repetition=1, arm="fact", client=client,
                                             guard=guard, root=ROOT)
                    ledger = request_usage(journal)["writer"]
                self.assertEqual(result["error_code"], "HTTP_429" if status == 429 else "PROVIDER_RESPONSE_ERROR")
                self.assertEqual(ledger["unknown_count"], guard.unknown_cost)
                self.assertEqual(float(ledger["reported_usd"]), guard.reported_usd)
                self.assertEqual(result["unknown_cost"], "cost" not in usage)
                self.assertEqual(audit_requests(root / name)["unknown_cost_requests"], ledger["unknown_count"])
