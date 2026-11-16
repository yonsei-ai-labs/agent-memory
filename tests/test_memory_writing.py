"""Feedback isolation and real recording/service routing with synthetic transports."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_memory_eval.analysis.memory_writing import audit_memory_writes
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, RunContext, canonical
from agent_memory_eval.memory.writing import WriterFeedback, freeze_public_experience, write_from_experience
from agent_memory_eval.recording.journal import Journal


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


if __name__ == "__main__":
    unittest.main()
