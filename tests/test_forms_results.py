import unittest

from agent_memory_eval.analysis.forms_results import contrast
from scripts.export_two_pass_forms_diagnostics import timeline


class PairingTests(unittest.TestCase):
    def test_payment_timeline_links_native_and_public_tool_replies(self):
        name = "get_user_details"
        args = {"user_id": "sample"}
        response = '{"user_id":"sample","reservations":["A"],"payment_methods":{}}'
        native = [
            {"role": "assistant", "tool_calls": [{"id": "x", "name": name, "arguments": args}]},
            {"role": "tool", "id": "x", "content": response},
        ]
        projected = [
            {"role": "assistant", "tool_calls": [{"id": "x", "function": {
                "name": name, "arguments": '{"user_id":"sample"}'}}]},
            {"role": "tool", "tool_call_id": "x", "content": response},
        ]
        evidence = timeline(native, "23", [])
        self.assertEqual(evidence, timeline(projected, "23", []))
        self.assertEqual(evidence["profiles"][0]["result_index"], 1)
        self.assertEqual(evidence["profiles"][0]["returned_reservation_ids"], ["A"])

    def test_cancellation_timeline_retains_an_additional_certificate_write(self):
        messages = [
            {"role": "user", "content": "Please cancel the reservation."},
            {"role": "assistant", "tool_calls": [
                {"id": "a", "name": "cancel_reservation", "arguments": {"reservation_id": "A"}}]},
            {"role": "tool", "id": "a", "content": '{"reservation_id":"A","status":"cancelled"}'},
            {"role": "assistant", "tool_calls": [
                {"id": "b", "name": "send_certificate", "arguments": {"user_id": "sample", "amount": 50}}]},
            {"role": "tool", "id": "b", "content": "Certificate sent."},
        ]
        writes = timeline(messages, "42", ["A"])["business_actions"]
        self.assertEqual([a["name"] for a in writes], ["cancel_reservation", "send_certificate"])
        self.assertEqual(writes[-1]["arguments"]["amount"], 50)
        self.assertEqual(writes[-1]["returned"], "Certificate sent.")

    def test_partial_pairs_keep_task_weights_and_tight_missing_bounds(self):
        rows = []
        for t, rep, data in [("A", 1, [True, False]), ("A", 2, [True, False]),
                             ("B", 1, [False, True]), ("B", 2, [None, True])]:
            for arm, val in zip(("fact", "no_memory"), data):
                rows.append(dict(task_id=t, repetition=rep, arm=arm, index=len(rows), result=dict(official_success=val)))
        s = contrast(rows, "fact", "no_memory", {"A": "g1", "B": "g2"}, resamples=25)
        self.assertEqual(s["jointly_graded_pairs"], 3)
        self.assertEqual(s["counts"], dict(gain=2, loss=1, both_success=0, both_failure=0, ungraded=1))
        self.assertEqual(s["task_equal_difference_pp"], 0)
        self.assertEqual(s["possible_difference_pp"], [0, 25])
        self.assertEqual(s["leave_one_group_out_pp"], {"g1": -100, "g2": 100})
