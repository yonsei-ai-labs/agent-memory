"""A frozen decision, its paired control and unobserved consultations retain their original denominators."""

from copy import deepcopy
from collections import Counter
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.assets import asset_bytes
from agent_memory_eval.recording.journal import BlobStore
from agent_memory_eval.study.adapters.retail_revisit import (
    ENDPOINTS, approval_amount, consultation_row, followup_scorecard, paired_outcome, transcript_outcome, validate_pairs,
)
from agent_memory_eval.study.plan import check_plan, load_plan, require_declared_models
from scripts.analyze_retail_two_visit import public_turns
from scripts.run_retail_two_visit import execute, read, read_receipt


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
        plan = load_plan(ROOT / "protocols/studies/retail-revisit-followup-02.json", ROOT)
        decisions = read_json(asset_bytes(ROOT, plan["benchmark"]["decisions"]))["decisions"]
        archive = ROOT / "artifacts/analyses/20261124-decision-attribution.json"
        audit = read_json(archive.read_bytes())["fixed"]["previous_only_conditional"]
        estimate = paired_outcome(decisions, plan["comparison"], "focused", None)
        self.assertEqual(estimate["pairs"], audit["pairs"])
        self.assertEqual(estimate["registered_customers"], audit["customers"])

    def test_archived_plans_are_readable_and_current_plan_requires_role_attribution(self):
        old = load_plan(ROOT / "protocols/studies/retail-revisit-followup-01.json", ROOT)
        with self.assertRaisesRegex(ContractError, "blinded primary"):
            require_declared_models(old)
        v2 = load_plan(ROOT / "protocols/studies/retail-revisit-followup-02.json", ROOT)
        with self.assertRaisesRegex(ContractError, "worker limits"):
            require_declared_models(v2)
        active = load_plan(ROOT / "protocols/studies/retail-revisit-followup-03.json", ROOT)
        self.assertEqual(active["benchmark"], old["benchmark"])
        with self.assertRaisesRegex(ContractError, "worker_failure_limits"):
            check_plan(dict(active, scoring=v2["scoring"]), ROOT)
        del active["scoring"]
        with self.assertRaisesRegex(ContractError, "scoring"):
            check_plan(active, ROOT)

    @unittest.skipUnless((ROOT / "runs/tau2-retail-two-visit-01-recovery/registration.json").exists(),
                         "Requires the original and recovery retail consultation journals")
    def test_known_missed_order_questions_require_the_blinded_labels(self):
        archive = ROOT / "artifacts/analyses"
        result = read(archive / "20261122-retail-two-visit-results-with-recovery.json")
        cohort = set(read(archive / "20261122-retail-two-visit-preflight.json")["continuation_sample"])
        jobs, labels = {}, {}
        for batch, review in (("01", "original"), ("01-recovery", "recovery")):
            folder = ROOT / "runs" / ("tau2-retail-two-visit-" + batch)
            for j in read(folder / "registration.json")["jobs"]["decision"]:
                jobs[j["arm"], j["task"]] = (j, folder / j["run"])
            labels.update(read(archive / f"20261122-retail-two-visit-{review}-reviews.json")["labels"])
        missed = Counter()
        reviewed = 0
        for row in result["continuations"]:
            if (row["customer"] not in cohort or row["arm"] not in {"query", "empty_query"}
                    or row["mode"] != "previous_only" or row["completion"] != "completed"):
                continue
            job, folder = jobs[row["arm"], row["task"]]
            receipt = read(folder / "continuation.json")
            turns = public_turns(BlobStore(folder).get(receipt["trajectory"]))
            direct = transcript_outcome(turns, job["target"], receipt["db_reward"], {}, {}, direct_review=True)
            self.assertEqual(direct["review"], "awaiting_review")
            reviewed += 1
            old_rule = transcript_outcome(turns, job["target"], receipt["db_reward"], {}, {}, direct_review=False)
            if old_rule["consent_without_reasking"] is True and row["clarifications"] > 0:
                label = labels[row["review_id"]]
                actual = transcript_outcome(turns, job["target"], receipt["db_reward"], {label["turns_sha256"]: label},
                                            {}, direct_review=True)
                self.assertIs(actual["asked_which_order"], True)
                self.assertIs(actual["consent_without_reasking"], False)
                missed[row["arm"]] += 1
        self.assertEqual(reviewed, 103)
        self.assertEqual(missed, {"query": 4, "empty_query": 1})

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

    def test_approval_increases_are_monotonic_and_linked_to_the_same_allocation(self):
        from agent_memory_eval.study.followup_budget import approval_chain, publish_approval
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "grant.json"
            first = dict(plan_id="study", resolved_sha256="abc", max_usd="0.1", user_authorization="fixture")
            second = dict(first, max_usd="1", user_authorization="raise the cumulative fixture amount")
            self.assertEqual(publish_approval(base, first), base)
            amendment = base.with_suffix(".increases") / "000001.json"
            self.assertEqual(publish_approval(base, second), amendment)
            self.assertEqual(publish_approval(base, second), amendment)
            self.assertEqual(approval_chain(base), [first, second])
            self.assertEqual(read(base), first)
            for change in (dict(max_usd="0.5"), dict(resolved_sha256="other", max_usd="2"), dict(user_authorization="", max_usd="2")):
                with self.assertRaises(ContractError):
                    publish_approval(base, dict(second, **change))
            (base.with_suffix(".increases") / "000001.json").rename(base.with_suffix(".increases") / "000002.json")
            with self.assertRaisesRegex(ContractError, "history"):
                approval_chain(base)

    def test_review_cli_outside_repository_and_incomplete_model_profile(self):
        from agent_memory_eval.study.plan import model_profile
        with TemporaryDirectory() as tmp:
            plan = ROOT / "protocols/studies/retail-revisit-followup-03.json"
            cmd = [sys.executable, "-I", "-m", "agent_memory_eval.study", "plan", "validate", "--plan", str(plan)]
            output = subprocess.run(cmd, cwd=tmp, capture_output=True, check=True)
            self.assertEqual(read_json(output.stdout)["plan"], "retail-revisit-followup-03")
            data = read(plan)
            partial = read(ROOT / data["models"]["profile"]["path"])
            del partial["expected_revisions"]["user_simulator"]
            with patch("agent_memory_eval.study.plan.asset_text", return_value=canonical(partial).decode()):
                with self.assertRaisesRegex(ContractError, "user_simulator"):
                    model_profile(data, ROOT)

    def test_only_committed_ceiling_increases_can_change_the_resume_revision(self):
        from agent_memory_eval.reproduction.freeze import implementation_identity
        from agent_memory_eval.study.followup_budget import registration_freeze
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                                *args], check=True, capture_output=True)
            git("init", "-q")
            code = root / "runner.py"
            code.write_text("pass\n")
            git("add", ".")
            git("commit", "-qm", "fixed implementation")
            first = implementation_identity(root)
            grants = root / "artifacts/approvals/retail.increases"
            grants.mkdir(parents=True)
            (grants / "000001.json").write_text("{}\n")
            git("add", ".")
            git("commit", "-qm", "increase")
            self.assertEqual(registration_freeze(root, "retail", first, implementation_identity(root)), first)
            code.write_text("changed = True\n")
            git("add", ".")
            git("commit", "-qm", "changed implementation")
            with self.assertRaisesRegex(ContractError, "registered implementation"):
                registration_freeze(root, "retail", first, implementation_identity(root))

    def test_consecutive_outages_preserve_unsent_jobs_for_resume(self):
        jobs = [dict(run=f"r_{n}", in_sample=True) for n in range(3)]
        study = dict(jobs=dict(source=[], writer=[], decision=[], continuation=jobs), tasks={}, public={})
        with TemporaryDirectory() as tmp, patch("scripts.run_retail_two_visit.time.sleep"):
            dest = Path(tmp)
            def run():
                guard = SimpleNamespace(denied=False, calls=0, spent=0, held=0, deadline=float("inf"))
                return execute(study, dict(guard=guard), through="continuation", continuation="sample",
                               destination=dest, pause_after_transient=2)
            outage = [{"status": "error", "provider_error": "HTTP_520"},
                      {"status": "error", "provider_error": "PROVIDER_RESPONSE_ERROR", "http_status": 524}]
            with patch("scripts.run_retail_two_visit.run_job", side_effect=outage) as attempt:
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
        negative = [dict(r, consent_without_reasking=r["arm"] == "empty") for r in rows]
        negative[0].update(review="unresolved", consent_without_reasking=None)
        worse = followup_scorecard(negative, data, comparison, {})
        self.assertEqual(worse["primary_result"], "worse_in_this_cohort")
        self.assertEqual(worse["contrasts"]["consent_without_reasking"]["bounds"], [-1, -.75])
        for row in rows[4:]:
            row.update(status="error", **dict.fromkeys(ENDPOINTS))
        self.assertEqual(followup_scorecard(rows, data, comparison, {})["primary_result"], "inconclusive")
        rows[-1]["status"] = "unstarted"
        self.assertEqual(followup_scorecard(rows, data, comparison, {})["primary_result"], "pending")
