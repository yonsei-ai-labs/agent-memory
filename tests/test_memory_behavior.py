"""Bound choice evidence to its request and report resource changes only on observable pairs."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_memory_eval.analysis.efficiency import archived_effort, episode_effort, paired_effort
from agent_memory_eval.analysis.selection import first_body_choice, worker_choices
from agent_memory_eval.contracts.core import ContractError, read_json
from agent_memory_eval.contracts.validation import ROOT


class BehaviorTests(unittest.TestCase):
    def test_outcome_and_metric_denominators_and_zero_reference(self):
        pairs = [(True, True, {"tokens": 8, "calls": 0}, {"tokens": 10, "calls": 0}),
                 (True, True, {}, {"tokens": 9}), (True, False, {"tokens": 16}, {"tokens": 8}),
                 (None, True, {"tokens": 1}, {"tokens": 20})]
        report = paired_effort(pairs, ("tokens", "calls"))
        self.assertEqual((report["outcomes"]["assigned"], report["outcomes"]["missing"]), (4, 1))
        successful = report["both_success"]
        self.assertEqual(successful["pairs"], 2)
        self.assertEqual(successful["metrics"]["tokens"], {"observed": 1, "missing": 1, "candidate_total": "8",
            "reference_total": "10", "difference": "-2", "relative_difference_pct": -20, "lower": 1, "equal": 0, "higher": 0})
        self.assertIsNone(successful["metrics"]["calls"]["relative_difference_pct"])
        self.assertEqual(report["jointly_graded"]["metrics"]["tokens"]["difference"], "6")
        self.assertIsNone(paired_effort([], ("tokens",))["jointly_graded"]["metrics"]["tokens"]["difference"])

    def test_unknown_usage_does_not_hide_observed_actions_or_become_free(self):
        usage = dict(http_attempts=3, unknown_token_requests=1, unknown_cost_requests=1, reported_tokens=10,
                     reported_cost_usd_decimal="0.01")
        row = {"result": {"grading_status": "graded", "usage_by_role": {"worker": usage, "grader": usage}}}
        turns = [dict(role="assistant", tool_calls=[{"name": "place_order"}, {"name": "memory_get"}]),
                 dict(role="user", content="Yes"), dict(role="user", content="###STOP###")]
        choices = [dict(status="response", delivery="response_received", reads=[{}]),
                   dict(status="error", delivery="delivery_unknown", reads=[])]
        result = episode_effort(row, turns, choices)
        self.assertEqual(result, dict(http_calls=3, tokens=None, usd=None, business_calls=1, customer_turns=1,
                                      memory_read_calls=None))
        with TemporaryDirectory() as tmp:
            self.assertEqual(archived_effort(dict(root="missing", log_sha256="abc"), Path(tmp), {})["status"], "unavailable")

    def test_unlisted_id_and_interrupted_lookup_do_not_imply_a_visible_choice(self):
        unknown = dict(status="timeout", delivery="delivery_unknown", reads=[])
        read = dict(name="memory_get", requested_item_id="guessed", visible_title_indices=[], returned=None)
        selection = dict(status="response", delivery="response_received", request_id="selected",
                         visible_memory=[], reads=[read])
        self.assertIsNone(first_body_choice(dict(choices=[unknown]))["get_requested"])
        first = first_body_choice(dict(choices=[unknown, selection]))
        self.assertFalse(first["earlier_responses_observed"])
        self.assertTrue(first["get_requested"])
        self.assertEqual(first["matched_list_entries"], [])
        self.assertIsNone(first["returned_status"])

    def test_lookup_follows_titles_and_precedes_body_and_action(self):
        spec = read_json((ROOT / "protocols/diagnostics/memory-claim-citations-v1.json").read_bytes())
        case = spec["cases"][0]
        folder = (ROOT / case["worker"]["path"]).parent
        if not (folder / "log.jsonl").exists():
            self.skipTest("The archived choice journal lives outside Git")
        trace = worker_choices(folder, through_request=case["request_id"])
        opening, reading, action = trace["choices"]
        self.assertEqual(opening["visible_memory"], [])
        self.assertIn("memory_list", opening["available_reads"])
        self.assertEqual([r["name"] for r in opening["reads"]], ["memory_list"])
        self.assertEqual([(f["field"], f["position_on_page"]) for f in reading["visible_memory"]],
                         [("title", 1), ("title", 2), ("title", 3)])
        self.assertEqual(reading["reads"][0]["visible_title_indices"], [0])
        self.assertEqual(trace["first_body_choice"]["matched_list_entries"][0]["size"], 3)
        self.assertTrue(any("last time" in i.get("text", "") for i in reading["public_inputs"]))
        self.assertTrue(any(i.get("tool") == "get_user_details" for i in reading["public_inputs"]))
        self.assertTrue(any(f["item_id"] == case["item"] and f["field"] == "body" for f in action["visible_memory"]))
        self.assertEqual(action["response"]["tool_calls"][0]["function"]["name"], "get_order_details")
        self.assertNotIn("reasoning", action["response"])
        early = worker_choices(folder, through_request=opening["request_id"])
        self.assertFalse(early["first_body_choice"]["get_requested"])
        self.assertIsNone(first_body_choice({"choices": []})["get_requested"])
        with self.assertRaises(ContractError):
            worker_choices(folder, run_id=trace["run_id"], through_request="nonexistent")
