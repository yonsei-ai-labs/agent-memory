from decimal import Decimal
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments import forms_batch as batch
from agent_memory_eval.experiments.batch_recovery import POLICY, stage_lease
from agent_memory_eval.experiments.forms_write import load_form_sources
from agent_memory_eval.experiments.fresh_forms import CostReservations
from agent_memory_eval.models.openrouter import OpenRouterClient
from scripts.run_full_retail import accounting
from scripts.run_recheck_rounds import sealed_rows
from test_forms_write import source_fixture
from test_openrouter import MODEL, Reply, response


class BatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.folder = self.workspace / "batch"
        self.design = source_fixture(self.workspace)
        cfg = read_json((ROOT / self.design["model"]["connection_profile_ref"]["path"]).read_bytes())
        names = ("fact", "lesson", "procedure")
        self.plan = dict(connection=cfg, summary={}, sources=load_form_sources(self.design, self.workspace),
                         jobs=[dict(index=i, task_id="3", repetition=1, phase="write", arm=arm)
                               for i, arm in enumerate(names, 1)])
        self.profile = dict(interruption_policy=POLICY, guards=dict(max_calls=10, max_usd=1, max_seconds=300),
                            http_timeout_seconds=15)
        self.model = dict(MODEL, selected_endpoint=dict(tag="openai", provider_name="OpenAI", pricing=MODEL["pricing"]),
                          reservation_pricing=MODEL["pricing"])
        self.calls = 0
        self.drop_at = None
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", OPENROUTER_API_KEY="offline-test-only").start()
        patch.object(batch, "OpenRouterClient", partial(OpenRouterClient, opener=self.opener)).start()

    def opener(self, request, timeout):
        self.calls += 1
        if self.calls == self.drop_at:
            raise KeyboardInterrupt("interrupted while awaiting the HTTP response")
        return Reply(response(cost=0.003))

    def execute(self):
        guard = batch.BatchGuard(**self.profile["guards"])
        models = {"worker": self.model}
        reserve = CostReservations(self.folder, dict(codes=[503], maximum_per_stage=3),
                                   guard, models)
        return batch.execute_prepared(self.profile, ROOT, self.folder, {}, self.design, self.plan,
                                      models, guard, close_cost=reserve)

    def test_closed_journal_without_ledger_receipt_is_recovered_once(self):
        write = batch.write_json

        def stop_before_receipt(path, rows):
            if path.name == "allocations.json" and rows[1]["status"] == "empty":
                raise KeyboardInterrupt("a closed writer has not yet reached the allocation ledger")
            write(path, rows)

        with patch.object(batch, "write_json", stop_before_receipt), self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.assertEqual(self.calls, 2)
        original = read_json((self.folder / "allocations.json").read_bytes())
        self.assertEqual([r["status"] for r in original], ["empty", "started", "planned"])
        logs = [(self.folder / r["directory"] / "log.jsonl").read_bytes() for r in original[:2]]
        self.execute()
        close, _, final = sealed_rows(self.folder)
        self.assertEqual([r["run_id"] for r in final], [r["run_id"] for r in original])
        self.assertEqual([r["status"] for r in final], ["empty"] * 3)
        self.assertEqual((self.calls, close["http_calls"], Decimal(str(close["reported_usd"]))), (3, 3, Decimal("0.009")))
        self.assertEqual(logs, [(self.folder / r["directory"] / "log.jsonl").read_bytes() for r in final[:2]])
        # Simulate the separate failure window after the final allocation and before the shard seal.
        (self.folder / "completion.json").unlink()
        self.execute()
        self.assertEqual(self.calls, 3)
        self.assertEqual(sealed_rows(self.folder)[2], final)

    def test_pending_request_is_never_resent_and_full_grant_remains_covered(self):
        self.drop_at = 2
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.execute()
        close, _, rows = sealed_rows(self.folder)
        cost = accounting(self.folder, "1")
        self.assertEqual((self.calls, close["http_calls"]), (2, 2))
        self.assertEqual([r["status"] for r in rows], ["empty", "interrupted", "not_started"])
        self.assertEqual(rows[2]["reason"], "unknown_cost")
        self.assertEqual(len(rows[1]["result"]["pending_request_ids"]), 1)
        self.assertEqual((Decimal(cost["reported"]), Decimal(cost["held"])), (Decimal(".003"), Decimal(".997")))
        (self.folder / "completion.json").unlink()
        self.execute()
        self.assertEqual((self.calls, sealed_rows(self.folder)[2]), (2, rows))

    def test_unstarted_evidence_and_registration_mismatch_block_dispatch(self):
        self.drop_at = 1
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        with patch.dict(self.profile, http_timeout_seconds=20), self.assertRaises(ContractError):
            self.execute()
        (self.folder / "jobs/0002").mkdir()
        with self.assertRaises(ContractError):
            self.execute()
        self.assertEqual(self.calls, 1)

    def test_active_worker_remains_exclusive(self):
        with stage_lease(self.folder), self.assertRaises(ContractError):
            with stage_lease(self.folder):
                self.fail("simultaneous stage entry")
