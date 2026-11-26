import json
import unittest
from collections import defaultdict, deque
from copy import deepcopy

from agent_memory_eval.analysis.bfcl_relevance import corruptions, trials
from agent_memory_eval.models.local_qwen import LocalQwen, public_reply, required_prefix, stop_ids
from agent_memory_eval.runner.bfcl_read_check import reader_config
from agent_memory_eval.runner.bfcl_relevance import grade_reply, normalized_first, read_choice, read_tools, worker_instruction
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.contracts.core import ContractError


TOOLS = [{"type": "function", "function": {"name": "weather", "parameters": {
    "type": "object", "required": ["city"], "properties": {"city": {"type": "string"}}}}}]
CALL = "<tool_call>\n<function=weather>\n<parameter=city>\nSeoul\n</parameter>\n</function>\n</tool_call>"


class QwenReplyTests(unittest.TestCase):
    def test_tool_only_descriptions_preserve_the_original_messages_and_tool_abi(self):
        source = {"worker_instruction": "Solve the task.", "memory_notice": "Previous notice."}
        descriptions = {"memory_list": "Browse notes from past situations.", "memory_get": "Read a selected note."}
        cfg = reader_config(source, {"memory_notice": "", "read_tool_descriptions": descriptions})
        self.assertEqual(worker_instruction(cfg, "on_demand"), source["worker_instruction"])
        self.assertEqual(reader_config(source, {})["memory_notice"], source["memory_notice"])
        standard = memory_tools(["memory_list", "memory_get"])
        customized = read_tools(cfg)
        for old, new in zip(standard, customized, strict=True):
            fn = old["function"]
            self.assertEqual(new, {"type": "function", "function": {**fn, "description": descriptions[fn["name"]]}})
        baseline = {"messages": [{"role": "system", "content": "Solve the task."}, {"role": "user", "content": "q"}],
                    "tools": TOOLS}
        wire = {**deepcopy(baseline), "tools": [*TOOLS, *customized]}
        self.assertEqual(normalized_first(wire, cfg, "on_demand", []), baseline)
        with self.assertRaises(ContractError):
            normalized_first({**wire, "tools": [*TOOLS, *standard]}, cfg, "on_demand", [])
        wire["messages"][0]["content"] += "\nA note exists."
        with self.assertRaises(ContractError):
            normalized_first(wire, cfg, "on_demand", [])
        for invalid in ({"memory_store": "write"}, {"memory_get": " "}, {"memory_list": 1}):
            with self.subTest(invalid=invalid), self.assertRaises(ContractError):
                memory_tools(["memory_list"], read_descriptions=invalid)

    def test_list_only_leaves_get_and_the_business_action_unconstrained(self):
        setting = ["memory_list"]
        self.assertEqual(read_choice(setting, 0), read_choice(True, 0))
        self.assertIsNone(read_choice(setting, 1))
        self.assertIsNone(read_choice(setting, 2))
        self.assertEqual(read_choice(True, 1)["function"]["name"], "memory_get")
        self.assertIsNone(read_choice(False, 0))
        for bad in (["memory_get"], ["memory_get", "memory_list"], "memory_list"):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                read_choice(bad, 0)

    def test_read_interface_advertises_actual_bound_and_stops_at_chat_end(self):
        old = read_tools({})
        limit = lambda tools: tools[0]["function"]["parameters"]["properties"]["limit"]
        self.assertNotIn("maximum", limit(old))
        self.assertEqual(limit(read_tools({"advertise_list_limit": True}))["maximum"], 1)
        self.assertEqual(limit(memory_tools(["memory_list"], page_size=4))["maximum"], 4)
        with self.assertRaises(ContractError):
            memory_tools(["memory_list"], page_size=0)
        self.assertEqual(stop_ids(248044, 248046, stop_at_chat_end=False), [248044])
        self.assertEqual(stop_ids(248044, 248046, stop_at_chat_end=True), [248044, 248046])
        self.assertEqual(stop_ids([248044, 248046], 248046, stop_at_chat_end=True), [248044, 248046])

    def test_matched_input_requires_the_registered_list_schema(self):
        cfg = {"memory_notice": "notes", "advertise_list_limit": True}
        wire = {"messages": [{"role": "system", "content": "worker\nnotes"}, {"role": "user", "content": "q"}],
                "tools": [*TOOLS, *read_tools(cfg)]}
        expected = {"messages": [{"role": "system", "content": "worker"}, {"role": "user", "content": "q"}], "tools": TOOLS}
        self.assertEqual(normalized_first(wire, cfg, "on_demand", []), expected)
        with self.assertRaises(ContractError):
            normalized_first(wire, {"memory_notice": "notes"}, "on_demand", [])

    def test_named_decoder_constraint_keeps_model_generated_arguments(self):
        tools = [*TOOLS, {"type": "function", "function": {"name": "memory_get", "parameters": {}}}]
        wire = {"tools": tools, "tool_choice": {"type": "function", "function": {"name": "memory_get"}}}
        self.assertEqual(required_prefix(wire), "<tool_call>\n<function=memory_get>\n")
        self.assertEqual(required_prefix({"tools": tools}), "")
        self.assertEqual(required_prefix({"tools": TOOLS, "tool_choice": "required"}), "<tool_call>\n<function=weather>\n")
        with self.assertRaises(ValueError):
            required_prefix({**wire, "tool_choice": {"type": "function", "function": {"name": "unregistered"}}})

    def test_chat_end_is_framing_and_does_not_erase_the_call(self):
        reply, error = public_reply(CALL + "<|im_end|>\n", TOOLS)
        self.assertIsNone(error)
        call, = reply["tool_calls"]
        self.assertEqual(call["function"]["name"], "weather")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"city": "Seoul"})
        self.assertEqual(public_reply("Forecast<|im_end|>\n", TOOLS)[0]["content"], "Forecast")
        for malformed in [CALL + "<|im_end|> extra", CALL.replace("</parameter>", ""), CALL + "unparsed"]:
            self.assertIsNotNone(public_reply(malformed, TOOLS)[1])

    def test_identical_wire_reuses_only_its_raw_generation_and_reparses_it(self):
        client = object.__new__(LocalQwen)
        client.model = {"id": "model"}
        client.saved = defaultdict(deque)
        wire = {"model": "model", "tools": TOOLS}
        raw = {"text": CALL + "<|im_end|>\n", "ended": True, "input_sha256": "abc", "prompt_tokens": 11,
               "completion_tokens": 3, "decode_error": "old framing error"}
        key = client.wire_key(wire)
        client.saved[key].append((raw, {"response_sha256": "original"}))
        self.assertNotEqual(client.wire_key({**wire, "tools": []}), key)
        result = client.respond(wire)
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(result["local_generation"]["text"], raw["text"])
        self.assertNotIn("decode_error", result["local_generation"])
        self.assertIn("decode_error", raw)
        self.assertEqual(result["reused_local_generation"]["response_sha256"], "original")
        self.assertFalse(client.saved[key])

    def test_wrong_call_still_counts_as_call_but_has_independent_diagnostics(self):
        reply, _ = public_reply(CALL, TOOLS)
        broken = deepcopy(reply)
        broken["tool_calls"][0]["function"]["arguments"] = "{}"
        scored = grade_reply(broken, "live_relevance", lambda output: not output, TOOLS)
        self.assertTrue(scored["valid"])
        self.assertEqual(scored["missing_required"], [["weather", "city"]])
        self.assertFalse(grade_reply(reply, "irrelevance", lambda output: not output, TOOLS)["valid"])

    def test_only_registered_memory_interface_can_be_removed(self):
        wire = {"messages": [{"role": "system", "content": "worker\nnotes"}, {"role": "user", "content": "question"}],
                "tools": TOOLS}
        with self.assertRaises(ContractError):
            normalized_first(wire, {"memory_notice": "notes"}, "on_demand", [])
        self.assertEqual(wire["messages"][0]["content"], "worker\nnotes")

    def test_scoring_probes_leave_observed_response_intact(self):
        original, _ = public_reply(CALL, TOOLS)
        saved = deepcopy(original)
        changes = corruptions(original, TOOLS)
        scores = {k: grade_reply(m, "live_relevance", lambda output: not output, TOOLS) for k, m in changes.items()}
        self.assertFalse(scores["remove_calls"]["valid"])
        self.assertTrue(scores["replace_function_name"]["valid"])
        self.assertEqual(scores["replace_function_name"]["undeclared_functions"], ["unregistered_control_function"])
        self.assertEqual(scores["remove_required_argument"]["missing_required"], [["weather", "city"]])
        self.assertEqual(original, saved)

    def test_missing_allocation_is_explicit_and_retains_the_assigned_question(self):
        row = dict(task="q", phase="solve", form="fact", mode="preload", status="not_started", success=None)
        study = {"format": "bfcl-local-two-pass/v1", "registration": {"tasks": ["q"]},
                 "tasks": {"q": {"allocations": [row]}}}
        trial, = trials(study, "solve", "fact", "preload")
        self.assertFalse(trial.started)
        self.assertIsNone(trial.success)
        self.assertEqual(trial.source, "q-source")
        study["tasks"]["q"]["allocations"].append(deepcopy(row))
        with self.assertRaises(ContractError):
            trials(study, "solve", "fact", "preload")
