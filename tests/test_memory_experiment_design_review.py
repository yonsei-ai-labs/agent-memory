import sys
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.contracts.validation import ROOT

with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
    from review_memory_experiment_design import (
        customer_prefix_and_proposals,
        join_wording,
        unread_variation,
    )


class MemoryDesignReviewTests(unittest.TestCase):
    def test_mask_ends_at_first_attempt_even_when_a_later_attempt_succeeds(self):
        def booking(count):
            return {
                "role": "assistant",
                "tool_calls": [{"name": "book_reservation", "arguments": {"total_baggages": count}}],
            }

        messages = [
            {"role": "assistant", "content": "Any bags?"},
            {"role": "user", "content": "Just the free allowance."},
            booking(6),
            {"role": "tool", "content": "Rejected"},
            {"role": "user", "content": "None at all."},
            booking(0),
        ]
        self.assertEqual(customer_prefix_and_proposals(messages), (["Just the free allowance."], 2, [6, 0]))
        self.assertEqual(customer_prefix_and_proposals(messages[:2]), (["Just the free allowance."], None, []))

    def test_later_customer_quote_cannot_explain_the_first_proposal(self):
        packet = {"case": ["Just the free allowance."]}
        row = {"index": 1, "review_id": "case", "graded": False, "arm": "fact", "proposed_quantities": []}
        interventions = {"rows": [{"index": 1, "arm": "fact", "status": "ungraded"}]}
        labels = {"case": {"category": "zero_checked", "evidence": [{"utterance": 0, "quote": "None at all."}]}}
        with self.assertRaisesRegex(ContractError, "blinded public prefix"):
            join_wording(packet, [row], labels, interventions)
        packet["case"] = ["Could we finish the booking?"]
        labels["case"] = {"category": "no_quantity_statement", "evidence": []}
        result = join_wording(packet, [row], labels, interventions)
        self.assertEqual(result["wording_by_first_proposal"], {"no_quantity_statement": {"partial_no_proposal": 1}})

    def test_title_only_exposure_disqualifies_an_unread_negative_control(self):
        episode = {
            "mode": "on_demand",
            "list_requests": 0,
            "get_requests": 0,
            "list_returns": 0,
            "get_returns": 0,
            "title_spans": 1,
            "body_spans": 0,
        }
        with self.assertRaisesRegex(ContractError, "requested or received memory"):
            unread_variation([episode])
