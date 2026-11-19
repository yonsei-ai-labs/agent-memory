import unittest
from types import SimpleNamespace

from scripts.analyze_full_retail import compare
from scripts.replay_full_retail_fields import aligned_fields


class FullRetailComparisonTests(unittest.TestCase):
    def test_field_replays_require_matching_operations_requestors_and_keys(self):
        def call(name, arguments, requestor="assistant"):
            return SimpleNamespace(name=name, arguments=arguments, requestor=requestor)

        original = [call("update", {"item": "x", "address": {"a": 1, "b": 2}})]
        reordered = [call("update", {"address": {"b": 2, "a": 1}, "item": "x"})]
        changed = [call("update", {"address": {"a": 1, "b": 3}, "item": "y"})]
        self.assertEqual(
            aligned_fields(original, reordered), ("same_operations_and_arguments", [])
        )
        self.assertEqual(
            aligned_fields(original, changed),
            ("aligned", [(0, "address"), (0, "item")]),
        )
        for other in (
            [],
            original * 2,
            [call("cancel", {})],
            [call("update", {}, "user")],
        ):
            self.assertEqual(
                aligned_fields(original, other), ("different_operations", [])
            )
        self.assertEqual(
            aligned_fields(original, [call("update", {"item": "x"})]),
            ("different_argument_keys", []),
        )

    def test_first_pass_and_shared_control_comparisons_have_distinct_denominators(self):
        cfg = {"forms": ["fact"], "controls": ["no_memory", "empty_preload"]}
        values = [
            ("1", "a", False, True, False, False),
            ("2", "a", False, False, True, False),
            ("3", "b", True, False, True, True),
            ("4", "b", True, None, False, True),
        ]
        cells, rows = [], []
        for t, customer, first, memory, pure, empty in values:
            rows.append(
                dict(
                    task=t,
                    customer=customer,
                    prior_task=False,
                    prior_customer=False,
                    assertions=0,
                    operations=[],
                )
            )
            for stage, arm, score in [
                ("source", "no_memory", first),
                ("solve1", "fact_original", memory),
                ("solve1", "no_memory", pure),
                ("solve1", "empty_preload", empty),
            ]:
                cells.append(
                    dict(
                        task=t,
                        stage=stage,
                        arm=arm,
                        official=score,
                        run=t + stage + arm,
                    )
                )
        groups, pairs = compare(cells, rows, cfg)
        result = groups["all"]["forms"]["fact_original"]
        self.assertEqual(
            result["vs_first"], {"gain": 1, "both_fail": 1, "loss": 1, "missing": 1}
        )
        self.assertEqual(
            result["vs_controls"]["no_memory"], {"gain": 1, "loss": 2, "missing": 1}
        )
        self.assertEqual(
            result["both_controls_by_first"]["first_failure"],
            {"gain": 1, "split_controls": 1},
        )
        self.assertEqual(result["customer_equal_weight_difference"]["no_memory"], -0.5)
        self.assertAlmostEqual(
            result["task_equal_weight_difference"]["no_memory"], -1 / 3
        )
        self.assertEqual(len(pairs), 8)
