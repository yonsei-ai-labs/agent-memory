"""Exact-wire repeats, explicit customer-query variants and first-action grading."""

from collections import Counter
from copy import deepcopy
import json
from tempfile import TemporaryDirectory
import unittest

from agent_memory_eval.contracts.core import ContractError, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.return_date import PROFILE, classify, ordered_slots
from agent_memory_eval.recording.journal import BlobStore
from agent_memory_eval.recording.query_variant import FORMAT, replay_decision


class ReturnDateCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = read_json((ROOT / PROFILE).read_bytes())

    def test_only_first_customer_content_can_change(self):
        with TemporaryDirectory() as directory:
            blobs = BlobStore(directory)
            messages = [dict(role="system", content="policy"), dict(role="assistant", content="Hello"),
                        dict(role="user", content="original")]
            parent = dict(wire=dict(messages=messages, seed=3), archive=dict(record_kind="observed"), response="original reply")
            parent_ref = blobs.put(dict(format="agent-memory-recorded-context/v1"))
            variant = dict(format=FORMAT, recorded_context_ref=parent_ref, customer_text="New follow-up")
            read = lambda b, r: deepcopy(parent) if r == parent_ref else self.fail("incorrect parent")
            actual = replay_decision(blobs, blobs.put(variant), read)
            self.assertEqual(actual["wire"]["messages"], messages[:2] + [dict(role="user", content="New follow-up")])
            self.assertEqual(messages[-1]["content"], "original")
            self.assertNotIn("response", actual)
            for invalid in (dict(variant, seed=7), dict(variant, customer_text="  "), dict(variant, customer_text="original")):
                with self.assertRaises(ContractError):
                    replay_decision(blobs, blobs.put(invalid), read)
            messages.insert(2, dict(role="assistant", content="later"))
            with self.assertRaises(ContractError):
                replay_decision(blobs, blobs.put(variant), read)

    def test_balanced_positions_and_individual_empty_slots(self):
        rows = ordered_slots(self.profile)
        self.assertEqual([r["slot"] for r in rows], list(range(1, 97)))
        for case in self.profile["cases"]:
            subset = [r for r in rows if r["case"] == case]
            self.assertEqual(Counter(r["arm"] for r in subset), dict.fromkeys(self.profile["arms"], 8))
            for offset in range(4):
                self.assertEqual(Counter(r["arm"] for r in subset[offset::4]), dict.fromkeys(self.profile["arms"], 2))

    def test_require_real_single_search_and_preserve_other_behaviors(self):
        case = "changed_date"
        def score(*actions, text=None):
            calls = [dict(function=dict(name=n, arguments=json.dumps(a))) for n, a in actions]
            return classify(case, self.profile["cases"][case], dict(content=text, tool_calls=calls))
        lookup = ("search_direct_flight", dict(origin="SEA", destination="JFK", date="2024-05-26"))
        self.assertTrue(score(lookup)["exact_action"])
        self.assertFalse(score(lookup, text='{"message":"I booked it."}')["exact_action"])
        self.assertFalse(score(lookup, lookup)["exact_action"])
        self.assertTrue(score(("search_direct_flight", dict(lookup[1], date="2024-05-25")))["stale_date_search"])
        self.assertTrue(score(lookup, ("book_reservation", {}))["unauthorized_mutation"])
        self.assertTrue(score(text='{"message":"Which date was the return?"}')["date_question"])

    def test_booking_compares_order_and_all_customer_authorized_fields(self):
        case = "approved_booking"
        rule = self.profile["cases"][case]
        def score(data):
            return classify(case, rule, dict(tool_calls=[dict(function=dict(name="book_reservation", arguments=json.dumps(data)))]))
        args = deepcopy(rule["expected_booking"])
        self.assertTrue(score(args)["exact_action"])
        args["destination"] = "JFK"
        args["flights"].reverse()
        args["nonfree_baggages"] = False
        self.assertEqual(score(args)["differing_fields"], ["destination", "flights", "nonfree_baggages"])
