"""Recorded public inputs reach the real writer/replay builders without paid calls."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from agent_memory_eval.analysis.memory_writing import audit_memory_writes
from agent_memory_eval.analysis.preparation import audit_preparations
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, RunContext, canonical, digest
from agent_memory_eval.memory.preparation import prepare_snapshot_replay, prepare_writer_request
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.memory.writing import WriterFeedback, read_public_experience, write_from_experience
from agent_memory_eval.recording.experience import (
    capture_recorded_context, import_recorded_experience, read_recorded_context,
)
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry


INSTRUCTION = "Optionally record reusable observations, preserving uncertainty."
MODEL = dict(model="synthetic-offline", reasoning=dict(effort="low"))


class MockWriter:
    offline = True

    def __init__(self, *, empty=False):
        self.requests, self.empty = [], empty

    def respond(self, request):
        self.requests.append(deepcopy(request))
        message = dict(role="assistant", content="Done.")
        if len(self.requests) == 1 and not self.empty:
            message = dict(role="assistant", content=None, tool_calls=[dict(
                id="write", type="function", function=dict(name="memory_store", arguments=json.dumps(
                    dict(title="Synthetic lesson", body="Keep requested bag count distinct from allowance."))))])
        return dict(choices=[dict(message=message)])


class RequestPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "schemas/recorded-preparation.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        cls.schema = Draft202012Validator(schema)

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.source = Path(self.folder.name) / "source"
        with Journal(self.source, "source-fixture") as journal:
            ctx = RunContext("source-fixture", "source-run", "source-session", "no-memory", "collection", "solve")
            policy = dict(role="system", content="PUBLIC_POLICY: Ask what baggage is needed.")
            journal.append([ctx.at("prepare").event("run.started", dict(bank_id=None,
                start_snapshot_id=None, fresh_session=True, source_run_ids=[], task_ref=journal.blobs.put(policy)))])
            registry = SourceRegistry(journal, ctx)
            template = registry.template(dict(MODEL, system=policy, initial=dict(role="user", content="No bags.")))
            history = [(template, "/system"), (template, "/initial")]

            class SourceClient:
                offline = True

                def respond(self, request):
                    message = dict(role="assistant", content="Booked.", reasoning="PRIVATE_REASONING")
                    if len(request["messages"]) == 2:
                        message.update(content=None, tool_calls=[dict(id="booking", type="function",
                            function=dict(name="book_reservation", arguments='{"total_baggages":6}'))])
                    return dict(choices=[dict(message=message)])

            gateway = RecordingGateway(journal)
            _, source, self.decision_id = gateway.call(ctx, registry, "worker", template, history, SourceClient(), max_requests=2)
            history.append((source, ""))
            result = ctx.event("business.transition", dict(public_message_ref=journal.blobs.put(
                dict(role="tool", tool_call_id="booking", content='{"booked":true}'))))
            journal.append([result])
            history.append((registry.add("business_result", result["event_id"]), ""))
            gateway.call(ctx, registry, "worker", template, history, SourceClient(), max_requests=2)
            journal.append([ctx.event("business.transition", dict(actor_role="user_simulator",
                model_message_ref=journal.blobs.put(dict(role="system", content="PRIVATE_SIMULATOR"))))])
            journal.append([ctx.at("grade").event("outcome.scored", dict(grading_status="graded",
                official_success=False, official_score=0.0,
                official_artifact_ref=journal.blobs.put(dict(answer="HIDDEN_GRADING_ANSWER"))))])
            journal.append([ctx.at("seal").event("run.ended", dict(termination_reason="fixture",
                grading_status="graded", end_snapshot_id=None))])
        self.source_log = (self.source / "log.jsonl").read_bytes()
        self.source_sha = digest(self.source_log)
        self.target = Path(self.folder.name) / "prepared"
        self.journal = Journal(self.target, "preparation-fixture", contract_version="1.2")
        self.addCleanup(self.journal.close)
        self.experience_ref = import_recorded_experience(self.journal, self.source,
            source_run_id="source-run", log_sha256=self.source_sha)
        self.decision_ref = capture_recorded_context(self.journal, self.source,
            source_run_id="source-run", log_sha256=self.source_sha, request_id=self.decision_id)
        for ref in (self.decision_ref, self.experience_ref):
            self.schema.validate(self.journal.blobs.get(ref))
        self.write_policy = MemoryPolicy(True, "post_task", False, 2, read_mode="none")
        self.replay_policy = MemoryPolicy(True, "none", False, 2, read_mode="preload")

    def prepare_writer(self, feedback, **kwargs):
        return prepare_writer_request(self.journal, self.experience_ref, feedback,
            instruction=INSTRUCTION, model_config=MODEL, condition_id=feedback.level, **kwargs)

    def write(self, *, empty=False):
        client = MockWriter(empty=empty)
        result = write_from_experience(self.journal, self.experience_ref, WriterFeedback("none"),
            instruction=INSTRUCTION, model_config=MODEL, condition_id="mock-only", client=client,
            policy=self.write_policy, max_requests=3)
        return result, client

    def replay(self, snapshot, **kwargs):
        return prepare_snapshot_replay(self.journal, self.decision_ref, snapshot,
            policy=kwargs.pop("policy", self.replay_policy), instruction="Past notes are fallible.",
            condition_id="replay-preparation", **kwargs)

    def test_recorded_projection_and_feedback_do_not_expose_private_material(self):
        prepared = [self.prepare_writer(WriterFeedback(level)) for level in ("none", "result_only")]
        for value in prepared:
            self.schema.validate(value)
        requests = [self.journal.blobs.get(p["request_ref"]) for p in prepared]
        self.assertEqual(requests[0]["messages"][1], requests[1]["messages"][1])
        for request in requests:
            text = canonical(request).decode()
            for secret in ("PRIVATE_REASONING", "PRIVATE_SIMULATOR", "HIDDEN_GRADING_ANSWER", "source-run"):
                self.assertNotIn(secret, text)
            self.assertIn("PUBLIC_POLICY", text)
            self.assertIn('total_baggages', text)
        self.assertNotIn("success", json.loads(requests[0]["messages"][2]["content"]))
        self.assertIs(json.loads(requests[1]["messages"][2]["content"])["success"], False)
        self.assertEqual(audit_preparations(self.target)["all_model_requests"], 0)
        self.assertEqual((self.source / "log.jsonl").read_bytes(), self.source_log)

    def test_imported_experience_survives_source_removal_and_matches_live_builder(self):
        self.source.rename(self.source.with_name("source-unavailable"))
        prepared = self.prepare_writer(WriterFeedback("none"))
        result, client = self.write()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(client.requests[0], self.journal.blobs.get(prepared["request_ref"]))
        self.assertEqual(len(audit_memory_writes(self.target)["writes"]), 1)
        self.replay(result["snapshot_id"])
        audit = audit_preparations(self.target)
        replay = next(p for p in audit["preparations"] if p["kind"] == "snapshot_replay")
        self.assertEqual(replay["planned_memory_spans"], 2)
        self.assertEqual(audit["preparation_memory_exposures"], 0)
        self.assertEqual(audit["all_model_requests"], len(client.requests))
        bank = self.journal.state["banks"][result["bank_id"]]
        self.assertEqual(bank["heads"], self.journal.state["snapshots"][result["snapshot_id"]]["heads"])

    def test_empty_written_memory_is_kept_as_an_empty_snapshot(self):
        result, _ = self.write(empty=True)
        self.assertEqual(result["status"], "empty")
        prepared = self.replay(result["snapshot_id"], max_input_bytes=1)
        self.assertFalse(prepared["fits_input_limit"])
        self.assertEqual(prepared["planned_memory_spans"], 0)
        audit_preparations(self.target)

    def test_capsule_refuses_changed_producer_refs_and_wrong_source_hash(self):
        archive = self.journal.blobs.get(self.decision_ref)
        archive["request_ref"] = self.journal.blobs.put(dict(messages=[]))
        with self.assertRaises(ContractError):
            read_recorded_context(self.journal.blobs, self.journal.blobs.put(archive))
        with self.assertRaisesRegex(ContractError, "immutable"):
            capture_recorded_context(self.journal, self.source, source_run_id="source-run", log_sha256="0" * 64)
        with Journal(self.source, "source-fixture"):
            with self.assertRaisesRegex(ContractError, "active writer"):
                capture_recorded_context(self.journal, self.source, source_run_id="source-run", log_sha256=self.source_sha)

    def test_decision_context_cannot_be_substituted_for_final_writing_experience(self):
        value = self.journal.blobs.get(self.experience_ref)
        value["archive_ref"] = self.decision_ref
        with self.assertRaises(ContractError):
            read_public_experience(self.journal.blobs, self.journal.scan, self.journal.blobs.put(value))

    def test_invalid_limits_are_refused_before_run_or_bank_creation(self):
        result, _ = self.write()
        before = self.journal.path.read_bytes()
        for policy in (MemoryPolicy(True, "none", False, 2, read_mode="preload", max_items=0),
                       MemoryPolicy(True, "none", False, 2, read_mode="preload", max_return_bytes=1)):
            with self.assertRaises(ContractError):
                self.replay(result["snapshot_id"], policy=policy)
        with self.assertRaises(ContractError):
            self.prepare_writer(WriterFeedback("none"), max_input_bytes=0)
        self.assertEqual(self.journal.path.read_bytes(), before)

    def test_audit_rejects_extra_prepared_context_even_with_valid_provenance(self):
        original = SourceRegistry.prepare

        def extra(registry, ctx, actor, template, messages, **kwargs):
            source = registry.template(dict(message=dict(role="user", content="UNPERMITTED_HINT")), roles=(actor,))
            return original(registry, ctx, actor, template, messages + [(source, "/message")], **kwargs)

        with patch.object(SourceRegistry, "prepare", extra):
            self.prepare_writer(WriterFeedback("none"))
        with self.assertRaises(ContractError):
            audit_preparations(self.target)


if __name__ == "__main__":
    unittest.main()
