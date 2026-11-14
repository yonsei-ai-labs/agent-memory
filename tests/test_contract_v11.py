"""Negative contract examples, not runtime isolation or benchmark tests."""

from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from contract_v11 import (artifact, canonical, digest, inspect_journal, read_json,
                          record_hash, validate_batch, validate_fixture,
                          validate_manifest, validate_public_return, validators)
from contract_fixture_v11 import write_fixture, write_journal


class ContractV11Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checks = validators()

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = write_fixture(Path(self.temp.name))
        self.journal = self.base / "log.jsonl"
        self.records = [read_json(line) for line in self.journal.read_bytes().splitlines()]
        self.scan = inspect_journal(self.journal.read_bytes(), self.checks)
        self.bundle = read_json((self.base / "fixture.json").read_bytes())
        self.catalog = artifact(self.base, self.bundle["sources_ref"])

    def event(self, name):
        return next(event for row in self.records for event in row["events"]
                    if event["event_id"] == name)

    def manifest(self, request):
        event = self.event(f"e_start_{request}")
        return event, artifact(self.base, event["payload"]["input_manifest_ref"])

    def check_manifest(self, event, manifest, catalog=None):
        return validate_manifest(self.base, manifest, event,
                                 self.catalog if catalog is None else catalog,
                                 self.scan["events"], self.bundle["approved_templates"], self.checks)

    def rewrite(self):
        write_journal(self.journal, self.records)

    def test_closed_fixture_links_all_event_kinds_and_repeated_titles(self):
        result = validate_fixture(self.base)
        self.assertEqual(result["synthetic_events_valid"], 29)
        self.assertEqual(result["input_spans_verified"], 6)
        self.assertFalse(result["runtime_readiness_verified"])
        self.assertEqual(result["benchmark_or_model_calls"], 0)
        store = next(row for row in self.records if row["operation"]
                     and row["operation"]["operation_id"] == "op_store")
        self.assertEqual([e["type"] for e in store["events"]], ["memory.stored", "memory.returned"])
        self.assertIn("outcome.scored", {e["type"] for e in self.scan["events"].values()})

    def test_unterminated_last_record_is_preserved_and_run_incomplete(self):
        data = self.journal.read_bytes()[:-1]
        self.journal.write_bytes(data)
        scan = inspect_journal(data, self.checks)
        self.assertGreater(scan["tail_bytes"], 0)
        self.assertEqual(scan["incomplete_runs"], ["r_probe"])
        self.assertEqual(self.journal.read_bytes(), data)

    def test_missing_end_is_incomplete_even_with_no_pending_requests(self):
        self.records.pop()
        self.rewrite()
        scan = inspect_journal(self.journal.read_bytes(), self.checks)
        self.assertEqual(scan["pending_requests"], [])
        self.assertEqual(scan["incomplete_runs"], ["r_probe"])

    def test_valid_json_tampering_in_final_record_is_corruption(self):
        self.records[-1]["events"][0]["payload"]["termination_reason"] = "tampered"
        self.journal.write_bytes(b"".join(canonical(row) + b"\n" for row in self.records))
        with self.assertRaisesRegex(ValueError, "checksum"):
            inspect_journal(self.journal.read_bytes(), self.checks)

    def test_complete_malformed_line_is_not_discarded_as_tail(self):
        with self.assertRaisesRegex(ValueError, "Corrupt complete"):
            inspect_journal(self.journal.read_bytes() + b'{"broken":\n', self.checks)

    def test_removed_middle_commit_breaks_chain(self):
        del self.records[4]
        with self.assertRaisesRegex(ValueError, "sequence or previous hash"):
            inspect_journal(b"".join(canonical(row) + b"\n" for row in self.records), self.checks)

    def test_event_gap_is_rejected_even_with_valid_commit_hash(self):
        self.records[-1]["events"][0]["event_seq"] += 1
        self.records[-1]["record_sha256"] = record_hash(self.records[-1])
        with self.assertRaisesRegex(ValueError, "Event sequence"):
            inspect_journal(b"".join(canonical(row) + b"\n" for row in self.records), self.checks)

    def test_duplicate_operation_cannot_be_another_committed_retry(self):
        duplicate = deepcopy(next(row for row in self.records if row["operation"]))
        self.records.append(duplicate)
        self.rewrite()
        with self.assertRaisesRegex(ValueError, "retry"):
            inspect_journal(self.journal.read_bytes(), self.checks)

    def test_reused_session_is_rejected_despite_fresh_flag(self):
        for row in self.records:
            for event in row["events"]:
                if event["run_id"] == "r_probe":
                    event["session_id"] = "s_collect"
        self.rewrite()
        with self.assertRaisesRegex(ValueError, "Session ID shared"):
            validate_fixture(self.base)

    def test_unknown_bank_and_snapshot_version_are_rejected(self):
        for name, field, value, message in (("e_get", "bank_id", "b_other", "another bank"),
                                            ("e_store", "bank_id", "b_other", "another bank"),
                                            ("e_get", "version", "v_unknown", "fixed read view")):
            with self.subTest(name=name, field=field):
                event = self.event(name)
                original = deepcopy(event["payload"])
                if field == "bank_id":
                    event["payload"][field] = value
                else:
                    event["payload"]["items"][0]["version_id"] = value
                self.rewrite()
                with self.assertRaisesRegex(ValueError, message):
                    validate_fixture(self.base)
                event["payload"] = original

    def test_missing_exposure_events_rejected_after_rehashing(self):
        self.records = [dict(row, events=[e for e in row["events"] if e["type"] != "memory.input_included"])
                        for row in self.records]
        self.records = [row for row in self.records if row["events"]]
        self.rewrite()
        with self.assertRaisesRegex(ValueError, "do not cover"):
            validate_fixture(self.base)

    def test_uncovered_request_and_overlapping_provenance_rejected(self):
        event, manifest = self.manifest("q5")
        missing = deepcopy(manifest)
        missing["bindings"].pop(0)
        with self.assertRaisesRegex(ValueError, "uncovered"):
            self.check_manifest(event, missing)
        manifest["bindings"].append(deepcopy(manifest["bindings"][0]))
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            self.check_manifest(event, manifest)

    def test_wrapper_hint_rejected_even_when_request_hash_is_updated(self):
        event, manifest = self.manifest("q3")
        request = artifact(self.base, manifest["request_ref"])
        request["messages"][0]["content"] += " 비공개 정답 표식"
        ref = dict(manifest["request_ref"])
        (self.base / ref["path"]).write_bytes(canonical(request))
        ref["sha256"] = digest(canonical(request))
        event["payload"]["request_ref"] = manifest["request_ref"] = ref
        with self.assertRaisesRegex(ValueError, "approved source"):
            self.check_manifest(event, manifest)

    def test_unapproved_template_and_wrong_role_rejected(self):
        event, manifest = self.manifest("q3")
        catalog = deepcopy(self.catalog)
        catalog["template"]["artifact_ref"] = catalog["task_p"]["artifact_ref"]
        with self.assertRaisesRegex(ValueError, "Unapproved template"):
            self.check_manifest(event, manifest, catalog)
        catalog = deepcopy(self.catalog)
        catalog["template"]["roles"] = ["writer"]
        with self.assertRaisesRegex(ValueError, "role or run"):
            self.check_manifest(event, manifest, catalog)

    def test_future_producer_and_other_run_source_rejected(self):
        event, manifest = self.manifest("q5")
        for producer in ("e_finish_q5", "e_finish_q1"):
            with self.subTest(producer=producer):
                catalog = deepcopy(self.catalog)
                catalog["authored_get"]["producer_event_id"] = producer
                with self.assertRaisesRegex(ValueError, "another run or a future"):
                    self.check_manifest(event, manifest, catalog)

    def test_self_authored_text_needs_no_fake_return_but_needs_spans(self):
        event, manifest = self.manifest("q2")
        spans = self.check_manifest(event, manifest)
        self.assertTrue(all(span["item_id"] is None for span in spans))
        self.assertEqual(self.event("e_input_q2:m0")["payload"]["source"],
                         {"kind": "self_authored", "response_event_id": "e_finish_q1"})
        catalog = deepcopy(self.catalog)
        catalog["authored_store"]["memory_spans"] = []
        with self.assertRaisesRegex(ValueError, "not completely annotated"):
            self.check_manifest(event, manifest, catalog)

    def test_public_prefix_mutation_rejected_but_later_append_is_legal(self):
        path = self.base / "experience.jsonl"
        original = path.read_bytes()
        # The committed prefix remains identical; later public observations do not alter it.
        path.write_bytes(original + canonical({"seq": 2, "message": "later observation"}) + b"\n")
        validate_fixture(self.base)
        path.write_bytes(original.replace("배송지".encode(), "수신처".encode()))
        with self.assertRaisesRegex(ValueError, "prefix hash"):
            validate_fixture(self.base)

    def test_future_cutoff_rejected_even_with_correct_prefix_hash(self):
        event, manifest = self.manifest("q1")
        catalog = deepcopy(self.catalog)
        catalog["public_c"]["prefix"]["through_event_seq"] = event["event_seq"]
        with self.assertRaisesRegex(ValueError, "future event"):
            self.check_manifest(event, manifest, catalog)

    def test_artifact_change_and_path_escape_rejected(self):
        ref = self.catalog["get"]["artifact_ref"]
        (self.base / ref["path"]).write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            artifact(self.base, ref)
        with self.assertRaisesRegex(ValueError, "inside its bundle"):
            artifact(self.base, dict(ref, path="../outside.json"))
        # A response not reused as a source is still a required recorded artifact.
        (self.base / "examples/response-final.json").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "hash mismatch: examples/response-final.json"):
            validate_fixture(self.base)

    def test_public_list_cannot_leak_bank_revision_or_body(self):
        payload = self.event("e_list")["payload"]
        wire = artifact(self.base, payload["result_ref"])
        for field, message in (("head_revision", "revision leaked"), ("body", "beyond its public fields")):
            with self.subTest(field=field):
                result = json.loads(wire["content"])
                result["items"][0][field] = "unwanted"
                altered = dict(wire, content=canonical(result).decode())
                with self.assertRaisesRegex(ValueError, message):
                    validate_public_return(altered, payload)

    def test_interleaved_side_effect_order_and_stop_preflight(self):
        batches = artifact(self.base, self.bundle["tool_batches_ref"])
        batches[0]["execution_order"] = ["memory_1", "business_1", "business_2"]
        with self.assertRaisesRegex(ValueError, "Execution order"):
            validate_batch(batches[0])
        batches[1]["execution_order"] = ["memory_1"]
        with self.assertRaisesRegex(ValueError, "before any effect"):
            validate_batch(batches[1])

    def test_reordered_tool_results_rejected(self):
        batch = artifact(self.base, self.bundle["tool_batches_ref"])[0]
        batch["result_order"].reverse()
        with self.assertRaisesRegex(ValueError, "original calls"):
            validate_batch(batch)


class PlanV11Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = validators()["run-plan"]
        cls.draft = read_json((ROOT / "protocols/drafts/tau2-two-pass-v1.1.json").read_bytes())

    def ready_shape(self):
        """Shape-only synthetic values. No such references, provider, or runtime exist."""
        ready = deepcopy(self.draft)
        ref = {"path": "synthetic-only.json", "sha256": "0" * 64, "media_type": "application/json"}
        ready["plan_status"] = "ready"
        ready["worker"]["parameters_confirmed"] = True
        ready["benchmark"].update(source_revision="0" * 40, domain="synthetic-only",
                                  task_manifest_ref="synthetic-only.json", task_manifest_sha256="0" * 64,
                                  runtime={"user_simulator": {"mode": "model", "implementation": "synthetic-only",
                                           "provider": "synthetic-only", "model_id": "synthetic-only",
                                           "parameters": {}, "parameters_confirmed": True},
                                           "native_limits": {"max_steps": 1, "max_errors": 1},
                                           "response_cache": {"enabled": False, "verified": True},
                                           "adapter_config_ref": ref})
        ready["sampling"].update(independent_bank_replicates=1, order_count=1, trials_per_probe=1, schedule_seed=0)
        ready["catalog"]["page_size"] = 1
        ready["limits"]["max_model_requests_per_run"] = 1
        ready["analysis"].update(pairing_ref="synthetic-only", uncertainty_method="synthetic-only",
                                annotation_protocol_ref="synthetic-only", cost_allocation_ref="synthetic-only",
                                analysis_confirmed=True)
        for condition in ready["conditions"]:
            if condition["write_timing"] in ("during_task", "during_and_post"):
                condition["instructions"]["solve"] = ref
            if condition["write_timing"] in ("post_task", "during_and_post"):
                condition["instructions"]["post_task_write"] = ref
        return ready

    def test_draft_remains_unresolved_and_cannot_be_promoted(self):
        self.validator.validate(self.draft)
        self.assertEqual(self.draft["plan_status"], "draft")
        self.assertIsNone(self.draft["benchmark"]["runtime"])
        self.assertFalse(self.validator.is_valid(dict(self.draft, plan_status="ready")))

    def test_ready_requires_resolved_benchmark_runtime(self):
        self.validator.validate(self.ready_shape())
        for field in ("user_simulator", "native_limits", "response_cache", "adapter_config_ref"):
            with self.subTest(missing=field):
                ready = self.ready_shape()
                del ready["benchmark"]["runtime"][field]
                self.assertFalse(self.validator.is_valid(ready))

    def test_independent_ready_shape_rejects_response_cache_and_native_gaps(self):
        for section, field, value in (("response_cache", "enabled", True),
                                      ("response_cache", "verified", False),
                                      ("native_limits", "max_steps", None),
                                      ("native_limits", "max_errors", 0),
                                      ("user_simulator", "parameters_confirmed", False),
                                      ("user_simulator", "model_id", None)):
            with self.subTest(section=section, field=field):
                ready = self.ready_shape()
                ready["benchmark"]["runtime"][section][field] = value
                self.assertFalse(self.validator.is_valid(ready))

    def test_stage_instructions_required_only_when_applicable(self):
        ready = self.ready_shape()
        self.validator.validate(ready)
        self.assertIsNone(ready["limits"]["storage_max_items"])
        self.assertIsNone(ready["conditions"][1]["instructions"]["solve"])
        self.assertIsNone(ready["conditions"][2]["instructions"]["post_task_write"])
        self.assertIsNone(ready["conditions"][1]["preload_selector_ref"])
        for index, stage in ((1, "post_task_write"), (2, "solve"), (3, "post_task_write"), (3, "solve")):
            with self.subTest(index=index, stage=stage):
                invalid = deepcopy(ready)
                invalid["conditions"][index]["instructions"][stage] = None
                self.assertFalse(self.validator.is_valid(invalid))


if __name__ == "__main__":
    unittest.main()
