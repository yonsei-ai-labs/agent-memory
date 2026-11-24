import copy
import re
import unittest
from dataclasses import replace
from types import SimpleNamespace

from agent_memory_eval.adapters.bfcl_task_start import open_simple_python
from agent_memory_eval.analysis.bfcl_decisions import decoded_choice
from agent_memory_eval.analysis.decision_attribution import (
    Action, Step, blinded_comparison, compare_episodes, compare_steps, public_history,
)
from agent_memory_eval.analysis.identifier_evidence import message_citations, trace_identifier
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions
from agent_memory_eval.analysis.tau2_attribution import fixed_choice, graded_episode
from agent_memory_eval.contracts.core import ContractError, read_json
from agent_memory_eval.contracts.validation import ROOT


def call(name, args):
    return Action.from_function(dict(name=name, arguments=args))


def step(actor="worker", rid="r", text=None, actions=(), fields=(), history="same"):
    return Step(actor, rid, "controls", history, text, tuple(actions), frozenset(fields))


def tool_pair(name, call_id, args, result):
    return [dict(role="assistant", content=None, tool_calls=[dict(id=call_id, type="function",
                 function=dict(name=name, arguments=args))]), dict(role="tool", tool_call_id=call_id, content=result)]


class DecisionAttributionTests(unittest.TestCase):
    def test_same_public_choice_and_distinct_settings_exposure_and_missing_evidence(self):
        a = step(text="Hello")
        self.assertEqual(compare_steps(a, replace(a, text="Hi"))["change"], "wording")
        self.assertEqual(compare_steps(a, replace(a, fields={"title"}))["status"], "matched_visible_memory")
        self.assertEqual(compare_steps(a, a)["status"], "matched_no_visible_memory")
        self.assertEqual(compare_steps(a, replace(a, controls="other-model"))["status"], "unmatched_controls")
        self.assertEqual(compare_steps(a, replace(a, history="new tool result"))["status"], "downstream")
        self.assertEqual(compare_steps(a, replace(a, history=None))["status"], "unverified_input")
        self.assertEqual(compare_steps(a, replace(a, fields=None))["status"], "unverified_exposure")
        self.assertEqual(compare_steps(a, None)["change"], None)

    def test_speech_and_immediate_lookup_can_end_in_the_same_ordered_calls(self):
        ask, lookup = step(text="Which order?"), step(actions=[call("lookup", {"order": 9})])
        real = [replace(lookup, fields={"body"}), step("customer", text="Thanks")]
        reference = [ask, step("customer", text="9"), replace(lookup, history="after clarification")]
        c = compare_episodes(reference, real)
        self.assertEqual(c["first_business_response"]["status"], "matched_visible_memory")
        self.assertEqual(c["first_business_response"]["change"], "call_vs_no_call")
        self.assertIsNone(c["first_ordered_call"])
        self.assertTrue(c["next_customer_changed"])

    def test_identical_response_wording_until_customer_changes_and_later_calls_differ(self):
        a, b = step("customer", text="One"), step("customer", text="Two")
        l = step(actions=[call("lookup", '{"id":1}')])
        r = replace(l, actions=(call("lookup", {"id": 2}),), fields={"body"})
        c = compare_episodes([a, l], [b, r])
        self.assertEqual(c["first_public"]["actor"], "customer")
        self.assertEqual(c["first_business_response"]["status"], "downstream")
        self.assertEqual(c["first_ordered_call"]["status"], "downstream")

    def test_different_result_before_first_changed_action_and_extra_batch_call(self):
        a = step(actions=[call("check", {"id": 1})])
        b = replace(a, actions=a.actions + (call("check", {"id": 2}),), history="changed inventory")
        c = compare_episodes([a], [b])
        self.assertEqual(c["first_public"]["status"], "downstream")
        self.assertEqual(c["first_business_response"]["change"], "function_or_batch")
        self.assertEqual(c["first_ordered_call"]["offset"], 1)
        self.assertEqual(c["first_ordered_call"]["status"], "unpaired_tail")

    def test_later_customer_is_distinct_from_immediate_next_speaker(self):
        a, b = step(text="Clarify"), step(text="Confirm")
        customer = step("customer", text="Hi")
        c = compare_episodes([a, step(actions=[call("lookup", {})]), customer], [b, customer], include_choices=True)
        self.assertIsNone(c["adjacent_customer_changed"])
        self.assertIs(c["next_customer_changed"], False)
        self.assertEqual(c["worker_inputs"], {"matched_no_visible_memory": 1, "unpaired_tail": 1})
        self.assertEqual(c["worker_choices"][1]["status"], "unpaired_tail")

    def test_blind_packet_hides_conditions_and_exposes_a_separate_reversible_key(self):
        history = [dict(role="user", content="Which order?")]
        a = step(rid="first-run", text="Which one?", history=public_history(history))
        b = replace(a, request="second-run", text=None, fields={"body"}, actions=(call("lookup", {"id": 2}),))
        packet, key = blinded_comparison(history, a, b)
        self.assertEqual(blinded_comparison(history, b, a), (packet, key))
        self.assertEqual(set(packet), {"pair_id", "public_history", "choices"})
        self.assertNotIn("first-run", str(packet))
        self.assertNotIn("body", str(packet))
        self.assertEqual(packet["choices"][key["request_order"].index("second-run")]["actions"][0]["name"], "lookup")
        with self.assertRaises(ContractError):
            blinded_comparison([dict(role="user", content="Different")], a, b)

    def test_canonical_arguments_preserve_order_of_lists_and_invalid_input(self):
        self.assertEqual(call("f", '{"y":2,"x":1}'), call("f", {"x": 1, "y": 2}))
        self.assertNotEqual(call("f", [1, 2]), call("f", [2, 1]))
        self.assertNotEqual(call("f", "{bad"), call("f", '"{bad"'))
        self.assertNotEqual(call("f", {"v": 1}), call("f", {"v": True}))
        action = call("f", {"v": 1, "tag": "observed"})
        self.assertTrue(step(actions=[action]).selects_single_call("f", {"v": 1}))
        self.assertFalse(step(actions=[action]).selects_single_call("f", {"v": True}))
        self.assertFalse(step(actions=[action, action]).selects_single_call("f", {"v": 1}))

    def test_public_normalization_preserves_linkage_and_domain_identifiers(self):
        history = tool_pair("lookup", "call-a", '{"order":"abc"}', '{"orders":["abc","xyz"]}')
        rhs = tool_pair("lookup", "call-b", '{"order": "abc"}', '{"orders":["abc","xyz"]}')
        self.assertEqual(public_history(history), public_history(rhs))
        rhs[0]["tool_calls"][0]["function"]["arguments"] = '{"order":"xyz"}'
        self.assertNotEqual(public_history(history), public_history(rhs))
        rhs[1]["tool_call_id"] = "missing"
        with self.assertRaises(ContractError):
            public_history(rhs)

    def test_only_recorded_private_returns_are_removed_and_mixed_business_is_kept(self):
        private = tool_pair("memory_list", "m", "{}", '{"items":[]}')
        business = tool_pair("lookup", "b", '{"order":1}', "current")
        journal = SimpleNamespace(returns={"m": ("list", private[1])})
        project = lambda msgs: RecordedDecisions.business_history(journal, msgs)
        self.assertEqual(project(private + business), business)
        mixed = copy.deepcopy(private[0])
        mixed["tool_calls"] += business[0]["tool_calls"]
        self.assertEqual(project([mixed, private[1], business[1]]), business)
        changed = {**private[1], "content": "unexpected"}
        with self.assertRaises(ContractError):
            project([private[0], changed])
        self.assertEqual(project([dict(role="user", content='{"items":[]}')]), [dict(role="user", content='{"items":[]}')])

    def test_identifier_source_storage_and_current_tool_do_not_imply_exposure(self):
        current = [dict(role="tool", content='{"orders":["#W1234567","#W1111111"]}')]
        past = [dict(role="user", content="See #W1234567, not #W12345678")]
        item = dict(id="i", version="v", title="Receipt", body="Order #W1234567")
        choice = dict(items=[item], public=current, visible=[], step=step(), focused=True)
        pattern = re.compile(r"#W1234567(?![0-9])")
        e = trace_identifier(dict(writer_runs=["w"]), choice, lambda _: ("s", past, {("i", "v"): (item, "e")}), pattern)
        self.assertEqual(len(e["sources"]["s"]), 1)
        self.assertEqual(e["stored"][0]["field"], "body")
        self.assertEqual(e["exposed"], [])
        self.assertEqual(e["current"][0]["role"], "tool")
        self.assertFalse(message_citations([dict(role="system", content="#W1234567")], pattern))

    def test_visible_identifier_requires_its_own_writer_source(self):
        past = [dict(role="user", content="My order is W1234567")]
        note = dict(id="i", version="v", title="W1234567", body="Question")
        writes = {"a": ("past-a", past, {}), "b": ("past-b", [], {("i", "v"): (note, "event-b")})}
        span = dict(request_id="r", item_id="i", version_id="v", field="title", pointer="/messages/1/content",
                    char_start=4, char_end=12, source={"kind": "memory_return"})
        choice = dict(items=[note], public=[], visible=[span], step=step(), focused=True)
        e = trace_identifier(dict(writer_runs=["a", "b"]), choice, writes.__getitem__, re.compile("W1234567"))
        self.assertTrue(e["sources"]["past-a"])
        self.assertEqual(e["exposed"][0]["source_run"], "past-b")
        self.assertFalse(e["exposed"][0]["identifier_in_own_source"])

    def test_bfcl_native_decoding_shares_the_same_comparison(self):
        row = dict(id="simple_python_9", question=[[dict(role="user", content="Find x.")]],
                   function=[dict(name="find", description="Find it", parameters={"type": "dict"})])
        start = open_simple_python(row, {"worker": "fixture"})
        a = decoded_choice(start, "a", [{"find": {"x": 1, "y": 2}}])
        b = decoded_choice(start, "b", [{"find": {"y": 2, "x": 1}}])
        self.assertEqual(compare_steps(a, b)["change"], "same")
        self.assertEqual(compare_steps(a, b)["status"], "unverified_exposure")
        c = decoded_choice(start, "c", [{"other": {"x": 1}}])
        self.assertEqual(compare_steps(a, c)["change"], "function_or_batch")
        with self.assertRaises(ContractError):
            decoded_choice(start, "bad", [{"find": {}, "other": {}}])

    def test_sealed_preload_retains_the_preparation_producer_and_matches_public_history(self):
        archive = ROOT / "runs/tau2-fresh-matched-01-continuation"
        if not (archive / "registration.json").exists():
            self.skipTest("Optional sealed fresh-matched archive is unavailable")
        reg = read_json((archive / "registration.json").read_bytes())["preflight"]
        case = next(c for c in reg["cases"] if c["task"] == "airline-0")
        base = archive / "task-airline-0"
        source_dir, _, solve_dir = [base / name for name in case["scheduled_stages"]]
        source, = read_json((source_dir / "allocations.json").read_bytes())
        row = next(r for r in read_json((solve_dir / "allocations.json").read_bytes()) if r["arm"] == "episode__preload")
        req = reg["read_instruction_sha256"]
        first, _ = graded_episode(source_dir, source, case, req)
        second, _ = graded_episode(solve_dir, row, case, req, source)
        pair = compare_episodes(first, second)
        self.assertEqual(pair["first_public"]["change"], "call_vs_no_call")
        self.assertEqual(pair["first_public"]["status"], "matched_visible_memory")
        self.assertEqual(pair["first_public"]["candidate_fields"], ["body", "title"])

    def test_registered_private_reads_and_stop_have_different_business_decision_coverage(self):
        root = ROOT / "runs/tau2-retail-conditional-retrieval-01"
        if not (root / "registration.json").exists():
            self.skipTest("Optional sealed order-choice archive is unavailable")
        jobs = read_json((root / "registration.json").read_bytes())["jobs"]["decision"]
        real = next(j for j in jobs if j["run"] == "r_e07af6ac60e57f09d29693e7243e5935")
        empty = next(j for j in jobs if j["task"] == real["task"] and j["arm"] == "conditional_empty_query")
        lhs, rhs = [fixed_choice(root / j["run"], j) for j in (empty, real)]
        self.assertTrue(lhs["at_registered_input"] and rhs["at_registered_input"])
        self.assertEqual(compare_steps(lhs["step"], rhs["step"])["status"], "matched_visible_memory")
        limit = next(j for j in jobs if j["run"] == "r_4d4c5f2032eaae8a30698c1d18bae9aa")
        capped = fixed_choice(root / limit["run"], limit)
        self.assertIs(capped["focused"], False)
        self.assertIsNone(capped["step"])
