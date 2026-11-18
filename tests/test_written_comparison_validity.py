"""Evidence boundaries and real refusal behavior for the current comparison path."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.written_comparison import (
    case_packet, review_original_decisions, screen_reply, validate_rubrics,
)
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.writing_comparison import (
    _profile, audit_comparison, export_comparison, prepare_comparison, selected_action,
)


def booking(quantity=3, payment_ids=("voucher",)):
    args = dict(user_id="customer", total_baggages=quantity,
                payment_methods=[dict(payment_id=key, amount=10) for key in payment_ids])
    return dict(role="assistant", content=None, tool_calls=[dict(id="call", type="function",
        function=dict(name="book_reservation", arguments=canonical(args).decode()))])


class WrittenComparisonValidityTests(unittest.TestCase):
    def setUp(self):
        self.profile = read_json((ROOT / "protocols/drafts/tau2-written-memory-replay-v2.json").read_bytes())
        messages = [dict(role="system", content="At most one travel certificate per reservation."),
            dict(role="user", content="I need three checked bags."),
            dict(role="assistant", content=None, reasoning="PRIVATE", tool_calls=[dict(id="lookup", type="function",
                function=dict(name="get_user_details", arguments='{"user_id":"customer"}'))]),
            dict(role="tool", tool_call_id="lookup", content=canonical(dict(user_id="customer",
                payment_methods={"voucher":dict(source="certificate"), "second":dict(source="certificate"),
                                 "certificate_named_card":dict(source="credit_card")})).decode())]
        self.case = dict(case_id="case", request_sha256="a" * 64, wire=dict(messages=messages),
            original_public_reply=booking(), verified_selection=dict(tool="book_reservation",
                request_ordinal=1, observed_request_count=1, call_id="call"), original_episode_success=True)
        self.packet = case_packet([self.case], self.profile)
        def evidence(index):
            msg = self.packet["cases"][0]["messages"][index]
            return dict(message_index=index, message_sha256=digest(canonical(msg)), excerpt=msg["content"])
        self.rules = dict(format="agent-memory-written-comparison-rubrics/v1", status="provisional",
            packet_sha256=digest(canonical(self.packet)),
            reviewer=dict(id="fixture", kind="analysis_agent", independence_declared=False,
                          historical_outcomes_visible=True),
            scope="public_quantity_and_certificate_checks_not_full_policy_or_episode_success",
            cases=[dict(case_id="case", baggage=dict(interpretation="explicit_quantity", expected_quantity=3,
                evidence=evidence(1), note="Fixture explicitly asks for three, not zero."),
                certificates=dict(max_per_reservation=1, evidence=evidence(0)),
                approval_review="Not judged", other_policy_review="Not judged")])

    def screen(self, message=None, finish="tool_calls"):
        validate_rubrics(self.packet, self.rules)
        return screen_reply(self.packet["cases"][0], self.rules["cases"][0],
                            message if message is not None else booking(), finish_reason=finish)

    def test_v2_profile_refuses_unknown_scope_ready_and_untyped_selection(self):
        _profile(self.profile)
        for change in (lambda p:p.update(status="ready"), lambda p:p.update(unrecognized=True),
                       lambda p:p["research_coverage"].update(episode_success_measured=True),
                       lambda p:p["cases"][0]["selection"].update(request_ordinal=True),
                       lambda p:p["guards"].update(max_seconds=float("inf"))):
            p = deepcopy(self.profile)
            change(p)
            with self.assertRaises(ContractError):
                _profile(p)

    def test_selection_is_bound_to_first_action_not_an_arbitrary_valid_worker_request(self):
        values, events = {}, []
        for index, message in enumerate((dict(role="assistant", content="Checking"), booking(), booking())):
            request = "request-" + str(index)
            raw = dict(choices=[dict(message=message, finish_reason="tool_calls" if message.get("tool_calls") else "stop")])
            values[request] = raw
            events.extend([dict(type="model.request_started", payload=dict(request_id=request, actor_role="worker")),
                           dict(type="model.request_finished", payload=dict(request_id=request, status="response",
                                                                            response_ref=dict(sha256=request)))])
        class Blobs:
            def get(self, ref):
                return deepcopy(values[ref["sha256"]])
        case = dict(request_id="request-1", selection=dict(tool="book_reservation", request_ordinal=1, expected_request_count=2))
        self.assertEqual(selected_action(events, Blobs(), case)["observed_request_count"], 2)
        for request in ("request-0", "request-2"):
            with self.assertRaisesRegex(ContractError, "declared target-action"):
                selected_action(events, Blobs(), dict(case, request_id=request))
        case["selection"]["expected_request_count"] = 1
        with self.assertRaises(ContractError):
            selected_action(events, Blobs(), case)
        case["selection"]["expected_request_count"] = 2
        values["request-1"]["choices"][0]["finish_reason"] = "length"
        with self.assertRaisesRegex(ContractError, "complete target call"):
            selected_action(events, Blobs(), case)

    def test_packet_excludes_hidden_reasoning_and_official_episode_grade(self):
        data = canonical(self.packet).decode()
        self.assertNotIn("PRIVATE", data)
        self.assertNotIn("original_episode_success", data)
        self.assertIn("PRIVATE", canonical(self.case).decode())
        self.assertFalse(self.packet["historical_grades_included"])

    def test_evidence_checks_do_not_claim_semantics_or_independent_review(self):
        result = validate_rubrics(self.packet, self.rules)
        self.assertFalse(result["semantic_judgments_verified"])
        self.assertFalse(result["independent_adjudication_verified"])
        result = review_original_decisions(self.packet, self.rules)
        self.assertEqual(result["new_model_or_catalog_calls"], 0)
        self.assertIn("not_new_memory_effect", result["scope"])

    def test_forged_future_or_wrong_role_evidence_is_rejected(self):
        for change in (lambda r:r.update(packet_sha256="0"*64),
                       lambda r:r["cases"][0]["baggage"]["evidence"].update(message_index=99),
                       lambda r:r["cases"][0]["baggage"]["evidence"].update(excerpt="hidden answer"),
                       lambda r:r["cases"][0]["baggage"].update(evidence=r["cases"][0]["certificates"]["evidence"]),
                       lambda r:r["cases"].append(deepcopy(r["cases"][0]))):
            rules = deepcopy(self.rules)
            change(rules)
            with self.assertRaises(ContractError):
                validate_rubrics(self.packet, rules)

    def test_unresolved_quantity_does_not_become_hidden_zero_answer(self):
        rule = self.rules["cases"][0]["baggage"]
        rule.update(interpretation="unresolved", expected_quantity=None)
        for quantity in (0, 6):
            result = self.screen(booking(quantity))
            self.assertEqual(result["status"], "unresolved_target_checks")
            self.assertEqual(result["checks"][0]["baggage_status"], "unresolved_public_quantity")
        rule["expected_quantity"] = 0
        with self.assertRaisesRegex(ContractError, "assumed answer"):
            validate_rubrics(self.packet, self.rules)

    def test_explicit_positive_quantity_passes_without_implying_full_policy_success(self):
        result = self.screen()
        self.assertEqual(result["status"], "target_checks_passed")
        self.assertIsNone(result["official_success"])
        self.assertEqual(result["approval_status"], "not_automatically_judged")
        self.assertEqual(self.screen(booking(0))["status"], "target_violation_detected")

    def test_certificate_count_uses_public_method_source_not_id_prefix_or_hidden_db(self):
        self.assertEqual(self.screen(booking(payment_ids=("voucher", "second")))["status"], "target_violation_detected")
        result = self.screen(booking(payment_ids=("voucher", "certificate_named_card")))
        self.assertEqual(result["checks"][0]["certificate_count"], 1)
        self.assertEqual(result["status"], "target_checks_passed")
        self.assertEqual(self.screen(booking(payment_ids=("not_observed",)))["status"], "unresolved_target_checks")

    def test_unlinked_profile_cannot_supply_a_payment_answer(self):
        case = deepcopy(self.packet["cases"][0])
        case["messages"][3]["tool_call_id"] = "not_a_profile_call"
        result = screen_reply(case, self.rules["cases"][0], booking(), finish_reason="tool_calls")
        self.assertEqual(result["status"], "unresolved_target_checks")

    def test_unknown_public_payment_type_is_not_counted_as_zero_certificates(self):
        for source in (None, "unknown", ""):
            with self.subTest(source=source):
                case = deepcopy(self.packet["cases"][0])
                profile = read_json(case["messages"][3]["content"].encode())
                profile["payment_methods"]["voucher"]["source"] = source
                case["messages"][3]["content"] = canonical(profile).decode()
                result = screen_reply(case, self.rules["cases"][0], booking(), finish_reason="tool_calls")
                self.assertEqual(result["status"], "unresolved_target_checks")
                self.assertIsNone(result["checks"][0]["certificate_count"])

    def test_question_observation_and_malformed_or_partial_calls_are_not_success(self):
        result = self.screen(dict(role="assistant", content="How many bags?"), finish="stop")
        self.assertEqual(result["status"], "no_target_write")
        result = self.screen(dict(role="assistant", tool_calls=[dict(id="lookup", function=dict(name="get_user_details"))]))
        self.assertEqual(result["status"], "no_target_write")
        self.assertEqual(self.screen(finish="length")["status"], "incomplete_or_rejected_response")
        for reply in (booking(True), booking(payment_ids=("voucher", "voucher")),
                      dict(role="assistant", tool_calls=[dict(id="call", function=dict(name="book_reservation", arguments='{broken'))])):
            self.assertEqual(self.screen(reply)["status"], "unparseable_or_unknown_arguments")

    def test_complete_source_receipt_is_compared_not_only_aggregate_item_counts(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source").mkdir()
            (root / "source/log.jsonl").write_bytes(b"anchor")
            receipt = dict(format="agent-memory-writing-result/v1", source_root="source", raw_log_sha256=digest(b"anchor"),
                           audit=dict(allocations=10), by_feedback={})
            export = dict(metrics=dict(runs=[], audit=dict(allocations=9), by_feedback={}))
            with patch("agent_memory_eval.experiments.writing_comparison.asset_text", return_value=canonical(receipt).decode()), \
                    patch("agent_memory_eval.experiments.writing_comparison.collect", return_value=export), \
                    patch("agent_memory_eval.experiments.writing_comparison._cases", side_effect=AssertionError("must reject first")):
                with self.assertRaisesRegex(ContractError, "complete source audit"):
                    prepare_comparison(self.profile, root)

    def test_export_audit_rebuilds_manifest_and_rejects_changed_extra_or_missing_requests(self):
        request = dict(messages=[dict(role="user", content="ORIGINAL")])
        key = digest(canonical(request))
        manifest = dict(status="prepared_offline_draft", rows=[dict(request_sha256=key)],
                        candidate_cases=[], identical_input_multiplicity={key:1})
        with TemporaryDirectory() as folder:
            root, output = Path(folder), Path(folder) / "outputs/new"
            with patch("agent_memory_eval.experiments.writing_comparison.asset_text", return_value='{"source_root":"runs/source"}'), \
                    patch("agent_memory_eval.experiments.writing_comparison.prepare_comparison", return_value=(manifest, {key:request})):
                export_comparison(self.profile, root, output)
                self.assertEqual(audit_comparison(self.profile, root, output)["request_files_verified"], 1)
                raw = (output / "requests" / (key + ".json"))
                raw.write_bytes(canonical(dict(messages=[])))
                with self.assertRaisesRegex(ContractError, "request differs"):
                    audit_comparison(self.profile, root, output)
                raw.write_bytes(canonical(request))
                (output / "unexpected.json").write_text("{}")
                with self.assertRaisesRegex(ContractError, "unexpected files"):
                    audit_comparison(self.profile, root, output)
                (output / "unexpected.json").unlink()
                raw.unlink()
                with self.assertRaisesRegex(ContractError, "missing"):
                    audit_comparison(self.profile, root, output)


if __name__ == "__main__":
    unittest.main()
