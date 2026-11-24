"""Feedback isolation and real recording/service routing with synthetic transports."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.memory_writing import audit_memory_writes
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, RunContext, canonical
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.memory.writing import (
    REQUIRED_STORE_CONTRACT, WRITER_COMPLETION_CONTRACT, WriterFeedback, freeze_public_experience, write_from_experience,
    writer_reply_error, writer_response_error,
)
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.recording.provenance import SourceRegistry


class ScriptedWriter:
    offline = True

    def __init__(self, *, empty=False, business=False):
        self.requests, self.empty, self.business = [], empty, business

    def respond(self, request):
        self.requests.append(request)
        if self.empty or len(self.requests) > 1:
            message = dict(role="assistant", content="Finished.")
        else:
            feedback = json.loads(request["messages"][2]["content"])
            message = dict(role="assistant", content=None, tool_calls=[dict(id="write", type="function",
                function=dict(name="book_reservation" if self.business else "memory_store",
                    arguments=json.dumps(dict(title="A lesson", body="Synthetic lesson: " + feedback["level"]))))])
        return dict(choices=[dict(message=message)])


class MemoryWritingTests(unittest.TestCase):
    def test_required_form_accepts_only_a_persisted_public_store(self):
        for empty in (True, False):
            with self.subTest(empty=empty):
                client = ScriptedWriter(empty=empty)
                result = self.write(WriterFeedback("none"), client, completion_contract=REQUIRED_STORE_CONTRACT)
                self.assertEqual([r["tool_choice"] for r in client.requests], ["required"])
                self.assertEqual(result["status"], "error" if empty else "completed")
                self.assertEqual(result["memory_stores"], 0 if empty else 1)
                self.assertEqual(result["error_code"], "WRITER_REQUIRED_STORE_MISSING" if empty else None)
                self.assertEqual(audit_memory_writes(self.folder.name)["status"], "passed")

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.journal = Journal(self.folder.name, "writing-test", contract_version="1.2")
        self.addCleanup(self.journal.close)
        self.ctx = RunContext("writing-test", "source", "source-session", "source", "collection", "solve")
        task = self.journal.blobs.put(dict(role="system", content="Public policy"))
        self.journal.append([self.ctx.at("prepare").event("run.started", dict(bank_id=None,
            start_snapshot_id=None, fresh_session=True, source_run_ids=[], task_ref=task))])
        public = dict(role="assistant", content="PUBLIC_OBSERVATION", reasoning="PRIVATE_REASONING")
        self.journal.append([self.ctx.event("business.transition", dict(public_message_ref=self.journal.blobs.put(public)))])
        # Private simulator state and grading details are deliberately available only outside public producers.
        self.journal.append([self.ctx.event("business.transition", dict(actor_role="user_simulator",
            model_message_ref=self.journal.blobs.put(dict(role="system", content="PRIVATE_SIMULATOR"))))])
        self.journal.append([self.ctx.at("grade").event("outcome.scored", dict(grading_status="graded",
            official_success=False, official_score=0.0,
            official_artifact_ref=self.journal.blobs.put(dict(answer="HIDDEN_GRADING_ANSWER"))))])
        self.journal.append([self.ctx.at("seal").event("run.ended", dict(termination_reason="synthetic_fixture",
            grading_status="graded", end_snapshot_id=None))])
        self.experience = freeze_public_experience(self.journal, "source")
        self.policy = MemoryPolicy(True, "post_task", False, 2, read_mode="none")

    def write(self, feedback, client=None, **kwargs):
        return write_from_experience(self.journal, self.experience, feedback,
            instruction="Write optional reusable observations; preserve uncertainty.", policy=self.policy,
            client=client or ScriptedWriter(), model_config=dict(model="synthetic-offline"),
            max_requests=kwargs.pop("max_requests", 4), condition_id="analysis-only-label", **kwargs)

    def test_same_experience_feedback_is_explicit_and_sessions_are_isolated(self):
        clients, reports = [], []
        for feedback in (WriterFeedback("none"), WriterFeedback("result_only"),
                         WriterFeedback("critique", "CRITIQUE_FIXTURE", "synthetic_test_fixture")):
            client = ScriptedWriter()
            reports.append(self.write(feedback, client))
            clients.append(client)
        self.assertTrue(all(r["status"] == "completed" and r["memory_stores"] == 1 for r in reports))
        starts = [self.journal.scan["starts"][r["run_id"]] for r in reports]
        self.assertEqual(len({s["session_id"] for s in starts}), 3)
        self.assertEqual(len({r["bank_id"] for r in reports}), 3)
        first_contexts = [client.requests[0]["messages"][1] for client in clients]
        self.assertEqual(first_contexts, [first_contexts[0]] * 3)
        for index, client in enumerate(clients):
            text = canonical(client.requests).decode()
            for secret in ("PRIVATE_REASONING", "PRIVATE_SIMULATOR", "HIDDEN_GRADING_ANSWER", "analysis-only-label"):
                self.assertNotIn(secret, text)
            feedback = json.loads(client.requests[0]["messages"][2]["content"])
            self.assertEqual("success" in feedback, index > 0)
            self.assertEqual("critique" in feedback, index == 2)
            self.assertNotIn("critique_origin", feedback)
            self.assertEqual([t["function"]["name"] for t in client.requests[0]["tools"]], ["memory_store"])
        audit = audit_memory_writes(self.folder.name)
        self.assertEqual(len(audit["writes"]), 3)
        self.assertGreater(audit["request_audit"]["memory_input_spans"], 0)

    def test_modified_experience_is_refused_before_bank_or_model_side_effects(self):
        value = self.journal.blobs.get(self.experience)
        value["observations"][0]["message"]["content"] = "INJECTED_FUTURE"
        self.experience = self.journal.blobs.put(value)
        before, client = self.journal.path.read_bytes(), ScriptedWriter()
        with self.assertRaisesRegex(ContractError, "differs from its source"):
            self.write(WriterFeedback("none"), client)
        self.assertEqual(self.journal.path.read_bytes(), before)
        self.assertEqual(client.requests, [])

    def test_empty_memory_and_request_limit_are_recorded_without_fallback(self):
        empty = self.write(WriterFeedback("none"), ScriptedWriter(empty=True))
        self.assertEqual(empty["status"], "empty")
        self.assertEqual(self.journal.state["snapshots"][empty["snapshot_id"]]["heads"], {})
        partial = self.write(WriterFeedback("none"), max_requests=1)
        self.assertEqual(partial["status"], "error")
        self.assertEqual(partial["error_code"], "RequestLimit")
        self.assertEqual(partial["memory_stores"], 1)
        self.assertEqual(len(audit_memory_writes(self.folder.name)["writes"]), 2)

    def test_business_calls_are_denied_in_the_writer(self):
        report = self.write(WriterFeedback("none"), ScriptedWriter(business=True))
        self.assertEqual(report["status"], "empty")
        events = [e for e in self.journal.events.values() if e["run_id"] == report["run_id"]]
        self.assertTrue(any(e["type"] == "policy.denied" for e in events))
        self.assertFalse(any(e["type"] == "business.transition" and "action" in e["payload"] for e in events))
        audit_memory_writes(self.folder.name)

    def test_invalid_feedback_and_unclosed_sources_are_refused(self):
        for arguments in (("unknown",), ("none", "hidden critique"), ("critique", "text", None)):
            with self.subTest(arguments=arguments), self.assertRaises(ContractError):
                WriterFeedback(*arguments)
        scan = deepcopy(self.journal.scan)
        scan["events"] = {key:e for key,e in scan["events"].items() if e["type"] != "run.ended"}
        self.journal.scan = scan
        with self.assertRaisesRegex(ContractError, "closed"):
            freeze_public_experience(self.journal, "source")

    def test_writer_reasoning_is_preserved_raw_but_not_reintroduced_as_history(self):
        class ReasoningWriter(ScriptedWriter):
            def respond(self, request):
                result = super().respond(request)
                message = result["choices"][0]["message"]
                message["reasoning"] = "WRITER_PRIVATE_CHAIN"
                message["reasoning_details"] = [dict(text="WRITER_PRIVATE_DETAIL")]
                for call in message.get("tool_calls") or []:
                    call["provider_metadata"] = "CALL_PRIVATE_METADATA"
                return result
        client = ReasoningWriter()
        report = self.write(WriterFeedback("none"), client)
        self.assertEqual(report["status"], "completed")
        for wire in client.requests:
            for secret in ("WRITER_PRIVATE_CHAIN", "WRITER_PRIVATE_DETAIL", "CALL_PRIVATE_METADATA"):
                self.assertNotIn(secret, canonical(wire).decode())
        responses = [e for e in self.journal.events.values() if e["run_id"] == report["run_id"]
                     and e["type"] == "model.request_finished"]
        self.assertIn("WRITER_PRIVATE_CHAIN", canonical(self.journal.blobs.get(responses[0]["payload"]["response_ref"])).decode())
        audit_memory_writes(self.folder.name)

    def test_audit_refuses_extra_context_even_with_valid_provenance(self):
        original = SourceRegistry.prepare
        def extra(registry, ctx, actor, template, messages, **kwargs):
            source = registry.template(dict(message=dict(role="user", content="UNPERMITTED_GRADING_HINT")), roles=("writer",))
            return original(registry, ctx, actor, template, messages + [(source, "/message")], **kwargs)
        with patch.object(SourceRegistry, "prepare", extra):
            self.write(WriterFeedback("none"))
        with self.assertRaises(ContractError):
            audit_memory_writes(self.folder.name)

    def test_duplicate_writer_id_is_refused_before_creating_another_bank(self):
        self.write(WriterFeedback("none"), ScriptedWriter(empty=True), run_id="fixed-writer")
        before = self.journal.path.read_bytes()
        banks = deepcopy(self.journal.state["banks"])
        with self.assertRaises(ValueError):
            self.write(WriterFeedback("none"), ScriptedWriter(empty=True), run_id="fixed-writer")
        self.assertEqual(self.journal.path.read_bytes(), before)
        self.assertEqual(self.journal.state["banks"], banks)

    def test_audit_refuses_cost_that_differs_from_the_raw_response(self):
        class CostWriter(ScriptedWriter):
            def respond(self, request):
                return dict(super().respond(request), usage=dict(cost=0.01))
        original = self.journal.append
        def altered(events, **kwargs):
            events = deepcopy(events)
            for event in events:
                if event["type"] == "model.request_finished":
                    event["payload"]["cost_usd"] = 9.0
            return original(events, **kwargs)
        with patch.object(self.journal, "append", altered):
            self.write(WriterFeedback("none"), CostWriter())
        with self.assertRaises(ContractError):
            audit_memory_writes(self.folder.name)

    def test_null_tool_calls_and_explicit_input_rejection_are_separate_outcomes(self):
        class NullCalls(ScriptedWriter):
            def respond(self, request):
                return dict(choices=[dict(message=dict(role="assistant", content="No memory", tool_calls=None))])
        empty = self.write(WriterFeedback("none"), NullCalls())
        self.assertEqual(empty["status"], "empty")
        client = ScriptedWriter()
        blocked = self.write(WriterFeedback("none"), client, max_requests=None,
            execution_limits=ExecutionLimits(0, 3, 0, 1))
        self.assertEqual((blocked["status"], blocked["error_code"]), ("error", "INPUT_SIZE_LIMIT"))
        self.assertEqual(client.requests, [])
        self.assertEqual(len(audit_memory_writes(self.folder.name)["writes"]), 2)

    def test_malformed_tool_shape_is_a_closed_error_not_a_partial_journal(self):
        class Malformed(ScriptedWriter):
            def respond(self, request):
                return dict(choices=[dict(message=dict(role="assistant", content=None, tool_calls={"invalid": True}))])
        result = self.write(WriterFeedback("none"), Malformed())
        self.assertEqual((result["status"], result["memory_stores"]), ("error", 0))
        self.assertEqual(audit_memory_writes(self.folder.name)["writes"][0]["termination_reason"], "memory_write_error")

    def test_truncated_empty_and_partially_written_runs_are_errors(self):
        for empty in (True, False):
            class TruncatedWriter(ScriptedWriter):
                def respond(self, request):
                    response = super().respond(request)
                    choice = response["choices"][0]
                    choice["finish_reason"] = "tool_calls" if choice["message"].get("tool_calls") else "length"
                    return response
            result = self.write(WriterFeedback("none"), TruncatedWriter(empty=empty))
            self.assertEqual((result["status"], result["error_code"]), ("error", "WRITER_OUTPUT_TRUNCATED"))
            self.assertEqual(result["memory_stores"], 0 if empty else 1)
            self.assertTrue(self.journal.state["banks"][result["bank_id"]]["sealed"])
        self.assertTrue(all(w["termination_reason"] == "memory_write_error"
                            for w in audit_memory_writes(self.folder.name)["writes"]))

    def test_truncated_tool_call_is_preserved_raw_and_never_dispatched(self):
        class TruncatedTool(ScriptedWriter):
            def respond(self, request):
                response = super().respond(request)
                response["choices"][0]["finish_reason"] = "length"
                return response
        result = self.write(WriterFeedback("none"), TruncatedTool())
        self.assertEqual((result["status"], result["memory_stores"]), ("error", 0))
        finish = next(e for e in self.journal.events.values() if e["run_id"] == result["run_id"]
                      and e["type"] == "model.request_finished")
        self.assertEqual(self.journal.blobs.get(finish["payload"]["response_ref"])["choices"][0]["finish_reason"], "length")
        audit_memory_writes(self.folder.name)

    def test_finish_contract_refuses_filtered_missing_and_invalid_provider_metadata(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] / "tests/fixtures/writer-finish-v1.json").read_text())
        self.assertEqual(WRITER_COMPLETION_CONTRACT, fixture["contract"])
        for case in fixture["cases"]:
            message = dict(role="assistant", content="")
            if case["calls"]:
                message["tool_calls"] = [dict(id="synthetic-call")]
            raw = dict(choices=[dict(finish_reason=case["finish_reason"], message=message)])
            self.assertEqual(writer_reply_error(raw, require_finish_reason=case["observed"]), case["error"])

    def test_audit_rejects_incomplete_reply_without_its_error_event(self):
        class Filtered(ScriptedWriter):
            def respond(self, request):
                return dict(choices=[dict(finish_reason="content_filter", message=dict(role="assistant", content=""))])
        original = self.journal.append
        def omit_error(events, **kwargs):
            return original([e for e in events if e["type"] != "runtime.error"], **kwargs)
        with patch.object(self.journal, "append", omit_error):
            self.write(WriterFeedback("none"), Filtered())
        with self.assertRaisesRegex(ContractError, "explicit termination error"):
            audit_memory_writes(self.folder.name)

    def test_missing_model_cannot_match_an_absent_canonical_alias(self):
        identity = dict(id="worker", canonical_slug=None, provider_name="Provider")
        raw = dict(provider="Provider", choices=[dict(finish_reason="stop", message=dict(role="assistant", content="Done"))])
        self.assertEqual(writer_response_error(raw, model_identity=identity, observed=True), "WRITER_MODEL_MISMATCH")
        raw["model"] = "worker"
        self.assertIsNone(writer_response_error(raw, model_identity=identity, observed=True))


if __name__ == "__main__":
    unittest.main()
