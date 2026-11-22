import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.recording.journal import Journal
from scripts.analyze_retail_order_choice_replay import contrast, verify_slot
from scripts.audit_retail_order_choice import comparable_hash, note_label, profile_state
from scripts.run_retail_order_choice_replay import execute, treatment


def call(name, args, cid):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": canonical(args).decode()}}


class ScriptedWorker:
    offline = True

    def __init__(self, calls):
        self.calls = iter(calls)
        self.wires = []

    def respond(self, wire):
        self.wires.append(wire)
        calls = next(self.calls)
        return {"id": f"example-{len(self.wires)}", "choices": [{"finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "reasoning": "private", "tool_calls": calls}}]}


class RetailOrderChoiceTests(unittest.TestCase):
    def setUp(self):
        self.base = dict(model="synthetic-offline", messages=[dict(role="system", content="Retail policy"),
            dict(role="user", content="Could you look up my order?")], tools=[])
        self.bank = [dict(id="m_order", version="v_order", title="Lamp observation", body="Seen in #W1234567.")]
        self.instructions = dict(preload="Optional history.", query="You may read prior notes.")
        self.select = call("get_order_details", {"order_id": "#W1234567"}, "business")
        self.list_call = call("memory_list", {}, "list")
        self.get_call = call("memory_get", {"id": "m_order"}, "get")

    def run_case(self, arm, replies, maximum):
        policy, entries, initial = treatment(self.base, arm, self.bank, self.instructions)
        slot = dict(run="r_sample", arm=arm, initial_sha256=digest(canonical(initial)))
        client = ScriptedWorker(replies)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with Journal(root, "order_choice_sample", contract_version="1.2") as journal:
                execute(journal, slot, (self.base, policy, entries, initial, []), client,
                        ExecutionLimits(maximum, 0, 0, 65536), None)
            model = {"pricing": {"prompt": 0, "completion": 0}, "identity": None}
            result = verify_slot(root, slot, (self.base, policy, entries, initial, []), "#W1234567", model)
        return result, client.wires

    def test_on_demand_never_reveals_a_body_before_get(self):
        result, wires = self.run_case("query", [[self.list_call], [self.get_call], [self.select]], 4)
        self.assertNotIn("Lamp observation", str(wires[0]))
        self.assertIn("Lamp observation", str(wires[1]))
        self.assertNotIn("Seen in #W1234567", str(wires[1]))
        self.assertIn("Seen in #W1234567", str(wires[2]))
        self.assertNotIn("private", str(wires[1:]))
        self.assertEqual((result["behavior"], result["exposed_fields"]), ("focused_target", ["body", "title"]))
        self.assertEqual(result["returned_memory_events"], 2)

    def test_each_mode_matches_its_empty_interface_and_the_pure_input(self):
        full, _ = self.run_case("preload", [[self.select]], 1)
        empty, _ = self.run_case("empty_preload", [[self.select]], 1)
        _, pure_wires = self.run_case("no_memory", [[self.select]], 1)
        query, query_wires = self.run_case("query", [[self.select]], 4)
        _, empty_query_wires = self.run_case("empty_query", [[self.select]], 4)
        self.assertEqual(query_wires, empty_query_wires)
        self.assertEqual(pure_wires, [self.base])
        self.assertEqual(full["exposed_fields"], ["body", "title"])
        self.assertEqual(empty["exposed_fields"], [])
        self.assertEqual(query["exposed_fields"], [])

    def test_mixed_batch_stops_without_dispatched_tools(self):
        result, wires = self.run_case("query", [[self.list_call, self.select]], 4)
        self.assertEqual(len(wires), 1)
        self.assertEqual(result["behavior"], "includes_target_with_other_calls")
        self.assertEqual(result["returned_memory_events"], 0)

    def test_last_memory_round_remains_unobserved(self):
        result, wires = self.run_case("empty_query", [[self.list_call]], 1)
        self.assertEqual((result["error"], result["behavior"]), ("MEMORY_ROUND_LIMIT", "unobserved"))
        self.assertEqual(len(wires), 1)

    def test_wrong_order_and_long_sku_do_not_become_target_evidence(self):
        orders = ["#W1234567", "#W2222222"]
        notes = [dict(title="Another order", body="#W2222222 and product 1234567890")]
        self.assertEqual(note_label(notes, orders, orders[0]), ("other_only", [orders[1]]))
        self.assertEqual(note_label([], orders, orders[0]), ("writer_empty", []))

    def test_matched_contrast_keeps_missing_out_of_customer_average(self):
        rows = []
        for source, arm_a, arm_b in (("1", "focused_target", "other_order_only"),
                                     ("2", "unobserved", "focused_target")):
            for arm, behavior in (("preload", arm_a), ("empty_preload", arm_b)):
                rows.append(dict(customer="u", task="t", source=source, repeat=1, arm=arm, behavior=behavior))
        result = contrast(rows, "preload", "empty_preload")
        self.assertEqual(result["pair_counts"], {"left_only": 1, "missing": 1})
        self.assertEqual(result["difference"], 1)

    def test_authentication_result_must_precede_profile_call(self):
        auth = call("find_user_id_by_email", {"email": "a@example.test"}, "a")
        get = call("get_user_details", {"user_id": "u"}, "p")
        profile = {"role": "tool", "tool_call_id": "p", "content": json.dumps({"user_id": "u", "orders": []})}
        found = {"role": "tool", "tool_call_id": "a", "content": "u"}
        assistant = lambda calls: {"role": "assistant", "tool_calls": calls}
        customer = digest(b"u")
        self.assertIsNone(profile_state({"messages": [assistant([auth, get]), found, profile]}, customer))
        wire = {"messages": [assistant([auth]), found, assistant([get]), profile]}
        self.assertIsNotNone(profile_state(wire, customer))
        renamed = json.loads(json.dumps(wire).replace('"a"', '"new_auth"').replace('"p"', '"new_profile"'))
        self.assertEqual(comparable_hash(wire), comparable_hash(renamed))
        renamed["messages"][1]["content"] = "other"
        self.assertNotEqual(comparable_hash(wire), comparable_hash(renamed))
