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
