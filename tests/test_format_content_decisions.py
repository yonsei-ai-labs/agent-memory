from collections import Counter
from copy import deepcopy
import unittest

from agent_memory_eval.analysis.format_content import literal_quote, review_packet, summarize, validate_labels
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.format_content import PROFILE, classify, matched_notes, quote_rule, schedule


class FormatContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = read_json((ROOT / PROFILE).read_bytes())

    def test_same_propositions_and_order_in_all_four_presentations(self):
        notes = matched_notes(self.profile)
        self.assertEqual(len({n["body"] for n in notes.values()}), 4)
        for note in notes.values():
            offsets = [note["body"].index(p) for p in self.profile["propositions"]]
            self.assertEqual(offsets, sorted(offsets))
        for template in ("{0} {1} {2} {3} {4}", "{1} {0} {2} {3} {4} {5}", "{0} {1} {2} {3} {4} {5} {5}"):
            changed = deepcopy(self.profile)
            changed["renderings"]["fact"] = template
            with self.assertRaises(ContractError):
                matched_notes(changed)

    def test_each_arm_has_four_positions_and_identical_mean_rank(self):
        rows = schedule(self.profile)
        self.assertEqual(list(range(1, 225)), [r["slot"] for r in rows])
        self.assertEqual(set(Counter(r["block"] for r in rows).values()), {14})
        for case in self.profile["cases"]:
            by_arm = {a: [] for a in self.profile["arms"]}
            positions = []
            for repeat in range(1, 5):
                group = [r for r in rows if (r["case"], r["repeat"]) == (case, repeat)]
                positions.append((group[0]["block"] - 1) % 4)
                for pos, row in enumerate(group, 1):
                    by_arm[row["arm"]].append(pos)
            self.assertEqual(set(positions), {0, 1, 2, 3})
            self.assertTrue(all(len(set(pos)) == 4 and sum(pos) == 30 for pos in by_arm.values()))

    def test_quote_uses_tier_cabin_person_count_and_actual_bags(self):
        policy = """If the booking user is a silver member:
  - 1 free checked bag for each basic economy passenger
  - 2 free checked bag for each economy passenger
- If the booking user is a gold member:
  - 3 free checked bag for each economy passenger
- Each extra baggage is 50 dollars."""
        for case in ("previous_tier", "changed_tier"):
            spec = self.profile["cases"][case]
            self.assertEqual(quote_rule(policy, spec), spec["expected"])
        fewer = dict(self.profile["cases"]["changed_tier"], requested_per_passenger=1, expected=dict(free=2, paid=0, fee_usd=0))
        self.assertEqual(quote_rule(policy, fewer), fewer["expected"])
        with self.assertRaises(ContractError):
            quote_rule(policy.replace("50 dollars", "75 dollars"), fewer)

    def test_mask_hides_condition_and_counts_and_distinguishes_safe_alternative(self):
        case = "changed_tier"
        rule = self.profile["cases"][case]
        proper = classify(case, rule, {"content": '{"message":"free=6, paid=2, fee_usd=$100.00"}'})
        wording = classify(case, rule, {"content": "Six go free, two cost a total of one hundred dollars."})
        wrong = classify(case, rule, {"content": "free=4, paid=4, fee_usd=200"})
        query = classify(case, rule, {"tool_calls": [{"function": {"name": "get_user_details", "arguments": "{}"}}]})
        raw = {"rows": [dict(case=case, repeat=r, arm=arm, score=s) for r, arm, s in (
            (1, "empty", proper), (2, "empty", proper), (1, "matched_fact", wrong),
            (2, "matched_fact", wording), (1, "other_fact", query))]}
        packet = review_packet(raw, self.profile)
        self.assertEqual(len(packet["replies"]), 3)
        self.assertNotIn("matched_fact", str(packet))
        self.assertIsNone(query["review_id"])
        labels = {s["review_id"]: dict(status="asserted", values=v) for s, v in
                  [(proper, rule["expected"]), (wording, rule["expected"]), (wrong, self.profile["cases"]["previous_tier"]["expected"])]}
        review = dict(packet_sha256=digest(canonical(packet)), labels=labels)
        result = summarize(raw, self.profile, review)
        self.assertEqual(result["counts"][case]["matched_fact"]["stale_tier_quote"], 1)
        self.assertEqual(result["paired_to_empty"][case]["matched_fact"]["lost"], 1)
        self.assertFalse(query["unauthorized_mutation"])
        self.assertEqual(literal_quote(proper["public_text"]), rule["expected"])
        tampered = deepcopy(review)
        tampered["labels"][proper["review_id"]]["values"]["free"] = 4
        with self.assertRaises(ContractError):
            validate_labels(packet, tampered)
        with self.assertRaises(ContractError):
            validate_labels(packet, dict(review, labels={}))
