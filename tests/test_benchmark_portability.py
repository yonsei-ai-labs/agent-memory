"""Fast compatibility checks using Git evidence; full native controls use the pinned external inputs."""

import unittest

from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from scripts.analyze_memory_claims import analyze
from scripts.audit_benchmark_portability import PROFILE, grade_agreement, routing_criteria


class PortabilityTests(unittest.TestCase):
    def test_missing_is_neither_a_failed_control_nor_an_observed_success(self):
        self.assertEqual(grade_agreement([(True, True), (False, False), (None, None)]),
                         dict(allocated=3, matched_success=1, matched_failure=1, ungraded=1))
        for pair in ((None, False), (False, None), (True, False), ("False", False)):
            with self.subTest(pair=pair), self.assertRaises(ContractError):
                grade_agreement([pair])

    def test_native_memory_requires_both_attributed_access_and_an_isolated_control(self):
        denied = dict(native_return_attributed=False, native_callback_received=[],
                      native_value_in_next_tool_message=False, native_value_spans_in_next_input=0,
                      tool_results={name: {"error_code": "POLICY_DENIED"} for name in ("memory_append", "memory_retrieve")})
        allowed = dict(native_return_attributed=True, native_callback_received=["memory_append", "memory_retrieve"],
                       native_value_in_next_tool_message=True, native_value_spans_in_next_input=1,
                       tool_results={name: {"error_code": None} for name in ("memory_append", "memory_retrieve")})
        for enabled, disabled, expected in ((allowed, denied, True), (denied, denied, False), (allowed, allowed, False)):
            with self.subTest(enabled=enabled["native_return_attributed"], disabled=disabled["native_return_attributed"]):
                self.assertEqual(all(routing_criteria(dict(with_framework_bank=enabled,
                                                           without_framework_bank=disabled)).values()), expected)

    def test_portability_receipt_covers_the_pinned_tasks_and_distinct_metric_denominators(self):
        spec = read_json(PROFILE.read_bytes())
        report = read_json((ROOT / "artifacts/analyses/20261123-framework-portability.json").read_bytes())
        self.assertEqual(report["profile_sha256"], digest(PROFILE.read_bytes()))
        for benchmark in ("tau2", "bfcl", "appworld"):
            self.assertEqual(report[benchmark]["benchmark_revision"], spec[benchmark]["revision"])
        cohort = analyze(ROOT, cohorts_only=True)
        tau = report["tau2"]
        self.assertEqual(tau["airline_official_task_success"], cohort["airline_second_pass"]["all"])
        self.assertEqual(tau["retail_first_action"], cohort["retail_first_decisions"]["all"])
        self.assertEqual(set(tau["samples"]), {r["id"] for r in spec["tau2"]["samples"]})
        self.assertIsNone(tau["samples"]["retail_unfinished"]["native_success"])
        self.assertEqual(tau["samples"]["retail_previous_order"]["native_status"], "not_available")
        self.assertEqual(set(report["appworld"]["tasks"]), spec["appworld"]["tasks"].keys())
        for task, states in report["appworld"]["tasks"].items():
            for grade in states.values():
                self.assertEqual(grade["required"], spec["appworld"]["tasks"][task]["requirements"])
                self.assertEqual(grade["passed"] + grade["failed"], grade["required"])
        bfcl = report["bfcl"]
        self.assertEqual(bfcl["grade_agreement"]["allocated"], 2 * bfcl["tasks"])
        self.assertEqual(bfcl["routing_criteria"], routing_criteria(bfcl["default_framework_router"]))
        for route in bfcl["default_framework_router"].values():
            self.assertEqual(route["common_request_audit"], "passed")
            self.assertEqual(route["native_return_attributed"],
                             route["native_callback_received"] == ["memory_append", "memory_retrieve"]
                             and route["native_value_in_next_tool_message"] and route["native_value_spans_in_next_input"] > 0)
