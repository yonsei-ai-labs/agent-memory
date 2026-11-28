"""A frozen decision, its paired control and unobserved consultations retain their original denominators."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.assets import asset_bytes
from agent_memory_eval.study.adapters.retail_revisit import (
    ENDPOINTS, approval_amount, consultation_row, followup_scorecard, paired_outcome, validate_pairs,
)
from scripts.run_retail_two_visit import execute, read_receipt


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.comparison = dict(memory="real", empty="empty", primary_mode="previous_only")
        self.arms = {"real": dict(native_arm="query", instruction="same"),
                     "empty": dict(native_arm="empty_query", instruction="same")}
        self.jobs = [dict(customer="c", task="t", source="s", mode="previous_only", prefix="p", target="a",
                          history="a", position=2, initial_sha256="wire", in_sample=True, arm=arm,
                          items_sha256=digest(canonical([])), writer_runs=["w"] if arm == "real" else [], **values)
                     for arm, values in self.arms.items()]

    def test_validate_same_wire_same_goal_and_empty_assignment(self):
        def validate(jobs):
            return validate_pairs(jobs, {"arms": self.arms}, self.comparison, {"c"})

        self.assertEqual(validate(self.jobs), 1)  # A naturally empty real bank keeps its assignment.
        for field, changed in (("writer_runs", ["w"]), ("items_sha256", "other"), ("target", "b"),
                               ("initial_sha256", "different"), ("in_sample", False)):
            with self.subTest(field=field), self.assertRaises(ContractError):
                jobs = deepcopy(self.jobs)
                jobs[1][field] = changed
                validate(jobs)
        with self.assertRaises(ContractError):
            validate(self.jobs[:1])

    def test_cluster_weights_missing_and_duplicate_cells(self):
        rows = [dict(customer=c, task=t, mode="previous_only", arm=arm, outcome=y)
                for c, t, ys in [("a", "1", (True, False)), ("a", "2", (True, False)),
                                 ("b", "3", (False, True)), ("b", "4", (None, True))]
                for arm, y in zip(("real", "empty"), ys)]
        result = paired_outcome(rows, self.comparison, "outcome", None)
        self.assertEqual(result["difference"], 0)
        self.assertEqual(result["pairs"]["missing"], 1)
        self.assertEqual(result["bounds"], [0, .25])
        with self.assertRaises(ContractError):
            paired_outcome(rows + [rows[0]], self.comparison, "outcome", None)

    def test_registered_positive_control_matches_independent_first_action_audit(self):
        plan = read_json((ROOT / "protocols/studies/retail-revisit-followup-01.json").read_bytes())
        decisions = read_json(asset_bytes(ROOT, plan["benchmark"]["decisions"]))["decisions"]
        archive = ROOT / "artifacts/analyses/20261124-decision-attribution.json"
        audit = read_json(archive.read_bytes())["fixed"]["previous_only_conditional"]
        estimate = paired_outcome(decisions, plan["comparison"], "focused", None)
        self.assertEqual(estimate["pairs"], audit["pairs"])
        self.assertEqual(estimate["registered_customers"], audit["customers"])

    def test_read_limit_and_unstarted_do_not_become_question_free_successes(self):
        previous = dict(session="s", first_lookup=True, focused=False, decision_request=None, included=[],
                        confirmed_usd="0", held_usd="0")
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / "absent"
            job = dict(self.jobs[0], run="r_example")
            capped = consultation_row(folder, job, {"status": "private_read_limit"}, previous, {}, {})
            unseen = consultation_row(folder, job, {"status": "checkpoint"}, previous, {}, {})
            self.assertIs(capped["consent_without_reasking"], False)
            self.assertIsNone(capped["asked_which_order"])
            self.assertIsNone(unseen["consent_without_reasking"])
            self.assertEqual(unseen["status"], "unstarted")

    def test_positive_finite_grant(self):
        for amount in ("NaN", "Infinity", "-1", "0", "x", None):
            with self.subTest(amount=amount), self.assertRaises(ContractError):
                approval_amount(amount)

    def test_consecutive_outages_preserve_unsent_jobs_for_resume(self):
        jobs = [dict(run=f"r_{n}", in_sample=True) for n in range(3)]
        study = dict(jobs=dict(source=[], writer=[], decision=[], continuation=jobs), tasks={}, public={})
        with TemporaryDirectory() as tmp, patch("scripts.run_retail_two_visit.time.sleep"):
            dest = Path(tmp)
            def run():
                guard = SimpleNamespace(denied=False, calls=0, spent=0, held=0, deadline=float("inf"))
                return execute(study, dict(guard=guard), through="continuation", continuation="sample",
                               destination=dest, pause_after_transient=2)
            with patch("scripts.run_retail_two_visit.run_job", return_value={"status": "error", "provider_error": "HTTP_503"}) as attempt:
                self.assertEqual(run()["continuation"], {"error": 2})
                self.assertEqual(attempt.call_count, 2)
            self.assertFalse((dest / "r_2" / "continuation.started").exists())
            previous = read_receipt(dest / "r_0", "continuation")
            with patch("scripts.run_retail_two_visit.run_job", return_value={"status": "completed"}) as attempt:
                self.assertEqual(run()["continuation"], {"error": 2, "completed": 1})
                self.assertEqual(attempt.call_count, 1)
            self.assertEqual(read_receipt(dest / "r_0", "continuation"), previous)

    def test_supported_direction_requires_finished_allocation_and_missing_bounds(self):
        comparison = dict(self.comparison, primary_outcome="consent_without_reasking")
        data = dict(uncertainty=dict(seed=1, replicates=100), record=dict(assigned={}, calibration={}))
        rows = [dict(customer=c, task=c, mode="previous_only", arm=arm, status="completed",
                     first_confirmed_usd="0", first_held_usd="0", continuation_confirmed_usd="0", continuation_held_usd="0",
                     **dict.fromkeys(ENDPOINTS, arm == "real")) for c in ("a", "b", "c", "d") for arm in ("real", "empty")]
        self.assertEqual(followup_scorecard(rows, data, comparison, {})["primary_result"], "improved_in_this_cohort")
        for row in rows[4:]:
            row.update(status="error", **dict.fromkeys(ENDPOINTS))
        self.assertEqual(followup_scorecard(rows, data, comparison, {})["primary_result"], "inconclusive")
        rows[-1]["status"] = "unstarted"
        self.assertEqual(followup_scorecard(rows, data, comparison, {})["primary_result"], "pending")
