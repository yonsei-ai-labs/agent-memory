"""Cross-task plan, decision and pairing checks with in-memory outcomes."""

from copy import deepcopy
import unittest

from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.study.plan import check_plan, load_plan
from agent_memory_eval.study.scorecard import scorecard
from agent_memory_eval.study.transfer import allocation_rows, decision_evidence
from agent_memory_eval.study.transfer_baselines import target_transitions


def study(domain):
    return load_plan(ROOT / f"protocols/studies/{domain}-behavior-transfer-dev-01.json", ROOT)


class TransferTests(unittest.TestCase):
    def test_banking_smoke_links_the_written_note_without_counting_it_as_exposed(self):
        plan = load_plan(ROOT / "protocols/studies/smoke/fresh-smoke-bank-transfer-01.json", ROOT)
        source = plan["transfer"]["pairs"][0]["source"]
        note = dict(form="fact", items=[dict(title="Case", body="Prior task outcome: success.")])
        rows = allocation_rows(plan, [], written_memories={(source, 1, "fact"): note})
        self.assertEqual(len(rows), 8)
        self.assertEqual(sum(r.get("written_memory") == note for r in rows), 4)
        self.assertTrue(all(r["decision"] is None and r["status"] == "missing" for r in rows))

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

    def test_natural_transitions_use_only_independent_no_memory_repeats_of_the_same_input(self):
        anchor = dict(run="first", opening_sha256="target-opening", first_worker_sha256="plain", success=False)
        row = dict(task="target", opening_sha256="target-opening", first_worker_sha256="preload", success=True,
                   target_baselines=[anchor])
        without_repeat = target_transitions([row], "official_success")
        self.assertEqual(without_repeat["observed"]["gain"], 1)
        self.assertIsNone(without_repeat["no_memory_expectation"]["expected_counts"])
        row["target_baselines"].extend([
            dict(anchor, run="second", success=True), dict(anchor, run="third"),
            dict(anchor, run="different-policy", first_worker_sha256="optional", success=True),
        ])
        unrelated = dict(row, opening_sha256="different-opening")
        result = target_transitions([row, unrelated], "official_success")
        self.assertEqual(result["missing_reasons"], {"matched_opening_unavailable": 1})
        estimate = result["no_memory_expectation"]
        self.assertEqual((estimate["covered_pairs"], estimate["independent_repeat_runs"]), (1, 2))
        self.assertEqual(estimate["expected_counts"], dict(gain=0.5, loss=0, both_success=0, both_failure=0.5))


if __name__ == "__main__":
    unittest.main()
