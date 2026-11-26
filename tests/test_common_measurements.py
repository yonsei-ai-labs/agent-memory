import unittest
from dataclasses import replace
from functools import partial

from agent_memory_eval.adapters.retail_revisit_results import FINAL_DB, FIRST_TARGET, revisit_trials
from agent_memory_eval.adapters.tau_banking_results import BANKING_SUCCESS
from agent_memory_eval.analysis.action_grounding import literal_citations
from agent_memory_eval.analysis.efficiency import business_calls_by_role
from agent_memory_eval.analysis.paired_trials import Trial, compare_trials, component_coverage, outcome_coverage, sign_test, target_effects
from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.contracts.validation import ROOT, pointer

fixture_trial = partial(Trial, metric=BANKING_SUCCESS, cohort="test-cohort")


class CommonMeasurementTests(unittest.TestCase):
    def test_one_arm_can_be_graded_without_a_comparable_pair(self):
        reference = fixture_trial("case", "procedure", "source", True, True, {"tokens": 12})
        candidate = fixture_trial("case", "procedure", "source", False, None)
        result = compare_trials([reference], [candidate])
        self.assertEqual(result["reference"]["success_rate"], 1)
        self.assertIsNone(result["candidate"]["success_rate"])
        self.assertEqual(result["pairs"]["missing"], 1)
        self.assertIsNone(result["pairs"]["discordance_rate"])
        self.assertEqual(result["sources"]["complete"], 0)
        self.assertIsNone(result["joint_success_effort"]["metrics"]["tokens"]["difference"])

    def test_repeated_slots_are_one_source_and_joint_success_keeps_its_members(self):
        old = [fixture_trial("t", f, "s", True, success) for f, success in (("A", False), ("B", True))]
        new = [fixture_trial("t", f, "s", True, True) for f in ("A", "B")]
        result = compare_trials(old, new)
        self.assertEqual(result["sources"]["assigned"], 1)
        self.assertEqual(result["pairs"]["gain"], 1)
        self.assertEqual(result["joint_success_members"], [{"task": "t", "slot": "B", "source": "s"}])
        self.assertEqual(sign_test(9, 1), 0.021484375)
        self.assertEqual(sign_test(2, 3), 1)
        self.assertIsNone(sign_test(0, 0))
        for other in (new[:-1], [*new, new[1]], [new[0], replace(new[1], source="other")]):
            with self.assertRaises(ContractError):
                compare_trials(old, other)

    def test_actor_counts_include_tool_only_turns_and_receipts_separate_extra_targets(self):
        messages = [{"role": "assistant", "tool_calls": [{"name": "memory_get"}, {"name": "lookup"}]},
                    {"role": "user", "content": "", "tool_calls": [{"name": "execute"}, {"name": "execute"}]}]
        self.assertEqual(business_calls_by_role(messages), {"assistant": 1, "user": 2})
        result = target_effects({"A", "B"}, [("A", True), ("C", True), ("A", True), ("B", None), ("B", False)])
        self.assertEqual(result, dict(expected=2, attempted=5, accepted_target=1, accepted_other=1,
            accepted_duplicate=1, rejected=1, unconfirmed=1, targets_without_confirmed_effect=1))
        self.assertEqual(component_coverage(["doc-A", "doc-B"], ["doc-A", "doc-A"]),
                         {"required": 2, "confirmed": 1, "missing": 1})
        with self.assertRaises(ContractError):
            component_coverage(["doc-A"], ["not registered"])

    def test_unidentified_source_is_neither_a_cluster_nor_a_graded_zero(self):
        unstarted = fixture_trial("t", "s", None, False, None)
        result = compare_trials([unstarted], [unstarted])
        self.assertEqual(result["sources"]["unknown_source_pairs"], 1)
        self.assertEqual(result["sources"]["assigned"], 0)
        self.assertIsNone(result["reference"]["success_rate"])
        self.assertIsNone(result["pairs"]["sign_p_if_independent"])
        with self.assertRaises(ContractError):
            sign_test(-2, 5)

    def test_literal_citations_have_replayable_pointers_and_offsets(self):
        doc = {"a/b": ["a $200 fee"], "z~": "$200 later", "unrelated": 200}
        cites = literal_citations(doc, "$200")
        self.assertEqual([c["pointer"] for c in cites], ["/a~1b/0", "/z~0"])
        for c in cites:
            self.assertEqual(pointer(doc, c["pointer"])[c["start"]:c["start"] + len(c["quote"])], "$200")

    def test_77_customer_revisit_reproduces_first_decisions_and_full_db_coverage(self):
        path = ROOT / "artifacts/analyses/20261122-retail-two-visit-results-with-recovery.json"
        raw = path.read_bytes()
        self.assertEqual(digest(raw), "5808756f078cc26b39f0453460582d079d9e4eb2eefd47b5dae8b8b586892ad3")
        study = read_json(raw)
        for name, observed in study["first"]["paired"].items():
            mode, candidate, reference = name.split("/")
            args = [revisit_trials(study, arm, FIRST_TARGET, mode) for arm in (reference, candidate)]
            table = compare_trials(*args, metrics=())
            self.assertEqual(table["pairs"]["jointly_graded"], observed["complete_pairs"])
            self.assertEqual(table["sources"]["included"], observed["customers_with_pairs"])
            for direction in ("gain", "tie", "loss"):
                self.assertEqual(table["sources"][direction], observed[direction])
            self.assertEqual(table["sources"]["sign_p_if_independent"], observed["two_sided_sign_p"])
        for name, observed in study["full_paired"].items():
            mode, candidate, reference, endpoint = name.split("/")
            if endpoint != "db_success":
                continue
            args = [revisit_trials(study, arm, FINAL_DB, mode) for arm in (reference, candidate)]
            table = compare_trials(*args, metrics=())
            self.assertEqual(table["pairs"]["jointly_graded"], observed["complete_pairs"])
            self.assertEqual(table["pairs"]["both_success"] + table["pairs"]["gain"], observed["left_positive"])
            self.assertEqual(table["pairs"]["both_success"] + table["pairs"]["loss"], observed["right_positive"])
            self.assertEqual(table["sources"]["included"], observed["customers_with_pairs"])
            for direction in ("gain", "tie", "loss"):
                self.assertEqual(table["sources"][direction], observed[direction])
            self.assertEqual(table["sources"]["sign_p_if_independent"], observed["two_sided_sign_p"])
            if name == "previous_only/preload/empty_preload/db_success":
                self.assertEqual((table["sources"]["complete"], table["sources"]["included"]), (76, 77))

        first = revisit_trials(study, "empty_preload", FIRST_TARGET, "previous_only")
        final = revisit_trials(study, "empty_preload", FINAL_DB, "previous_only")
        self.assertEqual((len(first), sum(t.success for t in first), sum(t.success for t in final)), (154, 27, 154))
        same = compare_trials(first, first, metrics=())["pairs"]
        self.assertEqual((same["gain"], same["loss"]), (0, 0))
        with self.assertRaisesRegex(ContractError, "retail.first_target_lookup.*retail.final_db_success"):
            compare_trials(first, final)

    def test_each_observation_binds_the_metric_and_cohort_even_in_a_mixed_arm(self):
        row = fixture_trial("t", "a", "source", False, None)
        for field, value in {"name": "another", "version": 2, "definition": "Changed rule", "stage": "initial",
                             "unit": "action", "denominator": "started_cases", "cluster_unit": "customer",
                             "cluster_eligibility": "any_pair_graded"}.items():
            other = replace(row, slot="b", metric=replace(row.metric, **{field: value}))
            with self.subTest(field=field), self.assertRaisesRegex(ContractError, field):
                compare_trials([row, other], [row, other])
        with self.assertRaisesRegex(ContractError, "cohort"):
            compare_trials([row], [replace(row, cohort="another-cohort")])
        with self.assertRaisesRegex(ContractError, "metric"):
            outcome_coverage([row, replace(row, metric=FINAL_DB)])
        with self.assertRaisesRegex(ContractError, "metric"):
            replace(row, metric=None)

    def test_banking_receipts_and_unstarted_slots_against_archived_outcomes(self):
        from agent_memory_eval.adapters.tau_banking_results import policy_table

        domain = ROOT / "data/tau2-bench/data/tau2/domains/banking_knowledge"
        if not (ROOT / "runs/tau-banking-policy-check-01").is_dir() or not domain.is_dir():
            self.skipTest("Archived native banking trajectories are not mounted")
        def table(profile, filename):
            study = read_json((ROOT / "protocols/pilots" / profile).read_bytes())
            result = read_json((ROOT / "artifacts/analyses" / filename).read_bytes())
            return policy_table(result, ROOT / "runs" / study["validation_study"], domain)
        comparison = table("tau-banking-policy-cycle-v1.json", "20261125-banking-policy-cycle-validation.json")
        self.assertEqual(comparison["role_calls"]["existing"], {"observed": 20, "worker": 123, "customer": 3})
        self.assertEqual(comparison["role_calls"]["candidate"], {"observed": 20, "worker": 121, "customer": 5})
        receipt, = [r for r in comparison["dispute_receipts"] if r["attempted"]]
        self.assertEqual((receipt["task"], receipt["accepted_target"], receipt["accepted_other"],
                          receipt["targets_without_confirmed_effect"]), ("task_017", 1, 1, 1))
        for name, successes in (("existing", 16), ("candidate", 12)):
            self.assertEqual(sum(r["official_actions"]["confirmed"] for r in comparison["process_checks"][name]), successes)
        missing = table("tau-banking-replacement-v1.json", "20261125-banking-replacement-original-results.json")
        for arm in ("reference", "candidate"):
            self.assertEqual(missing["comparison"][arm], dict(assigned=5, started=0, graded=0, successes=0, success_rate=None))
