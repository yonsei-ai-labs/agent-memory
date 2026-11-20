import json
import unittest

from scripts.analyze_retail_retry_gains import (
    argument_changes,
    compare_controls,
    replay_edits,
)


class RetryGainTests(unittest.TestCase):
    def test_swap_uses_each_runs_accepted_index_and_preserves_failed_attempt(self):
        def item(value, success=True):
            return (
                "modify_pending_order_items",
                "assistant",
                {"new_item_ids": [value]},
                success,
            )

        winner = {
            "attempts": [item("invalid", False), item("correct")],
            "context": "a",
            "db_match": True,
        }
        loser = {"attempts": [item("wrong")], "context": "b", "db_match": False}
        seen = []

        def execute(task, context, encoded):
            values = [args["new_item_ids"][0] for _, _, args in json.loads(encoded)]
            seen.append((context, values))
            return {
                "match": values[-1] == "correct",
                "failed_calls": [0] if values[0] == "invalid" else [],
            }

        changes = argument_changes(winner, loser)
        self.assertEqual(
            [(c["winner_index"], c["loser_index"]) for c in changes], [(1, 0)]
        )
        (replay,) = replay_edits("task", winner, loser, changes, execute)
        self.assertFalse(replay["winner_with_loser_values"]["match"])
        self.assertTrue(replay["loser_with_winner_values"]["match"])
        self.assertEqual(seen[-2:], [("a", ["invalid", "wrong"]), ("b", ["correct"])])

    def test_missing_control_does_not_establish_exclusive_recovery(self):
        solver = {"official": True}
        for scores, expected in [
            ([False, False], "neither"),
            ([False, True], "one"),
            ([True, True], "both"),
            ([False, None], "incomplete"),
        ]:
            with self.subTest(scores=scores):
                self.assertEqual(
                    compare_controls(solver, [{"official": s} for s in scores]),
                    expected,
                )
