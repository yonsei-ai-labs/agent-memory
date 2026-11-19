import unittest
from dataclasses import dataclass, replace

from scripts.analyze_retail_full_factors import (
    business_events,
    difference_kind,
    first_difference,
    named_values,
    public_message,
    transfer_position,
)
from scripts.replay_retail_full_actions import omitted_index, switched


@dataclass
class Call:
    name: str
    arguments: dict
    requestor: str = "assistant"

    def model_copy(self, *, update):
        return replace(self, **update)


class RetailDecisionFactorsTests(unittest.TestCase):
    def test_failed_or_rejected_actions_do_not_count_as_completed_work(self):
        def event(call_id, name):
            return dict(
                role="assistant",
                tool_calls=[
                    dict(id=call_id, name=name, arguments={"summary": "review"})
                ],
            )

        def reply(call_id, *, error=False, status="completed"):
            return dict(role="tool", id=call_id, error=error, execution_status=status)

        usage = [
            dict(role="user", content="I want to check this."),
            event("a", "cancel_pending_order"),
            reply("a", error=True),
            event("b", "cancel_pending_order"),
            reply("b", status="rejected"),
            event("c", "transfer_to_human_agents"),
            reply("c"),
        ]
        updates, transfers = business_events(usage)
        self.assertEqual(
            [(x["accepted"], x["success"]) for x in updates],
            [(True, False), (False, False)],
        )
        self.assertEqual(
            transfer_position(updates, transfers), "transfer_without_successful_change"
        )
        self.assertEqual(transfers[0]["last_user_turn"], 0)
        usage[2] = reply("a")
        self.assertEqual(
            transfer_position(*business_events(usage)), "transfer_after_change"
        )

    def test_public_comparison_preserves_language_and_ignores_generated_call_ids(self):
        a = dict(
            role="assistant",
            tool_calls=[
                dict(
                    id="first", requestor="assistant", name="read", arguments={"id": 1}
                )
            ],
        )
        b = dict(
            a, tool_calls=[dict(a["tool_calls"][0], id="second")], timestamp="later"
        )
        opening = dict(role="user", content="Blue please")
        left = [public_message(opening), public_message(a)]
        self.assertIsNone(
            first_difference(left, [public_message(opening), public_message(b)])
        )
        self.assertEqual(
            first_difference(
                left,
                [
                    public_message(dict(opening, content="Blue, please")),
                    public_message(b),
                ],
            )["turn"],
            0,
        )
        self.assertEqual(first_difference(left, left[:1])["right"], "end")

    def test_unique_omission_includes_failed_attempts_and_rejects_ambiguous_insertions(
        self,
    ):
        a, b = Call("address", {"zip": "123"}), Call("item", {"id": "new"})
        self.assertEqual(omitted_index([a, b], [b], [False, False]), 0)
        self.assertIsNone(omitted_index([a, b], [b], [True, False]))
        self.assertIsNone(omitted_index([b, b], [b], [False, False]))
        self.assertIsNone(
            omitted_index([a, b], [Call("item", {"id": "old"})], [False, False])
        )

    def test_joint_field_swap_accumulates_both_changes_and_keeps_original(self):
        a = [Call("item", {"ids": [1, 3], "payment": "a", "order": "fixed"})]
        b = [Call("item", {"ids": [2, 3], "payment": "b", "order": "other"})]
        changed = switched(a, b, [(0, "ids"), (0, "payment")])
        self.assertEqual(
            changed[0].arguments, {"ids": [2, 3], "payment": "b", "order": "fixed"}
        )
        self.assertEqual(
            a[0].arguments, {"ids": [1, 3], "payment": "a", "order": "fixed"}
        )
        notes = [dict(title="Replacement 11", body="Shared item 3 and alternative 2.")]
        self.assertEqual(named_values([1, 3], [2, 3], notes), {"1": False})
        self.assertEqual(named_values([2, 3], [1, 3], notes), {"2": True})

    def test_db_correct_language_failure_has_its_own_decision_category(self):
        loser = dict(
            db_match=True,
            failed_assertions=["amount"],
            transfer_position="no_transfer",
            updates=[],
        )
        self.assertEqual(difference_kind({}, loser), "natural_language_only")
