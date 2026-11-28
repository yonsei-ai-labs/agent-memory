"""Cross-task plan, decision and pairing checks with in-memory outcomes."""

from copy import deepcopy
import unittest

from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.study.plan import check_plan, load_plan
from agent_memory_eval.study.scorecard import scorecard
from agent_memory_eval.study.transfer import allocation_rows, decision_evidence


def study(domain):
    return load_plan(ROOT / f"protocols/studies/{domain}-behavior-transfer-dev-01.json", ROOT)


class TransferTests(unittest.TestCase):
    def test_invalid_or_duplicate_pairs_are_rejected(self):
        plan = study("retail")
        for source, target in [("44", "44"), ("58", "37"), ("44", "unknown")]:
            changed = deepcopy(plan)
            changed["transfer"]["pairs"][0].update(source=source, target=target)
            with self.assertRaises(ContractError):
                check_plan(changed, ROOT)
        plan["transfer"]["pairs"].append(plan["transfer"]["pairs"][0])
        with self.assertRaisesRegex(ContractError, "Duplicate transfer"):
            check_plan(plan, ROOT)

    def test_aborted_turn_is_not_credit_for_refraining_from_an_action(self):
        rule = dict(tools=["update"], expected=None)
        self.assertIsNone(decision_evidence(rule, [], completed=False)["correct"])
        self.assertIs(decision_evidence(rule, [], completed=True)["correct"], True)
        called = [dict(request="a", body=False, calls=[dict(name="update", arguments={})])]
        self.assertIs(decision_evidence(rule, called, completed=False)["correct"], False)

    def test_distinct_targets_and_missing_bounds(self):
        plan = study("airline")
        rows = allocation_rows(plan, [])
        self.assertEqual(len(rows), 12)
        rows[0].update(status="graded", success=True, decision=dict(correct=True, body_seen=False))
        rows[1].update(status="graded", success=False, decision=dict(correct=False, body_seen=False))
        outcome = scorecard(rows)["comparisons"]["free__on_demand"]["official_success"]
        self.assertEqual((outcome["real_only"], outcome["missing"], outcome["targets"]), (1, 1, 2))
        self.assertEqual(outcome["target_weighted_bounds"], [0, 1])


if __name__ == "__main__":
    unittest.main()
