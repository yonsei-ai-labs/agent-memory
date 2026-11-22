import unittest

from agent_memory_eval.contracts.core import canonical, digest
from scripts.analyze_retail_two_visit import full_contrasts, grade_turns, review_ids
from scripts.code_retail_two_visit_turns import propose


class PublicReviewTests(unittest.TestCase):
    def packet(self, dialogue, order="#W1234567"):
        # The first seven public turns are the frozen opening, authentication and profile reads.
        turns = [{"role": "user", "text": "Fixed prefix"} for _ in range(7)]
        for i, text in enumerate(dialogue):
            turns.append({"role": "assistant" if i % 2 == 0 else "user", "text": text})
        index = len(turns)
        turns.extend(
            [
                {
                    "role": "action",
                    "name": "cancel_pending_order",
                    "arguments": {"order_id": order, "reason": "no longer needed"},
                },
                {"role": "result", "action": index, "ok": True, "text": "Cancelled"},
            ]
        )
        return {"turns": turns, "turns_sha256": digest(canonical(turns))}

    def test_corrected_wrong_proposal_and_consent_stay_linked(self):
        packet = self.packet(
            [
                "Should I cancel #W7654321 because it is no longer needed?",
                "No, I meant #W1234567.",
                "Should I cancel #W1234567 because it is no longer needed?",
                "Yes, please cancel it.",
            ]
        )
        label, reason = propose(packet)
        self.assertEqual(reason, "strict")
        self.assertEqual([r["disposition"] for r in label["responses"]], ["refused", "confirmed"])
        graded = grade_turns(packet["turns"], "#W1234567", 1, label)
        self.assertTrue(graded["consent_qualified"])
        self.assertEqual((graded["wrong_proposals"], graded["corrected_proposals"]), (1, 1))

    def test_separate_identification_and_authorization(self):
        packet = self.packet(
            [
                "Which order are you referring to?",
                "It is #W1234567.",
                "Should I cancel #W1234567 for the reason 'no longer needed'?",
                "Yes, please.",
            ]
        )
        label, _ = propose(packet)
        self.assertEqual(label["clarifications"], [7])
        self.assertEqual(label["proposals"][0]["turn"], 9)
        for question, answer in (
            ("Should I cancel #W1234567?", "Yes."),
            ("Should I cancel #W1234567 because it is no longer needed?", "Yes, but wait until tomorrow."),
            ("Should I cancel #W1234567 because it is no longer needed?", "Yes, cancel #W7654321."),
        ):
            with self.subTest(answer=answer, question=question):
                self.assertIsNone(propose(self.packet([question, answer]))[0])

    def test_index_requires_unique_registered_ids(self):
        jobs = [{"run": "run1"}, {"run": "run2"}]
        valid = review_ids(jobs, b"x" * 32, None)
        self.assertEqual(review_ids(jobs, None, valid), valid)
        for wrong in ({"run1": valid["run1"]}, dict.fromkeys(valid, valid["run1"])):
            with self.assertRaises(ValueError):
                review_ids(jobs, None, wrong)

    def test_secondary_rates_exclude_missing_and_retain_bounds(self):
        rows = []
        for cid, task, pair in (("u", "u1", (1, 1)), ("u", "u2", (0, 1)), ("v", "v1", (None, 1))):
            for arm, val in zip(
                ("preload", "empty_preload", "query", "empty_query"), (*pair, *pair), strict=True
            ):
                rows.append(
                    {
                        "mode": "m",
                        "arm": arm,
                        "customer": cid,
                        "task": task,
                        "completion": "error" if val is None else "completed",
                        "db_reward": val,
                    }
                )
        s = full_contrasts(rows, {"modes": ["m"]})["m/preload/empty_preload/db_success"]
        self.assertEqual((s["left_positive"], s["right_positive"], s["complete_pairs"]), (1, 2, 2))
        self.assertEqual(s["difference"], -0.5)
        self.assertEqual(s["bounds_over_all_registered"], [-0.75, -0.25])


if __name__ == "__main__":
    unittest.main()
