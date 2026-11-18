"""Check the separate rule, deferral, and public-evidence judgments of the one-block pilot."""

import unittest
from copy import deepcopy

from agent_memory_eval.contracts.core import ContractError, canonical, digest
from scripts.review_written_replay_48 import dimension, validate_annotations


class WrittenReplayReviewTests(unittest.TestCase):
    def test_partial_booking_cannot_pass_and_known_violation_remains_visible(self):
        row = dict(status="response", screen=dict(status="unparseable_or_unknown_arguments",
            checks=[dict(certificate_status="pass")], target_write_calls=2))
        self.assertEqual(dimension(row, "certificate_status"), "indeterminate")
        row["screen"]["checks"][0]["certificate_status"] = "violation"
        self.assertEqual(dimension(row, "certificate_status"), "violation")

    def test_no_booking_and_unresolved_bags_are_distinct_from_within_rule(self):
        row = dict(status="response", screen=dict(status="no_target_write", checks=[], target_write_calls=0))
        self.assertEqual(dimension(row, "baggage_status"), "no_booking")
        row["screen"].update(status="unresolved_target_checks", target_write_calls=1,
                             checks=[dict(baggage_status="unresolved_public_quantity")])
        self.assertEqual(dimension(row, "baggage_status"), "public_quantity_unresolved")

    def test_blind_annotation_is_bound_to_the_reply_and_its_case(self):
        reply = dict(content="How many bags will you check?")
        sha = digest(canonical(reply))
        packet = dict(entries=[dict(review_id="reply", case_id="airline14-r2-booking", reply=reply, reply_sha256=sha)])
        label = dict(review_id="reply", reply_sha256=sha, action="clarification_or_approval",
                     asks_needed_bag_quantity=True, payment_review="not_applicable", evidence="How many bags")
        doc = dict(format="written-replay-blind-annotations/v1", reviewer="reviewer", condition_assignments_visible=False,
                   packet_sha256=digest(canonical(packet)), annotations=[label])
        self.assertEqual(validate_annotations(packet, doc), {"reply":label})
        changes = (dict(reply_sha256="other"), dict(action="booking_proposal"), dict(evidence="zero bags"),
                   dict(asks_needed_bag_quantity=None), dict(payment_review="requests_new_approval"))
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ContractError):
                altered = deepcopy(doc)
                altered["annotations"][0].update(change)
                validate_annotations(packet, altered)
        with self.assertRaises(ContractError):
            validate_annotations(packet, dict(doc, annotations=[label, label]))
        with self.assertRaises(ContractError):
            validate_annotations(packet, dict(doc, condition_assignments_visible=True))
