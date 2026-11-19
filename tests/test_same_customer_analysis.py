import sys
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.contracts.validation import ROOT

with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
    from analyze_same_customer_transfer import inspect_episode, masked_case


class ServiceOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "r", "name": "get_reservation_details", "arguments": {"reservation_id": "3RK2T9"}}
                ],
            },
            {"role": "tool", "id": "r", "content": "{}", "error": False},
            {"role": "assistant", "content": "This was booked May 2, outside the cancellation window."},
        ]
        self.label = {"grounded_denial": [2, "outside the cancellation window"], "unsupported_promise": None}

    def score(self):
        return inspect_episode(
            {"task_id": "48"}, self.messages, {"actions": []}, self.label, masked_case("48", self.messages)
        )

    def test_service_requires_completed_current_read_before_explanation(self):
        self.assertTrue(self.score()["service"])
        self.messages[1]["error"] = True
        self.assertFalse(self.score()["service"])
        self.messages[1]["error"] = False
        self.messages[0]["tool_calls"][0]["arguments"]["reservation_id"] = "JMO1MG"
        self.assertFalse(self.score()["service"])
        self.assertTrue(self.score()["old_read_executed"])

    def test_transfer_summary_cannot_count_as_customer_facing_explanation(self):
        self.messages[2] = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "t",
                    "name": "transfer_to_human_agents",
                    "arguments": {"summary": "Booked outside the cancellation window."},
                }
            ],
        }
        with self.assertRaisesRegex(ContractError, "missing speaker"):
            self.score()
        self.label["grounded_denial"] = None
        self.assertFalse(self.score()["service"])
