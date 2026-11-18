import unittest

from agent_memory_eval.analysis.fresh_behavior import both_controls, field_rates, inspect_mutations


class FreshBehaviorTests(unittest.TestCase):
    def test_repeated_targets_ordered_flights_and_errors(self):
        def flight(n, day):
            return dict(flight_number=n, date=day)

        a = dict(reservation_id="R", flights=[flight("A", "D"), flight("B", "D")], payment_id="P")
        b = dict(reservation_id="R", flights=[flight("C", "E")], payment_id="P")
        tool = "update_reservation_flights"
        criteria = dict(
            actions=[dict(action_id=n, name=tool, arguments=args) for n, args in (("one", a), ("two", b))]
        )
        messages = []
        for i, (args, error) in enumerate(
            [
                (a, True),
                (dict(a, flights=list(reversed(a["flights"]))), False),
                (dict(a, reservation_id="S"), False),
            ]
        ):
            messages += [
                dict(role="assistant", tool_calls=[dict(id=str(i), name=tool, arguments=args)]),
                dict(role="tool", id=str(i), error=error, execution_status="executed"),
            ]
        result = inspect_mutations(messages, criteria, [tool])
        first, second = result["aligned"]
        self.assertEqual(first["observed_turn"], 2)
        self.assertTrue(first["fields"]["flight_dates"])
        self.assertFalse(first["fields"]["flight_numbers"])
        self.assertIsNone(second["observed_turn"])
        self.assertFalse(second["fields"]["flight_dates"])
        self.assertEqual(result["extra_write_turns"], [4])
        rates = field_rates([dict(arm="fact", mutations=result)])["fact"]
        self.assertEqual(rates["target"], {"matched": 1, "applicable": 2})

    def test_control_agreement_keeps_empty_memory_and_missingness(self):
        cells = [
            dict(task_id="k", repetition=1, arm=a, index=i, official_success=v, body_exposed=False)
            for i, (a, v) in enumerate((("fact", True), ("no_memory", False), ("empty_preload", False)))
        ]
        self.assertEqual(both_controls(cells, ["fact"])[0]["label"], "gain_vs_both")
        cells[-1]["official_success"] = None
        self.assertEqual(both_controls(cells, ["fact"])[0]["label"], "ungraded")
