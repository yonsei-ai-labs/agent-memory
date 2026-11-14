"""Guard against hiding meaningful argument changes in the supplemental metric."""

from copy import deepcopy
import unittest

from agent_memory_eval.analysis.action_comparison import (
    build_action_supplement, compare_business_actions, without_transfer_summary,
)
from agent_memory_eval.contracts.core import ContractError


def run(actions, **values):
    return dict(started=True, actions=actions, final_db_hash="same-db", **values)


def action(name, **arguments):
    return dict(name=name, arguments=arguments)


class ActionComparisonTests(unittest.TestCase):
    def test_transfer_synopsis_paraphrase_is_separate_from_call_changes(self):
        left = run([action("transfer_to_human_agents", summary="Please help", user_id="u1")])
        right = run([action("transfer_to_human_agents", summary="Needs assistance", user_id="u1")])
        result = compare_business_actions(left, right)
        self.assertTrue(result["raw_actions_changed"])
        self.assertFalse(result["actions_changed_without_transfer_summary"])
        self.assertTrue(result["transfer_summary_only_changed"])
        self.assertFalse(result["final_db_changed"])

    def test_ids_other_summary_fields_and_call_order_remain_significant(self):
        original = [action("get_reservation_details", reservation_id="r1"),
                    action("transfer_to_human_agents", summary="Help", user_id="u1")]
        changed_id = deepcopy(original)
        changed_id[0]["arguments"]["reservation_id"] = "r2"
        changed_user = deepcopy(original)
        changed_user[1]["arguments"]["user_id"] = "u2"
        for changed in (changed_id, changed_user, list(reversed(original)), original[:1]):
            self.assertTrue(compare_business_actions(run(original), run(changed))["actions_changed_without_transfer_summary"])
        self.assertTrue(compare_business_actions(
            run([action("another_tool", summary="one")]), run([action("another_tool", summary="two")])
        )["actions_changed_without_transfer_summary"])

    def test_normalization_preserves_original_evidence(self):
        actions = [action("transfer_to_human_agents", summary="Help", extra={"value": [1]})]
        original = deepcopy(actions)
        normalized = without_transfer_summary(actions)
        normalized[0]["arguments"]["extra"]["value"].append(2)
        self.assertEqual(actions, original)

    def test_skipped_and_unavailable_db_are_not_equal_outcomes(self):
        result = compare_business_actions(run([]), dict(started=False, actions=[]))
        self.assertTrue(all(value is None for value in result.values()))
        right = run([])
        del right["final_db_hash"]
        self.assertIsNone(compare_business_actions(run([]), right)["final_db_changed"])

    def test_supplement_requires_audit_and_consistent_raw_comparison(self):
        summary = dict(audit_status="audit_passed", experiment_id="fixture", log_sha256="a", summary_sha256="b",
            runs=[run([action("transfer_to_human_agents", summary="one")], run_id="a"),
                  run([action("transfer_to_human_agents", summary="two")], run_id="b")],
            blocked_comparisons=[dict(block_id=1, baseline="empty_bank", treated="memory_bank",
                baseline_run_id="a", treated_run_id="b", actions_changed=True)])
        original = deepcopy(summary)
        counts = build_action_supplement(summary)["counts"]
        self.assertEqual(counts["raw_action_changes"], 1)
        self.assertEqual(counts["action_changes_without_transfer_summary"], 0)
        self.assertEqual(counts["transfer_summary_only_changes"], 1)
        self.assertEqual(summary, original)
        summary["blocked_comparisons"][0]["actions_changed"] = False
        with self.assertRaises(ContractError):
            build_action_supplement(summary)
        with self.assertRaises(ContractError):
            build_action_supplement({})


if __name__ == "__main__":
    unittest.main()
