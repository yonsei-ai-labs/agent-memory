import unittest

from scripts.analyze_full_retail import compare


class FullRetailComparisonTests(unittest.TestCase):
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
