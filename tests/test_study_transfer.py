"""Cross-task plan, decision and pairing checks with in-memory outcomes."""

from copy import deepcopy
import unittest

from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.study.plan import check_plan, load_plan
from agent_memory_eval.study.scorecard import scorecard
from agent_memory_eval.study.transfer import allocation_rows, decision_evidence
from agent_memory_eval.study.transfer_baselines import outcome, target_transitions


def study(domain):
    return load_plan(ROOT / f"protocols/studies/{domain}-behavior-transfer-dev-01.json", ROOT)


class TransferTests(unittest.TestCase):
    def test_each_cohort_task_keeps_its_first_pass_across_all_policy_comparisons(self):
        plan = study("airline")
        tasks = plan["benchmark"]["tasks"]
        template = plan["transfer"]["pairs"][0]
        plan["transfer"] = dict(bank_comparison="own_vs_mixed", pairs=[
            dict(template, source=t, target=t, customer_relation="same_customer") for t in tasks])
        plan["writing"]["representations"]["fact"] = dict(text="Write facts from the same experience.")
        plan["conditions"] += [dict(representation="fact", reading=r) for r in plan["reading"]]
        check_plan(plan, ROOT)
        rows = allocation_rows(plan, [])
        self.assertEqual(len(rows), len(tasks) * 12)
        for row in rows:
            expected = {row["task"]} if row["bank"] == "own" else set(tasks)
            self.assertEqual(set(row["bank_source_tasks"]), expected)
            self.assertEqual([b["index"] for b in row["target_baselines"]], [1])
        card = scorecard(rows)
        self.assertEqual(card["target_no_memory"]["assigned"], len(tasks))
        self.assertEqual(len(card["policy_contrasts"]), 18)
        self.assertTrue(all(c["official_success"]["missing"] == len(tasks) for c in card["policy_contrasts"]))

    def test_attempt_return_and_full_reference_have_separate_outcomes(self):
        rule = dict(kind="first_matching_tool", tools=["update"], expected="update", arguments={})
        step = dict(request="a", body=False, calls=[dict(name="update", arguments={}, arguments_valid=False)])
        self.assertFalse(decision_evidence(rule, [step], completed=True)["correct"])
        step["calls"][0]["arguments_valid"] = True
        call = dict(receipt=dict(status="reported_error"), matches_any_reference=False)
        row = dict(decision_rule=rule, decision=decision_evidence(rule, [step], completed=True),
                   state_diagnostics=dict(reference_actions=["update"], first_registered_call=call))
        self.assertTrue(outcome(row, "registered_decision"))
        self.assertFalse(outcome(row, "registered_call_executed"))
        call["receipt"]["status"] = "returned"
        self.assertTrue(outcome(row, "registered_call_executed"))
        self.assertFalse(outcome(row, "reference_call_executed"))

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
