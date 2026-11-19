import unittest

from scripts.analyze_recheck_rounds import inspect_episode, mutation_key, status


class RetailTraceTests(unittest.TestCase):
    def test_exchange_pairs_can_move_together_but_cannot_cross(self):
        name = "exchange_delivered_order_items"
        base = dict(
            order_id="#A", item_ids=["old1", "old2"], new_item_ids=["new1", "new2"], payment_method_id="card"
        )
        expected = mutation_key(name, base)
        self.assertEqual(
            expected, mutation_key(name, dict(base, item_ids=["old2", "old1"], new_item_ids=["new2", "new1"]))
        )
        self.assertNotEqual(expected, mutation_key(name, dict(base, new_item_ids=["new2", "new1"])))

    def test_parallel_lookup_is_not_a_pre_action_observation(self):
        args = dict(order_id="#A", reason="no longer needed")
        criteria = dict(actions=[dict(name="cancel_pending_order", arguments=args)])
        messages = [
            dict(role="user", content="Yes, cancel that order."),
            dict(
                role="assistant",
                tool_calls=[
                    dict(id="a", name="get_order_details", arguments={"order_id": "#A"}),
                    dict(id="b", name="cancel_pending_order", arguments=args),
                ],
            ),
            dict(role="tool", id="a"),
            dict(role="tool", id="b"),
        ]
        result = inspect_episode(dict(messages=messages), criteria)
        self.assertFalse(result["observed_order_before_first_write"])
        self.assertTrue(result["exact_mutations"])
        self.assertEqual(result["first_write_lead"][-1]["text"], "Yes, cancel that order.")
        messages[-1]["error"] = True
        self.assertFalse(inspect_episode(dict(messages=messages), criteria)["exact_mutations"])

    def test_ungraded_is_missing_even_when_other_condition_passes(self):
        self.assertEqual(status(True, None), "missing")
        self.assertEqual(status(False, True), "loss")
