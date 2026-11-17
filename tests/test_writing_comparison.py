"""Offline comparison rendering and refusal checks; no native environment or HTTP."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.writing_comparison import (
    ARMS, _cases, _profile, comparison_requests, export_comparison,
)
from agent_memory_eval.memory.context import PAST_MEMORY_NOTICE
from agent_memory_eval.memory.preparation import SnapshotBudgetExceeded


class WrittenMemoryComparisonTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((ROOT / "protocols/drafts/tau2-written-memory-replay-v1.json").read_text())
        self.policy = MemoryPolicy(True, "none", False, 4, read_mode="preload")
        self.original = dict(model="mock-worker", reasoning=dict(effort="low"), stream=False,
            tools=[dict(type="function", function=dict(name="book_reservation", parameters={}))],
            messages=[dict(role="system", content="CURRENT_POLICY"),
                dict(role="user", content="My user id is current_customer."),
                dict(role="assistant", content="Please confirm the requested quantity."),
                dict(role="user", content="Three bags, please.")])
        self.cases = [dict(case_id="correct-positive-bags-candidate", wire=deepcopy(self.original))]
        self.banks = [dict(run_id="writer-a", feedback_level="none", repetition=1, status="completed",
            snapshot_id="snapshot-a", items=[dict(id="m_original", version="v_original",
                title="Original title", body="Original text with past user id past_customer.")]),
            dict(run_id="writer-b", feedback_level="result_only", repetition=1,
                status="empty", snapshot_id="snapshot-b", items=[])]

    def build(self, **kwargs):
        return comparison_requests(self.cases, self.banks, self.policy, "SHARED_INSTRUCTION",
            self.profile["control_notes"], **kwargs)

    def test_four_arms_preserve_all_banks_including_empty_and_original_context(self):
        original_banks, original_cases = deepcopy(self.banks), deepcopy(self.cases)
        rows, requests = self.build()
        self.assertEqual(len(rows), 8)
        self.assertEqual({r["arm"] for r in rows}, set(ARMS))
        self.assertEqual(len({r["pair_id"] for r in rows}), 2)
        self.assertEqual(self.banks, original_banks)
        self.assertEqual(self.cases, original_cases)
        for row in rows:
            wire = requests[row["request_sha256"]]
            self.assertEqual(wire["messages"][2:], self.original["messages"][1:])
            self.assertEqual({k:v for k,v in wire.items() if k != "messages"},
                             {k:v for k,v in self.original.items() if k != "messages"})
            self.assertIn(PAST_MEMORY_NOTICE, wire["messages"][0]["content"])
            self.assertEqual(wire["messages"][1]["role"], "user")
            self.assertEqual(row["outcome"], "not_measured")
            self.assertFalse(row["response_observed"])
            self.assertIsNone(row["fits_input_limit"])
            self.assertEqual(digest(canonical(wire)), row["request_sha256"])
        authored = next(r for r in rows if r["arm"] == "model_written" and r["writer_run_id"] == "writer-a")
        self.assertEqual(json.loads(requests[authored["request_sha256"]]["messages"][1]["content"])["items"],
                         self.banks[0]["items"])

    def test_identical_empty_and_common_controls_are_explicit_shared_inputs(self):
        rows, requests = self.build()
        empty_hashes = {r["request_sha256"] for r in rows if r["arm"] == "empty"}
        empty_written = next(r for r in rows if r["arm"] == "model_written" and r["writing_status"] == "empty")
        self.assertEqual(empty_hashes, {empty_written["request_sha256"]})
        self.assertEqual(len(requests), 4)  # Three common controls plus the nonempty bank.
        for arm in ("generic", "researcher"):
            self.assertEqual(len({r["request_sha256"] for r in rows if r["arm"] == arm}), 1)

    def test_control_provenance_and_arm_labels_are_not_in_model_visible_items(self):
        rows, requests = self.build()
        for row in rows:
            if row["arm"] not in {"generic", "researcher"}:
                continue
            items = json.loads(requests[row["request_sha256"]]["messages"][1]["content"])["items"]
            self.assertEqual(set(items[0]), {"id", "version", "title", "body"})
            self.assertRegex(items[0]["id"], r"^m_[0-9a-f]{32}$")
            self.assertRegex(items[0]["version"], r"^v_[0-9a-f]{32}$")
            self.assertNotIn(row["arm"], items[0]["title"].lower())
            self.assertNotIn("information_access", canonical(items).decode())

    def test_no_automatic_zero_baggage_rule_is_applied_to_positive_correct_candidate(self):
        with patch("agent_memory_eval.analysis.decision_replay.screen_reply", side_effect=AssertionError("screen called")):
            rows, _ = self.build()
        self.assertTrue(all(r["outcome"] == "not_measured" for r in rows))

    def test_oversized_requests_are_retained_as_not_fitting_without_truncation(self):
        rows, requests = self.build(max_input_bytes=1)
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(r["fits_input_limit"] is False for r in rows))
        self.assertIn("Original text", canonical(requests).decode())

    def test_invalid_input_caps_and_duplicate_banks_are_rejected(self):
        for cap in (True, False, 0, -1, 1.5):
            with self.subTest(cap=cap), self.assertRaises(ContractError):
                self.build(max_input_bytes=cap)
        self.banks.append(deepcopy(self.banks[0]))
        with self.assertRaisesRegex(ContractError, "exactly once"):
            self.build()

    def test_each_declared_bank_limit_is_enforced_without_silent_truncation(self):
        for field, value in (("max_items", 0), ("max_payload_bytes", 1), ("max_return_bytes", 1)):
            with self.subTest(field=field):
                self.policy = MemoryPolicy(True, "none", False, 4, read_mode="preload", **{field:value})
                with self.assertRaises(SnapshotBudgetExceeded):
                    self.build()

    def test_pure_preparation_refuses_mutable_or_query_policies(self):
        for policy in (MemoryPolicy(True, "post_task", False, 4, read_mode="preload"),
                       MemoryPolicy(True, "none", True, 4, read_mode="preload"),
                       MemoryPolicy(True, "none", False, 4, read_mode="preload_and_on_demand")):
            self.policy = policy
            with self.assertRaisesRegex(ContractError, "frozen read-only"):
                self.build()

    def test_ready_or_query_enabled_profiles_and_conflicting_tasks_are_rejected(self):
        _profile(self.profile)
        for mutate in (lambda p:p.update(status="ready"),
                       lambda p:p["delivery"]["policy"].update(read_mode="preload_and_on_demand"),
                       lambda p:p["cases"][0].update(task_id="7")):
            profile = deepcopy(self.profile)
            mutate(profile)
            with self.assertRaises(ContractError):
                _profile(profile)

    def test_declared_delivery_must_match_the_actual_fixed_renderer(self):
        for field, value in (("format", "other/v1"), ("memory_role", "system"), ("position", 2), ("position", True)):
            profile = deepcopy(self.profile)
            profile["delivery"][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ContractError, "Supported delivery"):
                _profile(profile)

    def test_exports_refuse_existing_directories_and_original_source_descendants_before_collection(self):
        receipt = dict(source_root="runs/writing-source")
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for source in (root / "runs/writing-source", root / "runs/baseline-source"):
                source.mkdir(parents=True)
            profile = deepcopy(self.profile)
            profile["decision_source"]["path"] = "runs/baseline-source"
            with patch("agent_memory_eval.experiments.writing_comparison.asset_text", return_value=json.dumps(receipt)), \
                    patch("agent_memory_eval.experiments.writing_comparison.prepare_comparison",
                          side_effect=AssertionError("read or preparation attempted")):
                for output in (root / "runs/writing-source" / "new", root / "runs/baseline-source" / "new", root):
                    with self.subTest(output=output), self.assertRaises(ContractError):
                        export_comparison(profile, root, output)

    def test_changed_decision_log_is_rejected_before_parsing(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source").mkdir()
            (root / "source/log.jsonl").write_text("modified")
            profile = deepcopy(self.profile)
            profile["decision_source"] = dict(path="source", log_sha256="0" * 64)
            with patch("agent_memory_eval.experiments.writing_comparison.read_experiment",
                       side_effect=AssertionError("unverified source parsed")):
                with self.assertRaisesRegex(ContractError, "source log changed"):
                    _cases(profile, root)

    def test_exported_request_bytes_match_their_named_transport_hash(self):
        rows, requests = self.build()
        manifest = dict(rows=rows, candidate_cases=[], identical_input_multiplicity={}, status="prepared_offline_draft")
        with TemporaryDirectory() as folder:
            root, output = Path(folder), Path(folder) / "outputs" / "new"
            receipt = dict(source_root="runs/writing-source")
            with patch("agent_memory_eval.experiments.writing_comparison.asset_text", return_value=json.dumps(receipt)), \
                    patch("agent_memory_eval.experiments.writing_comparison.prepare_comparison", return_value=(manifest, requests)):
                export_comparison(self.profile, root, output)
            for key in requests:
                self.assertEqual(digest((output / "requests" / (key + ".json")).read_bytes()), key)

    def test_candidate_is_bound_to_its_allocation_worker_request_and_normal_reply(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source").mkdir()
            data = b"verified fixture source"
            (root / "source/log.jsonl").write_bytes(data)
            profile = deepcopy(self.profile)
            profile["decision_source"] = dict(path="source", log_sha256=digest(data))
            raw = dict(choices=[dict(finish_reason="tool_calls", message=dict(role="assistant", content=None))])
            request_ref = dict(sha256=digest(canonical(self.original)))
            response_ref = dict(sha256=digest(canonical(raw)))
            profile["cases"] = [dict(case_id="candidate", task_id="14", repetition=1,
                run_id="source-run", request_id="decision", request_sha256=request_ref["sha256"],
                response_sha256=response_ref["sha256"])]
            allocation = dict(run_id="source-run", task_id="14", repetition=1)
            control_ref = dict(sha256="control")
            events = [
                dict(event_id="started", run_id="source-run", type="model.request_started",
                     payload=dict(request_id="decision", actor_role="worker", request_ref=request_ref)),
                dict(event_id="finished", run_id="source-run", type="model.request_finished",
                     payload=dict(request_id="decision", status="response", response_ref=response_ref)),
                dict(event_id="ended", run_id="source-run", type="run.ended", payload=dict(grading_status="graded")),
                dict(event_id="graded", run_id="source-run", type="outcome.scored",
                     payload=dict(grading_status="graded", official_success=True)),
            ]
            scan = dict(tail_bytes=0, pending_requests=[], incomplete_runs=[],
                        events={e["event_id"]:e for e in events})
            values = {"control":dict(schedule=[allocation]), request_ref["sha256"]:self.original,
                      response_ref["sha256"]:raw}
            class Blobs:
                def get(self, ref):
                    return values[ref["sha256"]]
            with patch("agent_memory_eval.experiments.writing_comparison.read_experiment",
                       return_value=(scan, dict(baseline_screening=dict(control_ref=control_ref)), "fixture")), \
                    patch("agent_memory_eval.experiments.writing_comparison.BlobStore", return_value=Blobs()):
                case = _cases(profile, root)[0]
                self.assertTrue(case["original_episode_success"])
                self.assertEqual(case["decision_policy_adjudication"], "pending_independent_review")
                for field, value in (("task_id", "23"), ("repetition", 2), ("request_sha256", "0" * 64),
                                     ("response_sha256", "0" * 64)):
                    forged = deepcopy(profile)
                    forged["cases"][0][field] = value
                    with self.subTest(field=field), self.assertRaises(ContractError):
                        _cases(forged, root)
                events[0]["payload"]["actor_role"] = "user_simulator"
                with self.assertRaises(ContractError):
                    _cases(profile, root)
                events[0]["payload"]["actor_role"] = "worker"
                raw["choices"][0]["finish_reason"] = "length"
                with self.assertRaisesRegex(ContractError, "normal reply"):
                    _cases(profile, root)


if __name__ == "__main__":
    unittest.main()
