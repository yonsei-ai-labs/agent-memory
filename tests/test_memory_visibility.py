"""The diagnostic must detect missing tools and lost or fabricated memory returns."""

import unittest
import json
from copy import deepcopy
from dataclasses import asdict
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent_memory_eval.analysis.memory_visibility import inspect_choice, inspect_visibility, match_dispatches
from agent_memory_eval.contracts.core import MemoryPolicy, RunContext, canonical
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.models.openrouter import OpenRouterClient, SpendGuard
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry


class MemoryVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.names = ["memory_list", "memory_get"]
        self.wire = dict(tools=memory_tools(self.names), tool_choice="auto")
        self.response = dict(choices=[dict(message=dict(role="assistant", content="Done"), finish_reason="stop")])

    def test_tool_names_in_text_cannot_substitute_for_transmitted_schemas(self):
        wire = dict(messages=[dict(role="system", content="Use memory_list and memory_get")], tools=[], tool_choice="auto")
        result = inspect_choice(wire, self.response, self.names)
        self.assertIn("memory_tool_schema_mismatch", result["issues"])
        self.assertFalse(result["read_tools_available"])
        self.assertFalse(result["read_choice_permitted"])

    def test_visible_tools_with_none_choice_are_not_available_actions(self):
        self.wire["tool_choice"] = "none"
        result = inspect_choice(self.wire, self.response, self.names)
        self.assertTrue(result["read_tools_available"])
        self.assertFalse(result["read_choice_permitted"])
        self.assertIn("read_tool_choice_blocked", result["issues"])

    def test_actual_raw_call_is_separate_from_a_textual_promise(self):
        self.response["choices"][0]["message"]["content"] = "I will use memory_get."
        result = inspect_choice(self.wire, self.response, self.names)
        self.assertTrue(result["content_mentions_memory_tools"])
        self.assertEqual(result["memory_calls"], [])
        self.response["choices"][0]["message"]["tool_calls"] = [dict(id="c1", function=dict(name="memory_get", arguments='{"id":"m1"}'))]
        self.assertEqual(inspect_choice(self.wire, self.response, self.names)["memory_calls"], [dict(id="c1", name="memory_get")])

    def test_return_matching_requires_same_run_call_id_and_tool(self):
        call = dict(run_id="r1", request_id="q1", id="c1", name="memory_get", seq=10)
        result = dict(run_id="r1", id="c1", name="memory_get", seq=11)
        self.assertEqual(match_dispatches([call], [result])["missing_results"], [])
        for field, value in (("run_id", "other"), ("id", "other"), ("name", "memory_list"), ("seq", 9)):
            with self.subTest(field=field):
                changed = dict(result, **{field: value})
                matched = match_dispatches([call], [changed])
                self.assertEqual(matched["missing_results"], [call])
                self.assertEqual(matched["unmatched_results"], [changed])

    def test_missing_or_duplicate_return_is_not_a_successful_dispatch(self):
        call = dict(run_id="r1", id="c1", name="memory_list", seq=10)
        result = dict(run_id="r1", id="c1", name="memory_list", seq=11)
        self.assertEqual(match_dispatches([call], [])["missing_results"], [call])
        self.assertEqual(len(match_dispatches([call], [result, result])["unmatched_results"]), 1)

    def test_transport_keeps_memory_schemas_auto_choice_and_raw_call(self):
        route = dict(only=["openai"], allow_fallbacks=False, require_parameters=True)
        model = dict(id="openai/gpt-6-luna", pricing=dict(prompt="0", completion="0"))
        request = dict(self.wire, model=model["id"], provider=route, stream=False,
                       messages=[dict(role="user", content="Check earlier work.")], max_completion_tokens=64)
        call = dict(id="c1", type="function", function=dict(name="memory_list", arguments="{}"))
        response = dict(usage=dict(cost=0), choices=[dict(message=dict(role="assistant", content=None, tool_calls=[call]))])
        sent = []
        def opener(wire, timeout):
            sent.append(wire.data)
            return BytesIO(canonical(response))
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "visibility-test-token"}):
            actual = OpenRouterClient(model, SpendGuard(max_calls=1, max_usd=0.1, max_seconds=30),
                                      route=route, opener=opener).respond(request)
        self.assertEqual(sent, [canonical(request)])
        self.assertEqual(inspect_choice(request, actual, self.names)["memory_calls"], [dict(id="c1", name="memory_list")])

    def test_saved_report_roundtrip_has_the_same_structure(self):
        class Worker:
            offline = True
            def respond(self, request):
                return dict(choices=[dict(message=dict(role="assistant", content="done"), finish_reason="stop")])
        ctx = RunContext("visibility-test", "r_fixture", "s_fixture", "fixture", "probe", "solve", "illustrative")
        policy = MemoryPolicy(True, "none", False, 1)
        with TemporaryDirectory() as folder, Journal(folder, ctx.experiment_id) as journal:
            memory = MemoryService(journal)
            bank = memory.create_bank(ctx.run_id, policy)
            state = deepcopy(journal.state)
            state["assignments"][ctx.run_id] = dict(policy=asdict(policy), task_key="fixture", bank_id=bank)
            journal.append([ctx.event("run.started", dict(bank_id=bank, start_snapshot_id=None,
                fresh_session=True, source_run_ids=[], task_ref=journal.blobs.put(dict(role="system", content="fixture"))))], state=state)
            registry = SourceRegistry(journal, ctx)
            source = registry.template(dict(message=dict(role="user", content="A fixture question")), roles=("worker",))
            template = registry.template(dict(model="synthetic-offline", tools=memory_tools(self.names), tool_choice="auto"), roles=("worker",))
            RecordingGateway(journal).call(ctx, registry, "worker", template, [(source, "/message")], Worker(), max_requests=1)
            journal.append([ctx.at("seal").event("run.ended", dict(termination_reason="user_stop",
                grading_status="not_available", end_snapshot_id=memory.freeze(bank)))])
            report = inspect_visibility(folder)
            self.assertEqual(json.loads(json.dumps(report)), report)
            self.assertEqual(report["summary"][0]["requests_with_both_read_tools"], 1)


if __name__ == "__main__":
    unittest.main()
