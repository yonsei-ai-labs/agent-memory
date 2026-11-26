from collections import Counter
from decimal import Decimal
from functools import partial
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from agent_memory_eval.contracts.core import ContractError, canonical, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments import forms_batch as batch
from agent_memory_eval.experiments.batch_recovery import POLICY, _restore_result, stage_lease
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
        self.unpriced_at = None
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", OPENROUTER_API_KEY="offline-test-only").start()
        patch.object(batch, "OpenRouterClient", partial(OpenRouterClient, opener=self.opener)).start()

    def opener(self, request, timeout):
        self.calls += 1
        if self.calls == self.drop_at:
            raise KeyboardInterrupt("interrupted while awaiting the HTTP response")
        if self.calls == self.unpriced_at:
            body = canonical(dict(error=dict(code=503, message="fixture unavailable")))
            raise HTTPError("https://fixture.invalid", 503, "Unavailable", {}, BytesIO(body))
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
        self.assertEqual(close["requests_with_unconfirmed_submission"], 1)
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

    def test_started_slot_without_a_journal_keeps_the_grant_reserved(self):
        write = batch.write_json

        def stop_after_start(path, rows):
            write(path, rows)
            if path.name == "allocations.json" and rows[0]["status"] == "started":
                raise KeyboardInterrupt("no durable request log")

        with patch.object(batch, "write_json", stop_after_start), self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.execute()
        self.assertEqual(self.calls, 0)
        self.assertEqual(Decimal(accounting(self.folder, "1")["held"]), 1)

    def test_resuming_reconciles_a_bounded_provider_error_only_once(self):
        self.unpriced_at = 2
        write = batch.write_json

        def stop_after_error(path, rows):
            if path.name == "allocations.json" and rows[1]["status"] == "error":
                write(path, rows)
                raise KeyboardInterrupt("cost reservation and job are already recorded")
            write(path, rows)

        with patch.object(batch, "write_json", stop_after_error), self.assertRaises(KeyboardInterrupt):
            self.execute()
        before = read_json((self.folder / "allocations.json").read_bytes())
        held = Decimal(before[1]["result"]["unpriced_reservation"]["usd"])
        self.assertGreater(held, 0)
        self.execute()
        close, _, rows = sealed_rows(self.folder)
        self.assertEqual(rows[1], before[1])
        self.assertEqual((self.calls, Decimal(close["reserved_usd"]), Decimal(close["reported_usd_decimal"])),
                         (3, held, Decimal(".006")))


class ArchivedBankingRecoveryTests(unittest.TestCase):
    def test_closed_banking_journals_reproduce_preexisting_results(self):
        studies = [
            "tau-banking-memory-01", "tau-banking-required-01", "tau-banking-result-feedback-01",
            "tau-banking-policy-check-sources-01", "tau-banking-policy-check-01",
            "tau-banking-scoped-writers-01", "tau-banking-replacement-sources-01",
            "tau-banking-multitarget-forms-01",
        ]
        roots = [ROOT / "runs" / study for study in studies]
        if not all((root / "registration.json").is_file() for root in roots):
            self.skipTest("Local historical banking records are unavailable")
        counts = Counter()
        for root in roots:
            for ledger in sorted(root.glob("task-*/*/allocations.json")):
                stage = ledger.parent
                records = sealed_rows(stage)[2] if (stage / "completion.json").exists() else read_json(ledger.read_bytes())
                for row in records:
                    expected = row.get("result") or {}
                    if not expected.get("log_sha256"):
                        continue
                    with self.subTest(study=root.name, run=row["run_id"]):
                        interrupted = {k: v for k, v in row.items() if k != "result"}
                        interrupted["status"] = "started"
                        restored = _restore_result(stage / row["directory"], interrupted)
                        self.assertEqual(restored, {k: v for k, v in expected.items() if k != "unpriced_reservation"})
                    counts[stage.name.split("-repeat-")[0]] += 1
        self.assertEqual(counts, {"source": 13, "write": 140, "solve": 189})
