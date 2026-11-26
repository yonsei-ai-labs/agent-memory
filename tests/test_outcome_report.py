import unittest
from copy import deepcopy

from jsonschema import Draft202012Validator

from agent_memory_eval.adapters.bfcl_control_results import BFCL_ANSWER, BFCL_EXPOSURE, control_trials
from agent_memory_eval.adapters.retail_revisit_results import FINAL_DB, FIRST_TARGET, revisit_trials
from agent_memory_eval.adapters.tau_banking_results import BANKING_SUCCESS, banking_trials
from agent_memory_eval.analysis.outcome_report import archive_report
from agent_memory_eval.analysis.paired_trials import compare_trials
from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.contracts.validation import ROOT, pointer


ARCHIVES = ROOT / "artifacts/analyses"


class OutcomeReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = Draft202012Validator(read_json((ROOT / "schemas/paired-outcomes-v1.schema.json").read_bytes()))

    def test_bfcl_control_preserves_evidence_and_separates_correctness_from_exposure(self):
        path = ARCHIVES / "20261125-bfcl-native-memory-control.json"
        raw = read_json(path.read_bytes())
        fixture = read_json((ROOT / "tests/fixtures/paired-outcomes-v1.json").read_bytes())
        for metric in (BFCL_ANSWER, BFCL_EXPOSURE):
            result = archive_report(path, metric.name, "access_control", "read_enabled", expected_sha256=fixture["archive_sha256"])
            self.schema.validate(result)
            self.assertEqual(result["record_kind"], "scripted_control")
            self.assertIsNone(result["comparison"]["sources"]["sign_p_if_independent"])
            self.assertEqual(result["comparison"]["metric"]["name"], metric.name)
            for arm, expected in (("reference", False), ("candidate", True)):
                row, = result["observations"][arm]
                self.assertIs(row["success"], expected)
                self.assertEqual(pointer(raw, row["pointer"]), expected)
            if metric == BFCL_ANSWER:
                self.assertEqual(result, fixture)
        with self.assertRaisesRegex(ContractError, "metric mismatch"):
            compare_trials(control_trials(raw, "access_control", BFCL_ANSWER),
                           control_trials(raw, "read_enabled", BFCL_EXPOSURE))
        with self.assertRaisesRegex(ContractError, "SHA-256"):
            archive_report(path, BFCL_ANSWER.name, "access_control", "read_enabled", expected_sha256="0" * 64)
        with self.assertRaisesRegex(ContractError, "scripted"):
            control_trials(dict(raw, scripted=False), "read_enabled", BFCL_ANSWER)
        invalid = deepcopy(fixture)
        invalid["comparison"]["pairs"]["sign_p_if_independent"] = 1
        self.assertFalse(self.schema.is_valid(invalid))

    def test_banking_original_and_missing_results_have_replayable_rows(self):
        for name, successes, graded in (("policy-cycle-validation", (7, 4), 20),
                                        ("replacement-original-results", (0, 0), 0)):
            path = ARCHIVES / f"20261125-banking-{name}.json"
            raw = read_json(path.read_bytes())
            result = archive_report(path, "banking.official_success", "existing", "candidate")
            self.schema.validate(result)
            self.assertEqual(result["archive_sha256"], digest(path.read_bytes()))
            self.assertEqual(result["comparison"]["pairs"]["jointly_graded"], graded)
            for arm, expected in zip(("reference", "candidate"), successes, strict=True):
                coverage = result["comparison"][arm]
                self.assertEqual((coverage["started"], coverage["successes"]), (graded, expected))
                self.assertEqual(coverage["assigned"], 20 if graded else 5)
                if not graded:
                    self.assertIsNone(coverage["success_rate"])
                for obs in result["observations"][arm]:
                    row = pointer(raw, obs["pointer"])
                    self.assertEqual((obs["task"], obs["slot"], obs["success"]), (row["task"], row["arm"], row["success"]))
            raw["trials"]["existing"][0].update(status="unknown", success=None)
            with self.assertRaisesRegex(ContractError, "Unknown banking trial status"):
                banking_trials(raw, "existing", BANKING_SUCCESS)

    def test_retail_pre_recovery_does_not_count_unstarted_cases_as_incorrect(self):
        raw = read_json((ARCHIVES / "20261122-retail-two-visit-results.json").read_bytes())
        final = revisit_trials(raw, "preload", FINAL_DB, "previous_only")
        first = revisit_trials(raw, "preload", FIRST_TARGET, "previous_only")
        for trials in (first, final):
            absent = [t for t in trials if not t.started]
            self.assertTrue(absent)
            self.assertTrue(all(t.success is None for t in absent))
        self.assertTrue(all(pointer(raw, t.evidence_pointer)["completion"] == "decision_missing"
                            for t in final if not t.started))
        wrong_db = dict(raw, continuations=[dict(pointer(raw, final[0].evidence_pointer),
                                                 completion="completed", db_reward="0")])
        with self.assertRaisesRegex(ContractError, "binary"):
            revisit_trials(wrong_db, "preload", FINAL_DB, "previous_only")
