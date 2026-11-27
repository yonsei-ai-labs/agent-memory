import unittest


def row(task, source, condition, success, *, attempt=0, origin="run", reason=None, tokens=None, calls=None,
        reads=(0, 0), preloaded=False, cost=None):
    graded = success is not None
    return dict(task=task, source=source, condition=condition, origin=origin, attempt=attempt,
                status="graded" if graded else "missing", success=success, missing_reason=None if graded else reason,
                tokens=tokens, business_calls=calls, reads_list=reads[0], reads_get=reads[1], preloaded=preloaded,
                cost_usd=cost)


def first_passes():
    return [row("A", "A-1", "no_memory", True, tokens=100, calls=3), row("A", "A-2", "no_memory", False),
            row("A", "A-3", "no_memory", None, reason="HTTP_503"), row("B", "B-1", "no_memory", False)]


class ScorecardTests(unittest.TestCase):
    def card(self, rows):
        from agent_memory_eval.study.scorecard import scorecard
        return scorecard(rows)

    def test_first_graded_attempt_is_the_slot_outcome(self):
        rows = first_passes() + [row("A", "A-1", "fact__preload", None, reason="HTTP_503"),
                                 row("A", "A-1", "fact__preload", True, attempt=1)]
        fact = self.card(rows)["conditions"]["fact__preload"]
        self.assertEqual((fact["assigned"], fact["graded"], fact["successes"]), (1, 1, 1))
        self.assertEqual(fact["graded_after_retry"], 1)
        self.assertEqual(fact["missing"], {})

    def test_reference_origin_precedes_its_recovery_in_listed_order(self):
        rows = first_passes() + [row("A", "A-1", "fact__preload", None, origin="ref", reason="HTTP_502"),
                                 row("A", "A-1", "fact__preload", False, origin="ref-recovery")]
        card = self.card(rows)
        self.assertEqual(card["conditions"]["fact__preload"]["successes"], 0)
        self.assertEqual(card["conditions"]["fact__preload"]["graded"], 1)

    def test_no_memory_baseline_is_the_task_mean_of_graded_first_passes(self):
        rows = first_passes() + [row("A", "A-1", "fact__preload", True), row("A", "A-2", "fact__preload", False),
                                 row("B", "B-1", "fact__preload", True)]
        card = self.card(rows)
        fact = card["conditions"]["fact__preload"]
        self.assertAlmostEqual(fact["expected_no_memory_successes"], 0.5 + 0.5 + 0.0)
        self.assertEqual(card["conditions"]["no_memory"]["successes"], 1)
        self.assertEqual(card["conditions"]["no_memory"]["missing"], {"HTTP_503": 1})
        self.assertEqual(card["tasks"], {"A": dict(first_pass_graded=2, first_pass_successes=1),
                                         "B": dict(first_pass_graded=1, first_pass_successes=0)})

    def test_recoveries_and_regressions_are_against_the_own_source(self):
        rows = first_passes() + [row("A", "A-1", "fact__preload", False), row("A", "A-2", "fact__preload", True),
                                 row("B", "B-1", "fact__preload", True), row("A", "A-3", "fact__preload", True)]
        fact = self.card(rows)["conditions"]["fact__preload"]
        self.assertEqual((fact["recoveries"], fact["regressions"], fact["paired_with_source"]), (2, 1, 3))

    def test_joint_success_effort_compares_with_the_own_source(self):
        rows = first_passes() + [row("A", "A-1", "fact__preload", True, tokens=160, calls=2)]
        joint = self.card(rows)["conditions"]["fact__preload"]["joint_success_with_source"]
        self.assertEqual(joint, dict(pairs=1, tokens=dict(condition=160.0, source=100.0, difference=60.0),
                                     business_calls=dict(condition=2.0, source=3.0, difference=-1.0)))

    def test_missing_reasons_reads_preload_and_cost_are_counted(self):
        rows = first_passes() + [
            row("A", "A-1", "fact__on_demand", None, reason="INPUT_SIZE_LIMIT", cost="0.010"),
            row("A", "A-2", "fact__on_demand", False, reads=(1, 1), cost="0.020"),
            row("B", "B-1", "fact__on_demand", True, reads=(1, 0), cost="0.030"),
            row("B", "B-1", "fact__preload", True, preloaded=True),
        ]
        card = self.card(rows)["conditions"]
        demand = card["fact__on_demand"]
        self.assertEqual(demand["missing"], {"INPUT_SIZE_LIMIT": 1})
        self.assertEqual(demand["reads"], dict(list_calls=2, get_calls=1, slots_with_any_read=2))
        self.assertEqual(demand["cost_usd"], "0.060")
        self.assertEqual(card["fact__preload"]["preloaded"], 1)

    def test_repeated_readers_are_separate_slots_paired_with_the_same_source(self):
        rows = first_passes() + [row("A", "A-1", "fact__preload", True), dict(row("A", "A-1", "fact__preload", False), repeat=2)]
        fact = self.card(rows)["conditions"]["fact__preload"]
        self.assertEqual((fact["assigned"], fact["graded"], fact["successes"], fact["regressions"]), (2, 2, 1, 1))

    def test_rows_must_name_a_known_status(self):
        from agent_memory_eval.contracts.core import ContractError
        broken = dict(first_passes()[0], status="started")
        with self.assertRaisesRegex(ContractError, "graded or missing"):
            self.card([broken])


if __name__ == "__main__":
    unittest.main()
