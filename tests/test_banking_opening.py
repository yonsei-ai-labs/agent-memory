import json
import unittest
from copy import deepcopy
from importlib.util import find_spec

from agent_memory_eval.adapters.tau_banking_opening import DISPATCH, OPEN, OPENING_OUTCOMES, account_effects, opening_receipts, verify_account_rows
from agent_memory_eval.analysis.receipt_review import join_outcome_reviews, outcome_packet, score_outcome_review
from agent_memory_eval.contracts.core import ContractError


class AccountReceiptTests(unittest.TestCase):
    def setUp(self):
        self.params = dict(user_id="user", account_type="checking", account_class="Product A")
        dispatch = dict(agent_tool_name=OPEN, arguments=json.dumps(self.params))
        self.call = dict(role="assistant", tool_calls=[dict(id="a", type="function", function=dict(
            name=DISPATCH, arguments=json.dumps(dispatch)))])
        self.success = ("Bank account opened successfully!\n  - Account ID: 100\n  - User ID: user\n"
                        "  - Account Type: checking\n  - Account Class: Product A\n  - Status: OPEN")
        self.result = dict(role="tool", content=self.success, tool_call_id="a")

    def test_only_linked_public_return_proves_opening(self):
        messages = [dict(role="tool", content=self.success, tool_call_id="x"),
                    dict(role="assistant", content="I opened it"), self.call,
                    dict(self.result, tool_call_id="wrong"), dict(self.result, requestor="user")]
        receipts = opening_receipts(messages)
        self.assertEqual(receipts[0]["outcome"], "unobserved")
        with self.assertRaises(ContractError):
            opening_receipts([self.call, self.result, self.result])
        self.assertEqual(opening_receipts(messages+[self.result])[0]["outcome"], "opened")
        self.assertEqual(opening_receipts([self.call, dict(self.result, content=self.success.replace("Product A", "B"))])[0]["outcome"], "unrecognized")

    def test_native_and_wire_shapes_agree_and_other_product_is_counted(self):
        args = json.loads(self.call["tool_calls"][0]["function"]["arguments"])
        native = dict(role="assistant", tool_calls=[dict(id="a", name=DISPATCH, arguments=args)])
        r = opening_receipts([native, dict(role="tool", requestor="assistant", id="a", content=self.success)])
        self.assertEqual(r, opening_receipts([self.call, self.result]))
        counts = account_effects([dict(self.params, account_class="Requested B")], r)
        self.assertEqual((counts["all_target_accounts_opened"], counts["other_accounts_opened"]), (False, 1))
        self.assertEqual(verify_account_rows(set(), {"100": self.params}, r), 1)
        with self.assertRaises(ContractError):
            verify_account_rows(set(), {"100": dict(self.params, user_id="other")}, r)

    def test_refusal_and_invalid_request_differ_from_missing_reply(self):
        for content, label in [("Error: Account eligibility requirements not met.", "eligibility_rejected"),
                               ("Error: Tool has not been unlocked.", "request_rejected"),
                               ("Failed to open account: ID may already exist.", "opening_failed"),
                               ("Requested", "unrecognized")]:
            r = opening_receipts([self.call, dict(self.result, content=content)])
            self.assertEqual(r[0]["outcome"], label)
            self.assertFalse(account_effects([self.params], r)["all_target_accounts_opened"])

    @unittest.skipUnless(find_spec("tau2"), "Requires the pinned native banking environment")
    def test_required_read_is_part_of_the_sealed_database_hash(self):
        from tau2.data_model.message import AssistantMessage, ToolCall
        from tau2.domains.banking_knowledge.environment import get_environment, get_tasks
        from agent_memory_eval.adapters.tau_banking_opening import observed_accounts

        task = next(t for t in get_tasks() if t.id == "task_055")
        reference = task.evaluation_criteria.actions[:3]
        read = reference[-1]
        self.assertEqual((read.name, read.arguments["agent_tool_name"]), (DISPATCH, "get_all_user_accounts_by_user_id_3847"))
        native = get_environment(task=task, retrieval_variant="grep_only",
                                 read_log_allowlist={read.arguments["agent_tool_name"]})
        native.set_state(initialization_data=None, initialization_actions=None, message_history=[])
        history = []
        for action in reference:
            call = ToolCall(id=action.action_id, requestor=action.requestor, name=action.name, arguments=action.arguments)
            reply = native.get_response(call)
            self.assertFalse(reply.error)
            history.extend([AssistantMessage(role="assistant", tool_calls=[call]), reply])
        unlogged = get_environment(task=task, retrieval_variant="grep_only")
        unlogged.set_state(initialization_data=None, initialization_actions=None, message_history=history, strict=True)
        self.assertNotEqual(unlogged.get_db_hash(), native.get_db_hash())
        messages = [m.model_dump(mode="json") for m in history]
        audit = observed_accounts(task, messages, native.get_db_hash())
        self.assertEqual((audit["receipts"], audit["new_account_rows"]), ([], 0))

    def test_notes_have_separate_accuracy_and_coverage_denominators(self):
        key, packet = outcome_packet(dict(title="Result", body="We did open it."), opening_receipts([self.call, self.result]), "Open an account",
                                     claim_outcomes=OPENING_OUTCOMES)
        claim = dict(field="body", quote="did open it", judgment="supported", outcome="opened", call_ids=["a"])
        rating = dict(claims=[claim], no_outcome_reason=None)
        scored = score_outcome_review(packet, rating)
        self.assertFalse(scored["unsupported_or_contradicted"])
        self.assertEqual((scored["covered_receipts"], scored["observable_receipts"]), (1, 1))
        with self.assertRaises(ContractError):
            score_outcome_review(packet, dict(rating, claims=[dict(claim, quote="made up")]))
        with self.assertRaises(ContractError):
            score_outcome_review(packet, dict(rating, claims=[dict(claim, outcome="request_rejected")]))
        omitted = score_outcome_review(packet, dict(claims=[], no_outcome_reason="explicitly_unknown"))
        self.assertEqual(omitted["omitted_call_ids"], ["a"])
        self.assertFalse(omitted["unsupported_or_contradicted"])
        rows = [dict(task="t", form="fact", policy=p, packet=k) for p,k in
                [("existing", key), ("candidate", None)]]
        summary = join_outcome_reviews({key: packet}, rows, {key: rating})["policy_totals"]
        self.assertEqual(summary["existing"]["rated"], 1)
        self.assertEqual(summary["candidate"]["rated"], 0)
        self.assertEqual(summary["candidate"]["assigned_writers"], 1)
        corrupted = deepcopy(packet)
        corrupted["memory"]["body"] += "Changed"
        with self.assertRaises(ContractError):
            join_outcome_reviews({key: corrupted}, rows, {})


    def test_a_second_adapter_owns_its_outcome_vocabulary(self):
        receipt = dict(call_id="i", outcome="delivered")
        _, packet = outcome_packet(dict(title="Dispatch", body="Parcel arrived"), [receipt], "Track my shipment",
                                   claim_outcomes=["delivered", "returned"])
        claim = dict(field="body", quote="Parcel arrived", judgment="supported", outcome="delivered", call_ids=["i"])
        self.assertEqual(score_outcome_review(packet, dict(claims=[claim], no_outcome_reason=None))["covered_receipts"], 1)
