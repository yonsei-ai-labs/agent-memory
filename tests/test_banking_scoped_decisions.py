"""Preserve matching, technical missingness and call arguments in fixed-state scoring."""

import unittest

from scripts.analyze_banking_scoped_decisions import public_action, summarize


class BankingScopedDecisionTests(unittest.TestCase):
    def test_missing_outcomes_stay_out_of_discordances_and_repeats_share_a_cell(self):
        rows = []
        pairs = [
            ("investigate", "unsupported_state"),
            ("investigate", "investigate"),
            ("other", None),
            ("other", "investigate"),
        ]
        for repeat, pair in enumerate(pairs):
            for arm, label in zip(("incumbent", "scoped_candidate"), pair):
                rows.append(
                    {
                        "case": "opening",
                        "form": "fact",
                        "role": "before_handoff",
                        "arm": arm,
                        "repeat": repeat,
                        "label": label,
                        "flags": [],
                        "observed_id": label,
                        "tokens": 12,
                        "output_tokens": 2,
                        "confirmed_usd": "0",
                    }
                )
        result = summarize(rows)
        self.assertEqual(
            result["paired"],
            {"incumbent_only": 1, "both": 1, "missing": 1, "candidate_only": 1, "neither": 0},
        )
        candidate = result["by_arm"]["scoped_candidate"]
        self.assertEqual(
            (candidate["assigned"], candidate["observed"], candidate["targeted"]), (4, 3, 2)
        )
        self.assertEqual((candidate["observed_cells"], candidate["cells_with_both_outcomes"]), (1, 1))

    def test_masking_keeps_meaningful_arguments_and_text_but_drops_provider_ids(self):
        call = {
            "id": "provider-generated",
            "function": {"name": "lookup", "arguments": '{"id":"current"}'},
        }
        action = public_action({"message": {"content": "I can check.", "tool_calls": [call]}})
        self.assertEqual(
            action,
            {"text": "I can check.", "calls": [{"name": "lookup", "arguments": {"id": "current"}}]},
        )
        call["function"]["arguments"] = "malformed"
        self.assertEqual(
            public_action({"message": {"tool_calls": [call]}})["calls"][0]["arguments"], "malformed"
        )
