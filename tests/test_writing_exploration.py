"""Mock HTTP only: clean-freeze gates, bounded writing and generated-memory size checks."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.writing_exploration import audit_writing
from agent_memory_eval.analysis.writing_export import collect, export
from agent_memory_eval.contracts.core import ContractError, RunContext, canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.contracts.writing import writing_profile
from agent_memory_eval.experiments.writing import preflight_writing, run_writing
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry
from test_openrouter import MODEL, ROUTE, Reply, response


IDENTITY = dict(implementation_commit="1" * 40, worktree_has_uncommitted_implementation=False)


class WritingExplorationFixture:
    def setUp(self):
        self.folder = TemporaryDirectory(dir=ROOT / "outputs", prefix="writing-test-")
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)
        self.source, self.output = self.base / "source", self.base / "result"
        with Journal(self.source, "mock-source") as journal:
            ctx = RunContext("mock-source", "source", "source-session", "no-memory", "collection", "solve", "observed")
            policy = dict(role="system", content="PUBLIC_POLICY: Record requested bags, not the free allowance.")
            journal.append([ctx.at("prepare").event("run.started", dict(bank_id=None, start_snapshot_id=None,
                fresh_session=True, source_run_ids=[], task_ref=journal.blobs.put(policy)))])
            registry = SourceRegistry(journal, ctx)
            tool = dict(type="function", function=dict(name="book_reservation", description="d" * 10000,
                parameters=dict(type="object", properties={})))
            template = registry.template(dict(model=MODEL["id"], system=policy, tools=[tool],
                provider=ROUTE, stream=False, reasoning=dict(effort="low"), max_completion_tokens=100))
            user = registry.template(dict(message=dict(role="user", content="No bags please.")))
            history = [(template, "/system"), (user, "/message")]
            class Source:
                offline = False
                def respond(self, wire):
                    raw = response()
                    message = dict(role="assistant", content="Booked.")
                    if len(wire["messages"]) == 2:
                        message = dict(role="assistant", content=None, tool_calls=[dict(id="book", type="function",
                            function=dict(name="book_reservation", arguments='{"total_baggages":6}'))])
                    raw["choices"][0].update(message=message, finish_reason="tool_calls" if message.get("tool_calls") else "stop")
                    return raw
            gateway = RecordingGateway(journal)
            _, authored, request_id = gateway.call(ctx, registry, "worker", template, history, Source(), max_requests=2)
            self.decision = request_id
            history.append((authored, ""))
            event = ctx.event("business.transition", dict(public_message_ref=journal.blobs.put(
                dict(role="tool", tool_call_id="book", content='{"booked":true}'))))
            journal.append([event])
            history.append((registry.add("business_result", event["event_id"]), ""))
            gateway.call(ctx, registry, "worker", template, history, Source(), max_requests=2)
            journal.append([ctx.at("grade").event("outcome.scored", dict(grading_status="graded", official_success=False,
                official_score=0.0, official_artifact_ref=journal.blobs.put(dict(secret="HIDDEN_GRADING_ANSWER"))))])
            journal.append([ctx.at("seal").event("run.ended", dict(termination_reason="mock_fixture",
                grading_status="graded", end_snapshot_id=None))])
        self.profile = json.loads((ROOT / "protocols/drafts/tau2-memory-writing-exploration-v1.json").read_text())
        self.profile.update(status="ready", repetitions=1, schedule_seed=123, page_size=1, timeout_seconds=10,
            bank_limits=dict(max_items=2, max_payload_bytes=30000, max_return_bytes=40000),
            execution_limits=dict(worker_requests=0, writer_requests=3, user_simulator_requests=0, max_input_bytes=64000),
            guards=dict(max_model_requests=4, max_reported_cost_usd=1, max_seconds=60))
        self.profile["source"] = dict(path=str(self.source.relative_to(ROOT)), run_id="source",
            log_sha256=digest((self.source / "log.jsonl").read_bytes()), decision_request_id=self.decision)
        self.wires = []
        self.catalog = dict(data=[deepcopy(MODEL)])
        self.endpoints = dict(data=dict(id=MODEL["id"], endpoints=[dict(tag="openai", status=0, provider_name="OpenAI",
            supported_parameters=MODEL["supported_parameters"], pricing=MODEL["pricing"])]))

    def run_study(self, *, empty=False, empty_level=None, truncate=False, unknown=False, wrong_provider=False,
                  body="Synthetic memory."):
        def opener(request, timeout):
            wire = json.loads(request.data)
            self.wires.append(wire)
            raw = response(None if unknown else 0.0001)
            if wrong_provider:
                raw["provider"] = "Unexpected provider"
            wrote = any(c["function"]["name"] == "memory_store" for m in wire["messages"] for c in m.get("tool_calls") or [])
            message = dict(role="assistant", content="Done.")
            level = json.loads(wire["messages"][2]["content"])["level"]
            if not empty and level != empty_level and not wrote:
                message = dict(role="assistant", content=None, tool_calls=[dict(id="store", type="function",
                    function=dict(name="memory_store", arguments=json.dumps(dict(title="Mock lesson", body=body))))])
            raw["choices"][0].update(message=message,
                finish_reason="length" if truncate else "tool_calls" if message.get("tool_calls") else "stop")
            return Reply(raw)
        with patch.dict("os.environ", OPENROUTER_API_KEY="test-only"), \
                patch("agent_memory_eval.experiments.writing.implementation_identity", return_value=IDENTITY), \
                patch("agent_memory_eval.experiments.writing.model_catalog", return_value=self.catalog), \
                patch("agent_memory_eval.experiments.writing.model_endpoints", return_value=self.endpoints):
            return run_writing(self.profile, self.output, ROOT, opener=opener)


class WritingExplorationTests(WritingExplorationFixture, unittest.TestCase):
    def test_writing_only_integration_and_source_independent_audit(self):
        result = self.run_study()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.wires), 4)  # Exact exhaustion is completion, not a stop.
        self.source.rename(self.base / "source-unavailable")
        report = audit_writing(self.output)
        self.assertEqual((report["completed_writes"], report["skipped"], report["errors"]), (2, 0, 0))
        self.assertEqual(report["request_audit"]["requests_by_role"], dict(writer=4))
        self.assertEqual(report["replay_eligible_snapshots"], 2)
        for wire in self.wires:
            self.assertNotIn("HIDDEN_GRADING_ANSWER", canonical(wire).decode())
            self.assertEqual([t["function"]["name"] for t in wire["tools"]], ["memory_store"])

    def test_draft_and_dirty_checkout_stop_before_catalog_or_model_calls(self):
        draft = json.loads((ROOT / "protocols/drafts/tau2-memory-writing-exploration-v1.json").read_text())
        with patch("agent_memory_eval.experiments.writing.model_catalog", side_effect=AssertionError("HTTP attempted")):
            with self.assertRaises(ContractError):
                run_writing(draft, self.output, ROOT)
            with TemporaryDirectory(dir=ROOT, prefix=".dirty-writing-test-") as folder:
                (Path(folder) / "uncommitted.txt").write_text("fixture")
                with self.assertRaisesRegex(ContractError, "Commit all"):
                    run_writing(self.profile, self.output, ROOT)
        self.assertFalse(self.output.exists())

    def test_oversized_initial_input_is_blocked_before_catalog(self):
        self.profile["execution_limits"]["max_input_bytes"] = 1
        with patch("agent_memory_eval.experiments.writing.implementation_identity", return_value=IDENTITY), \
                patch("agent_memory_eval.experiments.writing.model_catalog", side_effect=AssertionError("HTTP attempted")):
            with self.assertRaisesRegex(ContractError, "fitting initial requests"):
                run_writing(self.profile, self.output, ROOT)
        self.assertFalse(self.output.exists())

    def test_unknown_cost_preserves_first_error_and_skips_the_remaining_allocation(self):
        result = self.run_study(unknown=True)
        self.assertEqual([r["status"] for r in result["results"]], ["error", "skipped"])
        self.assertEqual(len(self.wires), 1)
        report = audit_writing(self.output)
        self.assertEqual(report["request_audit"]["unknown_cost_requests"], 1)
        self.assertEqual(report["replay_eligible_snapshots"], 0)

    def test_call_budget_blocks_followup_http_and_keeps_partial_snapshot(self):
        self.profile["guards"]["max_model_requests"] = 1
        result = self.run_study()
        self.assertEqual(len(self.wires), 1)
        self.assertEqual(result["results"][0]["memory_stores"], 1)
        self.assertEqual(result["results"][0]["error_code"], "CALL_LIMIT")
        self.assertEqual(audit_writing(self.output)["skipped"], 1)

    def test_wrong_provider_stops_the_batch_with_saved_response(self):
        self.run_study(wrong_provider=True)
        self.assertEqual(len(self.wires), 1)
        report = audit_writing(self.output)
        self.assertEqual((report["errors"], report["skipped"]), (1, 1))

    def test_provider_mismatch_still_stops_when_reply_is_also_truncated(self):
        result = self.run_study(wrong_provider=True, truncate=True)
        self.assertEqual(len(self.wires), 1)
        self.assertEqual(result["results"][0]["error_code"], "WRITER_MODEL_MISMATCH")
        report = audit_writing(self.output)
        self.assertEqual((report["errors"], report["skipped"]), (1, 1))

    def test_changed_asset_fails_before_catalog(self):
        path = self.base / "changed-instruction.txt"
        path.write_text("Unpinned replacement")
        self.profile["instructions"]["writer"]["path"] = str(path.relative_to(ROOT))
        with patch("agent_memory_eval.experiments.writing.implementation_identity", return_value=IDENTITY), \
                patch("agent_memory_eval.experiments.writing.model_catalog", side_effect=AssertionError("HTTP attempted")):
            with self.assertRaisesRegex(ContractError, "Pinned asset changed"):
                run_writing(self.profile, self.output, ROOT)
        self.assertFalse(self.output.exists())

    def test_implementation_change_during_catalog_prevents_any_paid_request(self):
        with patch.dict("os.environ", OPENROUTER_API_KEY="test-only"), \
                patch("agent_memory_eval.experiments.writing.implementation_identity",
                    side_effect=[IDENTITY, dict(IDENTITY, implementation_commit="2" * 40)]), \
                patch("agent_memory_eval.experiments.writing.model_catalog", return_value=self.catalog), \
                patch("agent_memory_eval.experiments.writing.model_endpoints", return_value=self.endpoints):
            with self.assertRaisesRegex(ContractError, "Implementation changed"):
                run_writing(self.profile, self.output, ROOT, opener=lambda *a, **kw: self.fail("Paid call"))
        self.assertFalse(self.output.exists())

    def test_truncation_is_retained_without_dispatch_or_favorable_replacement(self):
        self.run_study(truncate=True)
        report = audit_writing(self.output)
        self.assertEqual((report["errors"], report["completed_writes"], report["empty_writes"]), (2, 0, 0))
        self.assertEqual(report["replay_eligible_snapshots"], 0)
        self.assertEqual(len(self.wires), 2)

    def test_completed_empty_writes_are_retained(self):
        self.run_study(empty=True)
        report = audit_writing(self.output)
        self.assertEqual((report["empty_writes"], report["errors"]), (2, 0))
        self.assertEqual(report["empty_without_store_calls"], 2)

    def test_budget_denial_is_not_reported_as_an_empty_write_without_attempts(self):
        self.profile["bank_limits"]["max_items"] = 0
        self.run_study()
        report = audit_writing(self.output)
        self.assertEqual((report["empty_writes"], report["empty_after_store_calls"]), (2, 2))
        self.assertEqual(report["empty_without_store_calls"], 0)

    def test_snapshot_return_budget_failure_is_distinct_from_a_writer_error(self):
        self.profile["bank_limits"]["max_return_bytes"] = 0
        result = self.run_study(empty=True)
        self.assertTrue(all(r["snapshot_preflight_error"] == "SNAPSHOT_BUDGET_EXCEEDED" for r in result["results"]))
        report = audit_writing(self.output)
        self.assertEqual((report["empty_writes"], report["errors"], report["replay_eligible_snapshots"]), (2, 0, 0))

    def test_generated_memory_is_size_checked_without_silent_truncation(self):
        self.profile["execution_limits"]["max_input_bytes"] = 15000
        self.run_study(body="x" * 5000)
        report = audit_writing(self.output)
        self.assertEqual(report["completed_writes"], 2)
        self.assertEqual(report["replay_eligible_snapshots"], 0)

    def test_audit_rejects_forged_replay_eligibility_for_oversized_memory(self):
        self.profile["execution_limits"]["max_input_bytes"] = 15000
        original = Journal.append
        def altered(journal, events, **kwargs):
            state = kwargs.get("state")
            if state and state.get("writing_exploration", {}).get("results"):
                state = deepcopy(state)
                for result in state["writing_exploration"]["results"]:
                    if result["status"] != "skipped":
                        result["replay_eligible"] = True
                kwargs["state"] = state
            return original(journal, events, **kwargs)
        with patch.object(Journal, "append", altered):
            self.run_study(body="x" * 5000)
        with self.assertRaisesRegex(ContractError, "eligibility hides"):
            audit_writing(self.output)

    def test_preflight_is_local_and_unresolved_limits_are_not_defaulted(self):
        self.profile.update(status="draft", repetitions=None)
        self.profile["execution_limits"]["max_input_bytes"] = None
        with patch("agent_memory_eval.experiments.writing.model_catalog", side_effect=AssertionError("HTTP attempted")):
            result = preflight_writing(self.profile, ROOT)
        self.assertFalse(result["local_ready"])
        self.assertFalse(result["live_ready"])
        self.assertIn("execution_limits.max_input_bytes", result["unresolved_fields"])
        self.assertTrue(all(r["fits_input_limit"] is None for r in result["initial_requests"]))
        invalid = deepcopy(self.profile)
        invalid["guards"]["max_seconds"] = float("nan")
        with self.assertRaises(ContractError):
            writing_profile(invalid)

    def test_export_preserves_empty_and_error_allocations_and_hides_condition_labels(self):
        self.run_study(unknown=True)
        folder = self.base / "export"
        export(self.output, folder)
        metrics = json.loads((folder / "metrics.json").read_text())
        blind = json.loads((folder / "blind.json").read_text())
        mapping = json.loads((folder / "mapping.json").read_text())
        self.assertEqual(len(metrics["runs"]), 2)
        self.assertEqual({r["status"] for r in metrics["runs"]}, {"error", "skipped"})
        self.assertEqual(sum(g["unknown_cost_requests"] for g in metrics["by_feedback"].values()), 1)
        self.assertEqual(len(blind["cases"]), 2)
        self.assertEqual(set(mapping["cases"]), {c["case_id"] for c in blind["cases"]})
        self.assertTrue(all(set(c) == {"case_id", "items"} for c in blind["cases"]))
        for c in blind["cases"]:
            self.assertTrue(all(set(item) == {"title", "body"} for item in c["items"]))
        self.assertNotIn("HIDDEN_GRADING_ANSWER", (folder / "public_context.json").read_text())

    def test_export_refuses_writing_inside_preserved_run(self):
        with self.assertRaises(ValueError):
            export(self.source, self.source / "derived-export")
        self.assertFalse((self.source / "derived-export").exists())

    def test_in_memory_review_never_creates_or_deletes_a_file(self):
        self.run_study(empty=True)
        with patch.object(Path, "mkdir", side_effect=AssertionError("mkdir attempted")), \
                patch("os.unlink", side_effect=AssertionError("unlink attempted")), \
                patch("os.rmdir", side_effect=AssertionError("rmdir attempted")):
            views = collect(self.output)
        self.assertEqual(len(views["blind"]["cases"]), 2)
        self.assertEqual(views["metrics"]["audit"]["empty_writes"], 2)


if __name__ == "__main__":
    unittest.main()
