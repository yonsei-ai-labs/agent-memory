"""Production recorder and mock HTTP; no real model, catalog or business execution."""

from copy import deepcopy
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from agent_memory_eval.analysis.preparation import audit_preparations
from agent_memory_eval.analysis.snapshot_replay import audit_snapshot_replays
from agent_memory_eval.analysis.written_comparison import case_packet, review_recorded_replies
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, RunContext, canonical, digest, read_json
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.contracts.snapshot_replay import replay_response_error
from agent_memory_eval.experiments.writing_comparison import comparison_requests
from agent_memory_eval.memory.preparation import prepare_snapshot_replay, snapshot_items
from agent_memory_eval.memory.writing import WriterFeedback, write_from_experience
from agent_memory_eval.models.openrouter import OpenRouterClient, SpendGuard
from agent_memory_eval.recording.experience import capture_recorded_context, import_recorded_experience
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry
from agent_memory_eval.runner.snapshot_replay import run_snapshot_replay
from test_openrouter import MODEL, ROUTE, Reply, response


INSTRUCTION = "Use past notes only when applicable to this request."
LIMITS = ExecutionLimits(1, 0, 0, 10000)


def tool(name, args, call_id="call"):
    return dict(id=call_id, type="function", function=dict(name=name, arguments=canonical(args).decode()))


def body(message=None):
    value = response()
    value["choices"][0]["message"] = message or dict(role="assistant", content="How many bags are needed?", reasoning="PRIVATE_NEW_REASONING")
    value["choices"][0]["finish_reason"] = "tool_calls" if value["choices"][0]["message"].get("tool_calls") else "stop"
    return value


class ScriptedClient:
    offline = True

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def respond(self, request):
        self.requests.append(deepcopy(request))
        return deepcopy(self.replies.pop(0))


class ReplayFixture:
    """Small illustrative source and actual writer/preparation components."""

    def __init__(self, root, *, observed=False, empty=False):
        self.root, self.observed = Path(root), observed
        self.source, self.target = self.root / "source", self.root / "target"
        self.model = dict(MODEL, selected_endpoint=dict(provider_name="OpenAI"))
        self.model_config = (dict(model=MODEL["id"], provider=deepcopy(ROUTE), stream=False,
                                  max_completion_tokens=100, reasoning=dict(effort="low")) if observed else
                             dict(model="synthetic-offline", reasoning=dict(effort="low")))
        source_client = ScriptedClient([body(dict(role="assistant", content="Source ended.", reasoning="SOURCE_PRIVATE"))])
        source_client.offline = not observed
        with Journal(self.source, "replay-source") as journal:
            ctx = RunContext("replay-source", "source-run", "source-session", "source", "collection", "solve",
                             "observed" if observed else "illustrative")
            policy = dict(role="system", content="PUBLIC_POLICY: Confirm requested baggage before booking. At most one certificate per reservation.")
            journal.append([ctx.at("prepare").event("run.started", dict(bank_id=None, start_snapshot_id=None,
                fresh_session=True, source_run_ids=[], task_ref=journal.blobs.put(policy)))])
            registry = SourceRegistry(journal, ctx)
            template = registry.template(dict(self.model_config, system=policy,
                initial=dict(role="user", content="No bags, please."),
                tools=[dict(type="function", function=dict(name="book_reservation", parameters={}))], tool_choice="auto"))
            RecordingGateway(journal).call(ctx, registry, "worker", template,
                [(template, "/system"), (template, "/initial")], source_client, max_requests=1)
            journal.append([ctx.at("grade").event("outcome.scored", dict(grading_status="graded", official_success=False,
                official_score=0.0, official_artifact_ref=journal.blobs.put(dict(answer="HIDDEN_GRADING_ANSWER"))))])
            journal.append([ctx.at("seal").event("run.ended", dict(termination_reason="test_fixture",
                grading_status="graded", end_snapshot_id=None))])
        self.source_log = (self.source / "log.jsonl").read_bytes()
        self.journal = Journal(self.target, "replay-test", contract_version="1.2")
        self.experience = import_recorded_experience(self.journal, self.source,
            source_run_id="source-run", log_sha256=digest(self.source_log))
        self.decision = capture_recorded_context(self.journal, self.source,
            source_run_id="source-run", log_sha256=digest(self.source_log))
        self.policy = MemoryPolicy(True, "none", False, 2, read_mode="preload")
        writer = ScriptedClient(([body(dict(role="assistant", content=None, tool_calls=[tool("memory_store",
            dict(title="Quantity", body="Ask for needed baggage; allowance is not the customer's request."))]))] if not empty else []) +
            [body(dict(role="assistant", content="Finished writing."))])
        writer.offline = not observed
        if observed:
            writer.model = self.model
        self.written = write_from_experience(self.journal, self.experience, WriterFeedback("none"),
            instruction="Optionally record a reusable lesson.", model_config=self.model_config,
            policy=MemoryPolicy(True, "post_task", False, 2, read_mode="none"), client=writer,
            max_requests=3, condition_id="writer-fixture")
        assert self.written["status"] == ("empty" if empty else "completed"), self.written
        self.prepared = prepare_snapshot_replay(self.journal, self.decision, self.written["snapshot_id"],
            policy=self.policy, instruction=INSTRUCTION, condition_id="prepared-only", max_input_bytes=LIMITS.max_input_bytes)
        self.http_wires = []

    def client(self, reply=None, *, limits=LIMITS, guard=None, failure=False):
        if not self.observed:
            return ScriptedClient([reply or body()])
        def opener(request, timeout):
            self.http_wires.append(request.data)
            if failure:
                raise HTTPError(request.full_url, 503, "fixture", {}, io.BytesIO(b'{"error":{"message":"unavailable"}}'))
            return Reply(reply or body())
        return OpenRouterClient(self.model, guard or SpendGuard(max_calls=5, max_usd=1, max_seconds=60),
            route=ROUTE, opener=opener, max_input_bytes=limits.max_input_bytes)

    def replay(self, client=None, **kwargs):
        return run_snapshot_replay(self.journal, self.decision, self.written["snapshot_id"],
            policy=kwargs.pop("policy", self.policy), instruction=INSTRUCTION,
            condition_id="analysis-label-never-model-input", client=client or self.client(),
            execution_limits=kwargs.pop("execution_limits", LIMITS),
            expected_request_sha256=kwargs.pop("expected_request_sha256", self.prepared["request_ref"]["sha256"]), **kwargs)


class SnapshotReplayFixtureMixin:
    def fixture(self, **kwargs):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        fixture = ReplayFixture(folder.name, **kwargs)
        self.addCleanup(fixture.journal.close)
        return fixture


class SnapshotReplayTests(SnapshotReplayFixtureMixin, unittest.TestCase):
    def review_inputs(self, fixture):
        archive = fixture.journal.blobs.get(fixture.decision)
        wire = fixture.journal.blobs.get(archive["request_ref"])
        case = dict(case_id="synthetic-case", request_sha256=archive["request_ref"]["sha256"], wire=wire,
            original_public_reply=dict(role="assistant", content="Source ended."), verified_selection={})
        packet = case_packet([case], dict(control_notes={}))
        def evidence(index):
            message = packet["cases"][0]["messages"][index]
            return dict(message_index=index, message_sha256=digest(canonical(message)), excerpt=message["content"])
        rules = dict(format="agent-memory-written-comparison-rubrics/v1", status="provisional",
            packet_sha256=digest(canonical(packet)), scope="public_quantity_and_certificate_checks_not_full_policy_or_episode_success",
            reviewer=dict(id="test-fixture", kind="analysis_agent", independence_declared=False, historical_outcomes_visible=True),
            cases=[dict(case_id="synthetic-case", baggage=dict(interpretation="explicit_quantity", expected_quantity=0,
                evidence=evidence(1), note="Synthetic explicit no-bags request."),
                certificates=dict(max_per_reservation=1, evidence=evidence(0)),
                approval_review="Not judged", other_policy_review="Not judged")])
        return packet, rules

    def test_prepared_bytes_become_recorded_input_without_tool_execution_or_private_leakage(self):
        f = self.fixture()
        client = f.client(body(dict(role="assistant", content=None,
            tool_calls=[tool("book_reservation", dict(total_baggages=0))], reasoning="PRIVATE_NEW_REASONING")))
        result = f.replay(client)
        expected = f.journal.blobs.get(f.prepared["request_ref"])
        self.assertEqual(client.requests, [expected])
        self.assertEqual(result["status"], "response")
        self.assertIsNone(result["official_success"])
        for hidden in ("HIDDEN_GRADING_ANSWER", "SOURCE_PRIVATE", "analysis-label-never-model-input"):
            self.assertNotIn(hidden, canonical(client.requests).decode())
        self.assertNotIn("PRIVATE_NEW_REASONING", canonical(f.journal.blobs.get(result["message_ref"])).decode())
        audit = audit_snapshot_replays(f.target)
        self.assertEqual(audit["model_requests"], 1)
        self.assertEqual(audit["replays"][0]["memory_input_spans"], 2)
        self.assertEqual(audit["replays"][0]["received_memory_spans"], 2)
        self.assertFalse(audit["business_tools_executed"])
        self.assertEqual((f.source / "log.jsonl").read_bytes(), f.source_log)
        self.assertEqual(audit_preparations(f.target)["preparation_memory_exposures"], 0)

    def test_four_arm_renderer_model_and_empty_rows_match_runtime_wire(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                f = self.fixture(empty=empty)
                archive = f.journal.blobs.get(f.decision)
                original = f.journal.blobs.get(archive["request_ref"])
                bank = dict(f.written, items=snapshot_items(f.journal.blobs, f.journal.state, f.written["snapshot_id"]), repetition=1)
                note = dict(title="Control", body="Check the policy.")
                rows, requests = comparison_requests([dict(case_id="case", wire=original)], [bank],
                    f.policy, INSTRUCTION, dict(generic=note, researcher=note))
                key = next(r["request_sha256"] for r in rows if r["arm"] == "model_written")
                client = f.client()
                f.replay(client, expected_request_sha256=key)
                self.assertEqual(client.requests[0], requests[key])
                if empty:
                    self.assertEqual(key, next(r["request_sha256"] for r in rows if r["arm"] == "empty"))
                self.assertEqual(audit_snapshot_replays(f.target)["replays"][0]["memory_input_spans"], 0 if empty else 2)

    def test_separate_replays_use_new_sessions_and_sealed_forks_without_changing_parent(self):
        f = self.fixture()
        original = deepcopy(f.journal.state["snapshots"][f.written["snapshot_id"]])
        results = [f.replay() for _ in range(2)]
        self.assertEqual(len({r["bank_id"] for r in results}), 2)
        self.assertEqual(len({f.journal.scan["starts"][r["run_id"]]["session_id"] for r in results}), 2)
        self.assertEqual(f.journal.state["snapshots"][f.written["snapshot_id"]], original)
        self.assertTrue(all(f.journal.state["banks"][r["bank_id"]]["sealed"] for r in results))
        self.assertEqual(audit_snapshot_replays(f.target)["model_requests"], 2)

    def test_portable_audit_does_not_need_the_original_source_directory(self):
        f = self.fixture()
        f.replay()
        expected = audit_snapshot_replays(f.target)
        f.source.rename(f.source.with_name("unavailable-source"))
        self.assertEqual(audit_snapshot_replays(f.target), expected)

    def test_preparation_only_is_not_reported_as_an_executed_replay(self):
        f = self.fixture()
        with self.assertRaisesRegex(ContractError, "No snapshot replay"):
            audit_snapshot_replays(f.target)

    def test_wrong_hash_policy_limits_and_duplicate_run_refuse_before_mutation(self):
        f = self.fixture()
        f.replay(run_id="fixed-run")
        before = f.journal.path.read_bytes()
        for kwargs in (dict(expected_request_sha256="0" * 64), dict(run_id="fixed-run"),
                       dict(execution_limits=ExecutionLimits(2, 0, 0, 10000)),
                       dict(execution_limits=ExecutionLimits(1, 1, 0, 10000)),
                       dict(policy=MemoryPolicy(True, "none", False, 2, read_mode="preload_and_on_demand"))):
            client = f.client()
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractError):
                f.replay(client, **kwargs)
            self.assertFalse(client.requests)
            self.assertEqual(f.journal.path.read_bytes(), before)

    def test_input_rejection_retains_prepared_spans_but_not_received_exposure(self):
        f = self.fixture()
        client = f.client()
        result = f.replay(client, execution_limits=ExecutionLimits(1, 0, 0, 1))
        self.assertEqual(result["error_code"], "INPUT_SIZE_LIMIT")
        self.assertEqual(client.requests, [])
        row = audit_snapshot_replays(f.target)["replays"][0]
        self.assertEqual(row["delivery_status"], "not_submitted")
        self.assertEqual(row["memory_input_spans"], 2)
        self.assertEqual(row["received_memory_spans"], 0)
        self.assertEqual(row["cost_status"], "not_applicable")

    def test_partial_refused_and_inconsistent_responses_are_preserved_as_errors(self):
        f = self.fixture()
        variants = [body(), body(), body(), body(), body()]
        variants[0]["choices"][0]["finish_reason"] = "length"
        variants[1]["choices"][0]["message"]["refusal"] = "Declined"
        variants[2]["choices"][0]["finish_reason"] = "tool_calls"
        variants[3]["choices"].append(deepcopy(variants[3]["choices"][0]))
        variants[4]["choices"][0]["finish_reason"] = []
        for raw in variants:
            result = f.replay(f.client(raw))
            self.assertEqual(result["status"], "error")
            self.assertIsNone(result["message_ref"])
            self.assertTrue(result["stop_required"])
        self.assertEqual(audit_snapshot_replays(f.target)["statuses"], dict(error=5))

    def test_unapproved_context_is_rejected_before_transport(self):
        f = self.fixture()
        original = SourceRegistry.prepare
        def extra(registry, ctx, actor, template, messages, **kwargs):
            source = registry.template(dict(message=dict(role="user", content="UNAPPROVED_HINT")))
            return original(registry, ctx, actor, template, messages + [(source, "/message")], **kwargs)
        client = f.client()
        with patch.object(SourceRegistry, "prepare", extra):
            result = f.replay(client)
        self.assertEqual(result["error_code"], "REPLAY_INPUT_MISMATCH")
        self.assertEqual(client.requests, [])
        self.assertEqual(audit_snapshot_replays(f.target)["model_requests"], 0)

    def test_template_disguised_memory_is_detected_even_with_identical_request_bytes(self):
        f = self.fixture()
        original = SourceRegistry.add
        def disguised(registry, kind, event_id, **kwargs):
            if kind == "memory_snapshot":
                event = registry.journal.events[event_id]
                return registry.template(registry.journal.blobs.get(event["payload"]["public_message_ref"]), roles=("worker",))
            return original(registry, kind, event_id, **kwargs)
        with patch.object(SourceRegistry, "add", disguised):
            f.replay()
        with self.assertRaisesRegex(ContractError, "actual memory provenance"):
            audit_snapshot_replays(f.target)

    def test_summary_reply_tampering_is_rejected_against_raw_response(self):
        f = self.fixture()
        original = f.journal.append
        def forged(events, **kwargs):
            events = deepcopy(events)
            for event in events:
                if event["type"] == "business.transition" and event["payload"].get("origin") == "snapshot_replay.result":
                    value = f.journal.blobs.get(event["payload"]["result_ref"])
                    value["message_ref"] = f.journal.blobs.put(dict(role="assistant", content="FORGED_SUCCESS"))
                    event["payload"]["result_ref"] = f.journal.blobs.put(value)
            return original(events, **kwargs)
        with patch.object(f.journal, "append", forged):
            f.replay()
        with self.assertRaisesRegex(ContractError, "public reply differs"):
            audit_snapshot_replays(f.target)

    def test_configuration_changes_and_unsealed_forks_are_rejected(self):
        for change in ("config", "bank", "final_snapshot"):
            f = self.fixture()
            result = f.replay()
            state = deepcopy(f.journal.state)
            if change == "config":
                state["snapshot_replays"][result["run_id"]]["instruction"] = "Changed after run"
            elif change == "bank":
                state["banks"][result["bank_id"]]["sealed"] = False
            else:
                state["snapshots"][result["snapshot_id"]]["parent_snapshot_ids"] = []
            f.journal.append([], state=state)
            with self.subTest(change=change), self.assertRaises(ContractError):
                audit_snapshot_replays(f.target)

    def test_recorded_cost_or_official_score_cannot_be_forged(self):
        for change in ("cost", "score"):
            f = self.fixture()
            original = f.journal.append
            def forged(events, **kwargs):
                events = deepcopy(events)
                for event in events:
                    if change == "cost" and event["type"] == "model.request_finished":
                        event["payload"]["cost_usd"] = 9.0
                    if change == "score" and event["type"] == "outcome.scored":
                        event["payload"].update(grading_status="graded", official_success=True, official_score=1.0,
                            official_artifact_ref=f.journal.blobs.put(dict(fake_score=1)))
                return original(events, **kwargs)
            with patch.object(f.journal, "append", forged):
                f.replay()
            with self.subTest(change=change), self.assertRaises(ContractError):
                audit_snapshot_replays(f.target)

    def test_recorded_reply_review_keeps_questions_errors_and_target_violations_distinct(self):
        f = self.fixture()
        packet, rules = self.review_inputs(f)
        f.replay()
        f.replay(f.client(body(dict(role="assistant", content=None, tool_calls=[tool("book_reservation", dict(
            user_id="customer", total_baggages=3, payment_methods=[dict(payment_id="unknown", amount=10)]))]))))
        partial = body()
        partial["choices"][0]["finish_reason"] = "length"
        f.replay(f.client(partial))
        review = review_recorded_replies(f.target, packet, rules)
        self.assertEqual([r["result"]["status"] if r["result"] else None for r in review["replays"]],
                         ["no_target_write", "target_violation_detected", None])
        self.assertFalse(review["policy_effect_estimated"])
        self.assertEqual(review["new_model_or_catalog_calls"], 0)

    def test_review_packet_cannot_replace_public_context_while_reusing_source_hash(self):
        f = self.fixture()
        packet, rules = self.review_inputs(f)
        f.replay()
        packet["cases"][0]["messages"].append(dict(role="user", content="FORGED_FUTURE_CONTEXT"))
        rules["packet_sha256"] = digest(canonical(packet))
        with self.assertRaisesRegex(ContractError, "public context differs"):
            review_recorded_replies(f.target, packet, rules)


class SnapshotReplayHTTPTests(SnapshotReplayFixtureMixin, unittest.TestCase):
    def setUp(self):
        key = patch.dict("os.environ", OPENROUTER_API_KEY="test-only-key")
        key.start()
        self.addCleanup(key.stop)

    def test_mock_http_preserves_transport_bytes_usage_and_private_response(self):
        f = self.fixture(observed=True)
        result = f.replay()
        self.assertEqual(f.http_wires, [canonical(f.journal.blobs.get(f.prepared["request_ref"]))])
        audit = audit_snapshot_replays(f.target)
        self.assertEqual(audit["observed_cost_usd_decimal"], "0.0001")
        self.assertNotIn("PRIVATE_NEW_REASONING", canonical(f.journal.blobs.get(result["message_ref"])).decode())

    def test_unknown_cost_preserves_reply_and_shared_guard_stops_next_submission(self):
        f = self.fixture(observed=True)
        raw = body()
        raw["usage"].pop("cost")
        client = f.client(raw)
        first, second = f.replay(client), f.replay(client)
        self.assertEqual(first["status"], "response")
        self.assertTrue(first["unknown_cost"] and first["stop_required"])
        self.assertEqual(second["error_code"], "UNKNOWN_COST_STOP")
        self.assertEqual(len(f.http_wires), 1)
        self.assertEqual(audit_snapshot_replays(f.target)["unknown_cost_requests"], 1)

    def test_http_failure_keeps_unknown_cost_and_no_automatic_replacement(self):
        f = self.fixture(observed=True)
        result = f.replay(f.client(failure=True))
        self.assertEqual(result["error_code"], "HTTP_503")
        self.assertTrue(result["unknown_cost"])
        self.assertEqual(len(f.http_wires), 1)
        self.assertEqual(audit_snapshot_replays(f.target)["statuses"], dict(error=1))

    def test_returned_model_mismatch_and_missing_model_are_not_accepted(self):
        f = self.fixture(observed=True)
        for returned_model in ("wrong/model", None, []):
            raw = body()
            raw["model"] = returned_model
            result = f.replay(f.client(raw))
            self.assertEqual(result["error_code"], "REPLAY_MODEL_MISMATCH")
        self.assertEqual(audit_snapshot_replays(f.target)["statuses"], dict(error=3))
        identity = dict(id="known", canonical_slug=None, provider_name="OpenAI")
        self.assertEqual(replay_response_error(dict(body(), model=None), identity), "REPLAY_MODEL_MISMATCH")

    def test_transport_limits_and_route_mismatch_refuse_before_run(self):
        f = self.fixture(observed=True)
        before = f.journal.path.read_bytes()
        for client in (f.client(limits=ExecutionLimits(1, 0, 0, 1)), f.client()):
            if client.max_input_bytes == LIMITS.max_input_bytes:
                client.route = dict(ROUTE, only=["another-provider"])
            with self.assertRaisesRegex(ContractError, "transport model, route or input limit"):
                f.replay(client)
            self.assertEqual(f.journal.path.read_bytes(), before)
        self.assertFalse(f.http_wires)

    def test_spend_guard_refusal_is_recorded_without_http_or_received_exposure(self):
        f = self.fixture(observed=True)
        guard = SpendGuard(max_calls=5, max_usd=0.00000001, max_seconds=60)
        result = f.replay(f.client(guard=guard))
        self.assertEqual(result["error_code"], "SPEND_LIMIT")
        self.assertFalse(f.http_wires)
        self.assertEqual(guard.calls, 0)
        row = audit_snapshot_replays(f.target)["replays"][0]
        self.assertEqual(row["delivery_status"], "not_submitted")
        self.assertEqual(row["received_memory_spans"], 0)


if __name__ == "__main__":
    unittest.main()
