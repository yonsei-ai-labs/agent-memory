"""Exercise the real writing, four-arm staging, transport and audit paths with mock HTTP."""

from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.writing_export import collect
from agent_memory_eval.analysis.written_replay_batch import audit_batch
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.replay_schedule import input_groups, schedule, validate_profile
from agent_memory_eval.experiments.writing_comparison import export_comparison
from agent_memory_eval.experiments.written_replay_batch import run_batch, stage_batch
from agent_memory_eval.recording.journal import Journal
from test_openrouter import Reply, response
from test_writing_exploration import IDENTITY, WritingExplorationFixture


def asset(path, value):
    path.write_bytes(canonical(value) + b"\n")
    return dict(path=str(path.relative_to(ROOT)), sha256=digest(path.read_bytes()))


class BatchSources(WritingExplorationFixture, unittest.TestCase):
    def prepare(self):
        self.setUp()
        with Journal(self.source, "mock-source") as j:
            state = deepcopy(j.state)
            allocation = dict(run_id="source", repetition=1, task_id="14")
            state["baseline_screening"] = dict(control_ref=j.blobs.put(dict(schedule=[allocation])))
            j.append([], state=state)
            events = [e for e in j.events.values() if e["payload"].get("request_id") == self.decision]
            request = next(e["payload"]["request_ref"]["sha256"] for e in events if e["type"] == "model.request_started")
            reply = next(e["payload"]["response_ref"]["sha256"] for e in events if e["type"] == "model.request_finished")
        source = self.profile["source"]
        source["log_sha256"] = digest((self.source / "log.jsonl").read_bytes())
        self.run_study(empty_level="none")
        metrics = collect(self.output)["metrics"]
        comparison = read_json((ROOT / "protocols/drafts/tau2-written-memory-replay-v2.json").read_bytes())
        comparison["writing_result_ref"] = asset(self.base / "receipt.json", dict(format="agent-memory-writing-result/v1",
            source_root=str(self.output.relative_to(ROOT)), raw_log_sha256=metrics["run_log_sha256"],
            audit=metrics["audit"], by_feedback=metrics["by_feedback"]))
        comparison["decision_source"] = {k:source[k] for k in ("path", "log_sha256")}
        comparison["cases"] = [dict(comparison["cases"][0], case_id="fixture-booking", repetition=1, run_id="source",
                                     request_id=self.decision, request_sha256=request, response_sha256=reply)]
        self.preparation = self.base / "prepared"
        export_comparison(comparison, ROOT, self.preparation)
        self.manifest = read_json((self.preparation / "preparation.json").read_bytes())
        packet = self.manifest["case_review_packet"]
        def evidence(index):
            msg = packet["cases"][0]["messages"][index]
            return dict(message_index=index, message_sha256=digest(canonical(msg)), excerpt=msg["content"])
        self.rubrics = dict(format="agent-memory-written-comparison-rubrics/v1", status="reviewed",
            reviewer=dict(id="fixture-only", kind="human", independence_declared=True, historical_outcomes_visible=False),
            packet_sha256=digest(canonical(packet)), scope="public_quantity_and_certificate_checks_not_full_policy_or_episode_success",
            cases=[dict(case_id="fixture-booking", baggage=dict(interpretation="explicit_quantity", expected_quantity=0,
                evidence=evidence(1), note="Fixture user requests no bags."), certificates=dict(max_per_reservation=1,
                evidence=evidence(0)), approval_review="Fixture only", other_policy_review="Fixture only")])
        self.batch = dict(read_json((ROOT / "protocols/drafts/tau2-written-memory-batch-v1.json").read_bytes()),
            comparison_ref=asset(self.base / "comparison.json", comparison), rubrics_ref=asset(self.base / "rubrics.json", self.rubrics),
            preparation_sha256=digest(canonical(self.manifest)), status="ready", repetitions=2, schedule_seed=51,
            max_input_bytes=64000, max_model_requests=8, max_reported_cost_usd=1, max_seconds=120, timeout_seconds=10,
            bank_limits=dict(max_items=3, max_payload_bytes=6000, max_return_bytes=7000), analysis_plan="Fixture comparisons only",
            control_note_review=dict(packet_sha256=digest(canonical(packet)), reviewer="fixture reviewer", independence_declared=True,
                                     generic="Fixture evidence only", researcher="Fixture evidence only"),
            paid_authorization="Mock transport fixture only")


class WrittenReplayBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = BatchSources()
        cls.addClassCleanup(cls.source.doCleanups)
        cls.source.prepare()

    def setUp(self):
        folder = TemporaryDirectory(dir=self.source.base)
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.out = self.folder / "batch"
        self.profile = deepcopy(self.source.batch)
        self.http = []
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch.dict("os.environ", OPENROUTER_API_KEY="fixture-only"))
        module = "agent_memory_eval.experiments.written_replay_batch."
        self.stack.enter_context(patch(module + "implementation_identity", return_value=IDENTITY))
        self.catalog = self.stack.enter_context(patch(module + "model_catalog", return_value=self.source.catalog))
        self.stack.enter_context(patch(module + "model_endpoints", return_value=self.source.endpoints))

    def execute(self, change_reply=None):
        def opener(request, timeout):
            self.http.append(request.data)
            reply = response(0.0001)
            if change_reply is not None:
                change_reply(len(self.http), reply)
            return Reply(reply)
        run_batch(self.profile, ROOT, self.source.preparation, self.out, opener=opener)
        return audit_batch(self.out, expected_profile=self.profile)

    def test_complete_shared_repetitions_and_portable_source_assignment(self):
        report = self.execute()
        self.assertEqual((report["status"], len(self.http), report["submitted_requests"]), ("completed", 8, 8))
        self.assertEqual((len(report["allocations"]), len(report["distinct_inputs"])), (16, 8))
        self.assertEqual(report["reported_replay_cost_usd_decimal"], "0.0008")
        self.assertEqual(report["historical_writing_usage"]["requests"], 3)
        self.assertEqual(report["screens_by_distinct_response"], {"no_target_write":8})
        self.assertEqual([digest(wire) for wire in self.http], [row["request_sha256"] for row in report["distinct_inputs"]])
        self.assertEqual(Counter(self.http).most_common()[0][1], 2)
        for wire in self.http:
            self.assertNotIn(b"HIDDEN_GRADING_ANSWER", wire)
            self.assertNotIn(b"fixture reviewer", wire)
        for repetition in (1, 2):
            rows = [r for r in report["allocations"] if r["repetition"] == repetition and r["writing_status"] == "empty"]
            empty = next(r for r in rows if r["arm"] == "empty")
            written = next(r for r in rows if r["arm"] == "model_written")
            self.assertEqual(written["run_id"], empty["run_id"])
            self.assertNotEqual(written["local_input_snapshot_id"], empty["local_input_snapshot_id"])
            self.assertEqual(len({r["arm"] for r in rows}), 4)
        # Move original decisions, writing journal, export AND rubric away before repeating the audit.
        with ExitStack() as restore:
            for path in (self.source.source, self.source.output, self.source.preparation, self.source.base / "rubrics.json"):
                dest = self.folder / ("absent-" + path.name)
                path.rename(dest)
                restore.callback(dest.rename, path)
            self.assertEqual(audit_batch(self.out), report)
        self.assertFalse(report["official_scores_computed"])

    def test_call_budget_keeps_unattempted_suffix_in_all_four_arm_denominators(self):
        self.profile["max_model_requests"] = 2
        report = self.execute()
        self.assertEqual((report["stop_reason"], len(self.http), report["skipped_inputs"]), ("call_limit", 2, 6))
        skipped = [r for r in report["allocations"] if r["status"] == "skipped"]
        self.assertEqual(len(skipped), report["skipped_logical_rows"])
        self.assertEqual(len(report["allocations"]), 16)
        self.assertTrue(all(r["screen_status"] is None for r in skipped))
        self.assertTrue(all(r["skip_reason"] == "call_limit" for r in skipped))
        self.assertTrue(all(r["error_code"] is None for r in report["distinct_inputs"] if r["status"] == "skipped"))

    def test_unknown_cost_mid_block_keeps_reply_and_stops_before_next_http(self):
        def change(index, raw):
            if index == 2:
                raw["usage"].pop("cost")
        report = self.execute(change)
        self.assertEqual((report["observed_replies"], report["unknown_cost_requests"], report["stop_reason"]), (2, 1, "unknown_cost"))
        self.assertEqual(report["reported_replay_cost_usd_decimal"], "0.0001")
        self.assertEqual((len(self.http), report["skipped_inputs"]), (2, 6))

    def test_last_rejected_completion_is_an_error_even_with_no_skipped_inputs(self):
        self.profile["repetitions"] = 1
        def change(index, raw):
            if index == 4:
                raw["choices"][0]["finish_reason"] = "length"
        report = self.execute(change)
        self.assertEqual((report["status"], report["stop_reason"], report["skipped_inputs"]), ("stopped", "REPLAY_INCOMPLETE_REPLY", 0))
        self.assertEqual((report["observed_replies"], report["errors"]), (3, 1))
        self.assertIsNone(report["distinct_inputs"][-1]["screen"])

    def test_spend_reservation_denied_before_http_does_not_inflate_submissions(self):
        self.profile["max_reported_cost_usd"] = 0.00000001
        report = self.execute()
        self.assertEqual((len(self.http), report["submitted_requests"], report["replay_model_requests"]), (0, 0, 1))
        self.assertEqual((report["stop_reason"], report["skipped_inputs"], report["unknown_cost_requests"]), ("SPEND_LIMIT", 7, 0))

    def test_draft_cannot_contact_catalog_or_create_live_output(self):
        self.profile.update(status="draft", paid_authorization=None)
        with self.assertRaisesRegex(ContractError, "ready"):
            self.execute()
        self.catalog.assert_not_called()
        self.assertFalse(self.out.exists())
        stage = stage_batch(self.profile, ROOT, self.source.preparation, self.out)
        self.assertEqual(stage["replay_model_requests"], 0)
        audit = audit_batch(self.out)
        self.assertEqual(audit["status"], "staged")
        self.assertEqual(audit["input_groups"], stage["input_groups"])
        self.assertEqual(sorted(len(g["members"]) for g in audit["input_groups"]), [1, 2, 2, 3])
        self.assertTrue(all("run_id" not in g for g in audit["input_groups"]))
        self.assertEqual(stage["input_snapshots"], 5)

    def test_ready_review_bank_and_input_caps_are_checked_before_any_catalog_call(self):
        original = deepcopy(self.profile)
        for index, change in enumerate((dict(max_input_bytes=10), dict(bank_limits=dict(original["bank_limits"], max_items=0)),
                dict(control_note_review=dict(original["control_note_review"], packet_sha256="9"*64)),
                dict(rubrics_ref=asset(self.folder / "provisional.json", dict(self.source.rubrics, status="provisional"))))):
            with self.subTest(change=change):
                self.profile = dict(original, **change)
                self.out = self.folder / str(index)
                with self.assertRaises(ContractError):
                    self.execute()
        self.catalog.assert_not_called()

    def test_guards_and_repetitions_cannot_be_inferred_from_legacy_draft(self):
        for field in ("max_model_requests", "schedule_seed", "bank_limits", "analysis_plan", "control_note_review", "paid_authorization"):
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_profile(dict(self.profile, **{field:None}), require_ready=True)
        rows = self.source.manifest["rows"]
        with self.assertRaises(ContractError):
            input_groups(rows[:-1])
        # Even equal bytes across two case labels are separate draws.
        copied = [dict(r, case_id="another-case", pair_id="another-case:" + r["writer_run_id"]) for r in rows]
        groups = input_groups(rows + copied)
        self.assertEqual(len(groups), 8)
        plan = schedule(groups, 2, 91)
        self.assertEqual(len({r["run_id"] for r in plan}), 16)
        self.assertEqual(plan, schedule(groups, 2, 91))

    def test_skip_or_completion_claim_without_matching_events_is_rejected(self):
        self.profile["max_model_requests"] = 1
        from agent_memory_eval.experiments import written_replay_batch
        record = written_replay_batch._record
        def altered(journal, phase, **payload):
            if phase == "finished":
                payload.update(stop_reason=None, skipped_run_ids=[])
            record(journal, phase, **payload)
        with patch.object(written_replay_batch, "_record", altered), self.assertRaisesRegex(ContractError, "skipped"):
            self.execute()
        self.assertEqual(len(self.http), 1)

    def test_output_is_exclusive_and_must_be_outside_preserved_sources(self):
        for path in (self.source.output / "new", self.source.preparation / "new", self.folder):
            with self.subTest(path=path), self.assertRaises(ContractError):
                stage_batch(self.profile, ROOT, self.source.preparation, path)
        self.catalog.assert_not_called()

    def test_elapsed_guard_before_first_replay_preserves_every_assignment(self):
        self.profile["max_seconds"] = 1e-12
        report = self.execute()
        self.assertEqual((report["stop_reason"], report["replay_model_requests"], report["skipped_inputs"]), ("time_limit", 0, 8))
        self.assertEqual((len(self.http), report["skipped_logical_rows"]), (0, 16))

    def test_interruption_after_a_completed_call_leaves_unfinished_batch_detectable(self):
        from agent_memory_eval.experiments import written_replay_batch
        replay = written_replay_batch.run_snapshot_replay
        def interrupt(*args, **kwargs):
            if self.http:
                raise KeyboardInterrupt
            return replay(*args, **kwargs)
        with patch.object(written_replay_batch, "run_snapshot_replay", interrupt), self.assertRaises(KeyboardInterrupt):
            self.execute()
        with self.assertRaisesRegex(ContractError, "Incomplete"):
            audit_batch(self.out)
        self.assertEqual(len(self.http), 1)

    def test_portable_audit_detects_swap_between_empty_control_and_empty_writer_bank(self):
        from agent_memory_eval.experiments import written_replay_batch
        record = written_replay_batch._record
        def altered(journal, phase, **payload):
            control = journal.blobs.get(payload["control_ref"])
            catalog = control["catalog"]
            run = next(row["run_id"] for row in self.source.manifest["writing_allocations"] if row["status"] == "empty")
            catalog["writers"][run], catalog["controls"]["empty"] = catalog["controls"]["empty"], catalog["writers"][run]
            record(journal, phase, control_ref=journal.blobs.put(control))
        with patch.object(written_replay_batch, "_record", altered):
            stage_batch(self.profile, ROOT, self.source.preparation, self.out)
        with self.assertRaisesRegex(ContractError, "source, producer or frozen bank"):
            audit_batch(self.out)


if __name__ == "__main__":
    unittest.main()
