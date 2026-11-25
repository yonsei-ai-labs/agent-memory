import json
import unittest
from types import SimpleNamespace

from agent_memory_eval.adapters.banking_dispute_evidence import public_opportunity


class PublicDisputeEvidenceTest(unittest.TestCase):
    def fixture(self, *, first="Please check", result="txn_a", document="card-one", rate="2%", submission=""):
        expected = [dict(name="call_discoverable_user_tool", requestor="user", arguments={
            "discoverable_tool_name": "submit_cash_back_dispute_0589",
            "arguments": json.dumps(dict(transaction_id=k, user_id="c"))}) for k in ("txn_a", "txn_b")]
        task = dict(required_documents=["card-one"], evaluation_criteria=dict(actions=expected))
        messages = [dict(role="assistant", content="Hello"), dict(role="user", content=first)]
        for i, (name, value) in enumerate([("lookup", result), ("grep", f"1. Title\n   ID: {document}\n   Score: 1.0\n   Content: {rate} {submission}")]):
            messages.extend([dict(role="assistant", tool_calls=[dict(id=str(i), function=dict(name=name))]),
                             dict(role="tool", tool_call_id=str(i), content=value)])
        source = SimpleNamespace(public_context=json.dumps(dict(task_context={}, messages=messages)),
                                 source_run_id="source", public_context_sha256="public")
        return public_opportunity(task, source)

    def test_both_witnesses_must_be_public_and_new(self):
        self.assertTrue(self.fixture()["eligible"])
        self.assertFalse(self.fixture(first="check txn_a")["eligible"])
        self.assertFalse(self.fixture(result="txn_bogus", document="unrelated")["eligible"])
        self.assertFalse(self.fixture(document="another-card")["eligible"])
        self.assertFalse(self.fixture(rate="cash back")["eligible"])
        self.assertTrue(self.fixture(document="general", rate="", submission="submit_cash_back_dispute_0589")["eligible"])

    def test_ungraded_sources_do_not_release_readers(self):
        self.assertFalse(public_opportunity({}, None)["eligible"])


class FormRepeatControlsTest(unittest.TestCase):
    def test_nonpercentage_nonmatching_document_does_not_change_the_registered_gate(self):
        self.assertFalse(PublicDisputeEvidenceTest().fixture(rate="5 points per dollar")["eligible"])

    def test_public_merchant_and_amount_must_resolve_to_one_transaction(self):
        from agent_memory_eval.adapters.banking_dispute_evidence import merchant_amount_mentions
        labels = {"12": dict(merchant_name="Market", transaction_amount="$1,230.00"),
                  "13": dict(merchant_name="Market", transaction_amount="$1.23"),
                  "14": dict(merchant_name="Market", transaction_amount="$1.23")}
        self.assertEqual(merchant_amount_mentions("Market paid $1230, then Market ($1.23).", labels),
                         {"12": "Market paid $1230"})
        self.assertFalse(merchant_amount_mentions("Aftermarket $1230", labels))
        self.assertFalse(merchant_amount_mentions("Market " + "x" * 70 + "$1230", labels))

    def test_equal_bank_repeats_share_their_reference_source(self):
        from agent_memory_eval.analysis.form_repeats import summarize_form_repeats
        from agent_memory_eval.adapters.tau_banking_results import BANKING_SUCCESS
        from agent_memory_eval.contracts.core import ContractError
        first = {"T": dict(run="one", started=True, success=False)}
        readers = [dict(task="T", form="fact", repeat=i, started=True, success=i == 1) for i in (1, 2)]
        def summary(rows):
            return summarize_form_repeats(first, rows, ["fact"], 2, metric=BANKING_SUCCESS, cohort="study")["fact"]
        report = summary(readers)
        self.assertEqual(report["same_bank_discordant_pairs"], 1)
        self.assertEqual(report["comparison"]["sources"]["included"], 1)
        self.assertEqual(report["comparison"]["sources"]["two_sided_sign_p"], 1.0)
        self.assertEqual(summary([readers[0], dict(readers[1], success=None)])["same_bank_complete_pairs"], 0)
        with self.assertRaises(ContractError):
            summary([readers[0], readers[0]])

    def test_excluded_shard_has_zero_http_and_no_job_directory(self):
        import os
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from scripts import run_full_retail as study
        cfg = dict(name="gate-test", stages=["solve"], max_concurrent=1, total_usd_limit="1")
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"OPENROUTER_API_KEY": "offline-placeholder"}), \
                patch.object(study, "implementation_identity", return_value={"test": True}):
            workspace = Path(tmp)
            study.run_study(workspace, cfg, {}, {}, [dict(task="task")], {"task":{}}, {},
                            model_resolver=lambda *_: ({}, None), stage_admission=lambda *_: "absent_clue")
            root = workspace / "runs/gate-test"
            entry = json.loads((root / "completion.json").read_bytes())["stages"]["task-task/solve"]
            self.assertEqual((entry["reason"], entry["http"], entry["spent"]), ("absent_clue", 0, "0"))
            self.assertFalse((root / "task-task/solve").exists())

    def test_parameter_rejections_are_distinct_from_unconfirmed_receipts(self):
        from agent_memory_eval.adapters.tau_banking_results import dispute_submissions
        args = dict(discoverable_tool_name="submit_cash_back_dispute_0589", arguments="{}")
        call = dict(id="one", name="call_discoverable_user_tool", arguments=args)
        request = dict(role="user", tool_calls=[call])
        for text, expected in [("Error: Missing required parameter: user_id", False),
                               ("Error: Unexpected parameter: context", False), ("Unrecognized response", None)]:
            receipt = dict(role="tool", id="one", requestor="user", execution_status="executed", error=False, content=text)
            self.assertIsNone(dispute_submissions([request, receipt])[0][2])
            self.assertIs(dispute_submissions([request, receipt], native_argument_errors=True)[0][2], expected)
        self.assertIsNone(dispute_submissions([request], native_argument_errors=True)[0][2])
