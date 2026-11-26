import unittest

from agent_memory_eval.adapters.tau_banking_opening import FIELDS, OPENING_OUTCOMES
from agent_memory_eval.analysis.receipt_review import join_bank_reviews, outcome_packet, score_outcome_review
from agent_memory_eval.contracts.core import ContractError, read_json
from agent_memory_eval.contracts.validation import ROOT


def claim_packet(product, receipt):
    quote = f"This call opened {product} for customer {receipt['arguments']['user_id']}."
    key, packet = outcome_packet(dict(title="Opening", body=quote), [receipt], "Open an account",
                                 claim_outcomes=OPENING_OUTCOMES, subject_fields=FIELDS,
                                 required_subject_fields=("account_class",))
    claim = dict(field="body", quote=quote, judgment="supported", outcome="opened", call_ids=[receipt["call_id"]],
                 subject=dict(user_id=receipt["arguments"]["user_id"], account_type=None, account_class=product))
    return key, packet, claim


def vote(claim):
    return dict(claims=[claim], no_outcome_reason=None)


class SubjectReviewTests(unittest.TestCase):
    def test_all_native_wrong_products_and_matching_public_receipts(self):
        pre = read_json((ROOT / "artifacts/pilots/20261126-banking-opening-cohort-preflight.json").read_bytes())
        count = 0
        for task, paths in pre["grader_controls"].items():
            correct = {r["call_id"]: r for r in paths["reference"]["receipts"]}
            for wrong in paths["wrong_product"]["receipts"]:
                ref = correct[wrong["call_id"]]
                product = ref["arguments"]["account_class"]
                _, packet, claim = claim_packet(product, wrong)
                with self.subTest(task=task, call=wrong["call_id"]):
                    with self.assertRaises(ContractError):
                        score_outcome_review(packet, vote(claim))
                    self.assertTrue(score_outcome_review(packet, vote(dict(claim, judgment="contradicted")))
                                    ["unsupported_or_contradicted"])
                    _, right_packet, right_claim = claim_packet(product, ref)
                    self.assertEqual(score_outcome_review(right_packet, vote(right_claim))["covered_receipts"], 1)
                    with self.assertRaises(ContractError):
                        score_outcome_review(right_packet, vote(dict(right_claim, judgment="contradicted")))
                count += 1
        self.assertEqual(count, 28)

    def test_subject_must_be_cited_and_unknown_return_cannot_contradict(self):
        receipt = dict(call_id="call-1", arguments=dict(user_id="owner", account_type="checking", account_class="Blue"),
                       outcome="opened")
        _, packet, claim = claim_packet("Blue", receipt)
        for subject in (dict(claim["subject"], user_id="invented"), dict.fromkeys(FIELDS)):
            with self.assertRaises(ContractError):
                score_outcome_review(packet, vote(dict(claim, subject=subject)))
        packet["receipts"] = [dict(receipt, outcome="unobserved")]
        with self.assertRaises(ContractError):
            score_outcome_review(packet, vote(dict(claim, judgment="contradicted")))
        for k, wrong in (("user_id", "other"), ("account_type", "savings")):
            _, p = outcome_packet(dict(title="Receipt", body=f"Opened Blue {wrong}"), [receipt], "",
                                  claim_outcomes=OPENING_OUTCOMES, subject_fields=FIELDS)
            bad = dict(claim, quote=p["memory"]["body"], subject={**dict.fromkeys(FIELDS), "account_class":"Blue", k:wrong})
            with self.assertRaises(ContractError):
                score_outcome_review(p, vote(bad))
            self.assertTrue(score_outcome_review(p, vote(dict(bad, judgment="contradicted")))["unsupported_or_contradicted"])

    def test_status_only_and_repeated_identical_items_have_distinct_denominators(self):
        receipt = dict(call_id="1", outcome="opened", arguments=dict(user_id="u", account_type="checking", account_class="A"))
        key, p, claim = claim_packet("B", receipt)
        key2, p2 = outcome_packet(dict(title="Status", body="The account was opened."), [receipt], "",
                                  claim_outcomes=OPENING_OUTCOMES, subject_fields=FIELDS, required_subject_fields=("account_class",))
        only = vote(dict(claim, quote=p2["memory"]["body"], subject=dict.fromkeys(FIELDS), judgment="outcome_only"))
        packets = {key:p, key2:p2}
        assignments = [dict(policy="p", items=[dict(id=str(i), version="v"+str(i), packet=k)
                                               for i,k in enumerate((key, key, key2))]),
                       dict(policy="p", items=[])]
        votes = {key:vote(dict(claim, judgment="contradicted")), key2:only}
        out = join_bank_reviews(packets, assignments, votes)
        c = out["policy_totals"]["p"]
        self.assertEqual((c["assigned_writers"], c["stored_banks"], c["rated_items"], c["fully_rated_banks"]), (2,1,3,1))
        self.assertEqual((c["items_with_unsupported_or_contradicted"], c["banks_with_unsupported_or_contradicted"]), (2,1))
        self.assertEqual((c["outcome_only_claims"], c["covered_receipts"]), (1,0))
        self.assertTrue(out["rows"][0]["rating"]["unsupported_or_contradicted"])
        self.assertIsNone(out["rows"][1]["rating"])
        del votes[key2]
        self.assertIsNone(join_bank_reviews(packets, assignments, votes)["rows"][0]["rating"])

    def test_another_product_opening_is_not_a_contradiction_of_a_supported_claim(self):
        actual = dict(call_id="call-1", outcome="opened", arguments=dict(user_id="owner", account_class="Blue"))
        _, packet, claim = claim_packet("Blue", actual)
        other = dict(actual, call_id="call-2", arguments=dict(actual["arguments"], account_class="Red"))
        packet["receipts"].append(other)
        with self.assertRaises(ContractError):
            score_outcome_review(packet, vote(dict(claim, judgment="contradicted", call_ids=["call-2"])))
        with self.assertRaises(ContractError):
            score_outcome_review(packet, vote(dict(claim, judgment="outcome_only", subject=dict.fromkeys(FIELDS))))
