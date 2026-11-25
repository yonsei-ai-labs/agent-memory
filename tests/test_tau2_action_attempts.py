from copy import deepcopy
from importlib.util import find_spec
import unittest
from unittest.mock import patch

from agent_memory_eval.adapters.tau2_assessment import action_attempt_counts, single_action_attempts
from agent_memory_eval.analysis.prompt_report import _grade_evidence, _grade_section
from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.contracts.validation import ROOT


def example_report():
    action = dict(action_id="handoff", name="transfer", requestor="assistant",
                  arguments={"reason": "verified", "summary": ""}, compare_args=["reason"])
    def call(name, reason):
        return dict(name=name, arguments={"reason": reason, "summary": "free text"})
    return dict(reward_info=dict(reward=1, reward_basis=["ACTION"],
                                 action_checks=[dict(action=action, action_match=True)]),
                messages=[dict(role="assistant", tool_calls=[call("lookup", "verified")]),
                          dict(role="assistant", tool_calls=[call("transfer", "other"), call("transfer", "verified")])])


class ActionAttemptTests(unittest.TestCase):
    def test_non_action_grades_and_unassessed_allocations(self):
        report = example_report()
        report["reward_info"]["reward_basis"] = ["DB"]
        self.assertIsNone(single_action_attempts(report))
        report["reward_info"]["reward_basis"] = ["ACTION"]
        report["reward_info"]["action_checks"] *= 2
        self.assertIsNone(single_action_attempts(report))
        counts = action_attempt_counts([dict(official_success=False, action=None), dict(official_success=None, action=None)])
        self.assertEqual((counts["assigned"], counts["graded"], counts["assessed_consultations"], counts["no_attempt"]),
                         (2, 1, 0, 0))

    def test_an_offline_report_without_native_extra_keeps_the_official_grade(self):
        report = example_report()
        with patch("agent_memory_eval.analysis.prompt_report.find_spec", return_value=None):
            grade = _grade_evidence(report, report["messages"])
        self.assertTrue(grade["reference_actions"][0]["matched"])
        self.assertIsNone(grade["single_action"])
        self.assertIn("패키지가 필요", "\n".join(_grade_section(grade)))

    @unittest.skipUnless(find_spec("tau2"), "Requires the native ACTION matcher")
    def test_first_attempt_uses_native_argument_subset_and_recorded_call_order(self):
        report = example_report()
        attempt = single_action_attempts(report)
        self.assertEqual(attempt["first_correct_attempt"], 2)
        self.assertEqual(attempt["attempts"], [dict(turn=1, call_index=0, matched=False),
                                               dict(turn=1, call_index=1, matched=True)])
        page = "\n".join(_grade_section(_grade_evidence(report, report["messages"])))
        self.assertIn("[첫 시도](#m-1) · 불일치", page)
        self.assertIn("2번째 호출", page)
        official = report["reward_info"]["action_checks"][0]
        with self.assertRaisesRegex(ContractError, "registered task"):
            single_action_attempts(report, expected_action={**official["action"], "compare_args": []})
        official["action_match"] = False
        with self.assertRaisesRegex(ContractError, "recorded tool calls"):
            single_action_attempts(report)

    @unittest.skipUnless(find_spec("tau2"), "Requires the native ACTION matcher")
    def test_another_actor_can_earn_native_credit_without_a_matching_actor_attempt(self):
        report = example_report()
        report["messages"][1]["role"] = "user"
        for call in report["messages"][1]["tool_calls"]:
            call["requestor"] = "user"
        action = single_action_attempts(report)
        self.assertTrue(action["official_match"])
        self.assertEqual(action["attempts"], [])
        self.assertIsNone(action["first_correct_attempt"])
        user_action = deepcopy(report["reward_info"]["action_checks"][0]["action"])
        user_action["requestor"] = "user"
        report["reward_info"]["action_checks"][0]["action"] = user_action
        self.assertEqual(single_action_attempts(report)["first_correct_attempt"], 2)

    @unittest.skipUnless(find_spec("tau2") and (ROOT / "runs/tau-banking-memory-01").is_dir(),
                         "Requires the archived banking consultations")
    def test_reproduce_first_and_later_matches_from_two_archived_tasks(self):
        from scripts.analyze_banking_memory import action_attempt_report

        shards = [f"runs/tau-banking-{study}-01/task-task_008/solve" for study in ("memory", "required", "result-feedback")]
        shards.extend(f"runs/tau-banking-policy-check-01/task-task_014__{arm}/solve" for arm in ("existing", "candidate"))
        shards += ["runs/tau-banking-memory-01/task-task_008/source", "runs/tau-banking-policy-check-sources-01/task-task_014/source"]
        report = action_attempt_report(ROOT, shards)
        for task, expected in (("task_008", (30, 29, 24, 8, 16)), ("task_014", (10, 10, 8, 8, 0))):
            counts = report["by_task"][task]["solve"]
            self.assertEqual(tuple(counts[k] for k in ("assigned", "graded", "official_success", "first_correct", "later_correct")), expected)
        self.assertEqual(len(report["allocations"]), 42)
        missing, = [r for r in report["allocations"] if r["official_success"] is None]
        self.assertIsNone(missing["action"])
        self.assertEqual(missing["status"], "planned")
