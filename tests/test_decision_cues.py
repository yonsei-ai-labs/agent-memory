"""Exercise cue attribution at the request boundary, including missing deliveries and public alternatives."""

import json
from copy import deepcopy
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from agent_memory_eval.analysis.decision_cues import trace_decision_cue, writer_material
from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.journal import read_experiment
from test_snapshot_replay import SnapshotReplayFixtureMixin, body, tool


class DecisionCueTests(SnapshotReplayFixtureMixin, unittest.TestCase):
    schema = Draft202012Validator(json.loads((ROOT / "schemas/decision-cue-v1.schema.json").read_bytes()))

    def request(self, fixture, *args, **kwargs):
        run = fixture.replay(*args, **kwargs)["run_id"]
        return next(e["payload"]["request_id"] for e in fixture.journal.events.values()
                    if e["type"] == "model.request_started" and e["run_id"] == run)

    def test_public_alternatives_stored_version_and_real_action(self):
        f = self.fixture()
        reply = body(dict(role="assistant", content=None, tool_calls=[tool("book_reservation", {"baggage": "baggage"})]))
        req = self.request(f, f.client(reply))
        result = trace_decision_cue(f.target, req, "baggage")
        self.schema.validate(result)
        author, = result["writers"]
        self.assertEqual(author["source"]["run"], "source-run")
        self.assertTrue(author["source"]["matches"])
        item, = author["items"]
        self.assertTrue(item["assigned"])
        self.assertEqual(item["matches"][0]["pointer"], "/body")
        reader = result["reader"]
        self.assertEqual(reader["assigned_versions"], [dict(item=item["item"], version=item["version"])])
        shown, = reader["exposed_matches"]
        self.assertEqual(shown["source"]["kind"], "memory_snapshot")
        self.assertLess(shown["clause_char_start"], shown["clause_char_end"])
        self.assertTrue(reader["other_input_matches"])
        self.assertFalse(any(h["source"] in {"memory_return", "memory_snapshot"} for h in reader["other_input_matches"]))
        self.assertEqual(reader["action"]["calls"], ["book_reservation"])
        self.assertEqual(reader["action"]["matches"][0]["pointer"], "/tool_calls/0/function/arguments")
        self.assertEqual(reader["reads_through_response"], [])

    def test_exact_case_and_private_reply_fields(self):
        f = self.fixture()
        req = self.request(f)
        author, = trace_decision_cue(f.target, req, "reusable lesson")["writers"]
        self.assertEqual(author["source"]["matches"], [])
        self.assertEqual([h["pointer"] for h in author["guidance_matches"]], ["/instruction"])
        for term in ("BAGGAGE", "PRIVATE_NEW_REASONING"):
            with self.subTest(term=term):
                result = trace_decision_cue(f.target, req, term)
                author, = result["writers"]
                self.assertEqual(author["source"]["matches"], [])
                self.assertEqual(author["items"][0]["matches"], [])
                reader = result["reader"]
                self.assertEqual(reader["exposed_matches"], [])
                self.assertEqual(reader["other_input_matches"], [])
                self.assertEqual(reader["action"]["matches"], [])

    def test_writer_provenance_survives_reordered_public_inputs(self):
        f = self.fixture()
        scan, state, _ = read_experiment(f.target)
        blobs = f.journal.blobs
        event = next(e for e in scan["events"].values() if e["type"] == "model.request_started"
                     and e["payload"]["actor_role"] == "writer")
        expected = writer_material(f.target, blobs, scan, state, event)
        changed = deepcopy(event)
        req = changed["payload"]
        wire = blobs.get(req["request_ref"])
        manifest = blobs.get(req["input_manifest_ref"])
        wire["messages"][1], wire["messages"][2] = wire["messages"][2], wire["messages"][1]
        manifest["request_ref"] = req["request_ref"] = blobs.put(wire)
        swap = {"/messages/1": "/messages/2", "/messages/2": "/messages/1"}
        for binding in manifest["bindings"]:
            binding["pointer"] = swap.get(binding["pointer"], binding["pointer"])
        manifest["bindings"].reverse()
        req["input_manifest_ref"] = blobs.put(manifest)
        self.assertEqual(writer_material(f.target, blobs, scan, state, changed), expected)
        # Changing content without changing its producer must fail.
        wire["messages"][2]["content"] = "{}"
        manifest["request_ref"] = req["request_ref"] = blobs.put(wire)
        req["input_manifest_ref"] = blobs.put(manifest)
        with self.assertRaises(ValueError):
            writer_material(f.target, blobs, scan, state, changed)
        with self.assertRaises(ContractError):
            trace_decision_cue(f.target, req, " ")

    def test_empty_bank_and_unrelated_writer(self):
        f = self.fixture(empty=True)
        req = self.request(f)
        result = trace_decision_cue(f.target, req, "bags")
        self.schema.validate(result)
        self.assertEqual(result["writers"][0]["items"], [])
        self.assertEqual(result["reader"]["visible_fields"], [])
        self.assertTrue(result["reader"]["action"]["matches"])
        with self.assertRaisesRegex(ContractError, "unrelated"):
            trace_decision_cue(f.target, req, "bags", writer_folders=[self.fixture(empty=True).target])

    def test_first_pass_can_be_examined_without_a_writer(self):
        f = self.fixture()
        scan, _, _ = read_experiment(f.source)
        req = next(e["payload"]["request_id"] for e in scan["events"].values() if e["type"] == "model.request_started")
        result = trace_decision_cue(f.source, req, "bags")
        self.schema.validate(result)
        self.assertEqual(result["writers"], [])
        self.assertIsNone(result["reader"]["snapshot"])
        self.assertEqual(result["reader"]["assigned_versions"], [])
        self.assertEqual(result["reader"]["exposed_matches"], [])
        self.assertTrue(result["reader"]["other_input_matches"])
        with self.assertRaises(ContractError):
            trace_decision_cue(f.source, req, "bags", writer_folders=[f.target])

    def test_not_submitted_and_truncated_are_distinct(self):
        f = self.fixture()
        req = self.request(f, execution_limits=ExecutionLimits(1, 0, 0, 1))
        result = trace_decision_cue(f.target, req, "baggage")
        self.schema.validate(result)
        reader = result["reader"]
        self.assertEqual(reader["delivery"], "not_submitted")
        for key in ("visible_fields", "exposed_matches", "other_input_matches", "action"):
            self.assertIsNone(reader[key])
        raw = body()
        raw["choices"][0]["finish_reason"] = "length"
        req = self.request(f, f.client(raw))
        result = trace_decision_cue(f.target, req, "baggage")
        self.schema.validate(result)
        self.assertTrue(result["reader"]["exposed_matches"])
        self.assertEqual(result["reader"]["action_status"], "truncated")
        self.assertIsNone(result["reader"]["action"])

    def test_published_cases_reproduce_when_native_journals_are_present(self):
        cases = json.loads((ROOT / "artifacts/analyses/20261126-decision-cues.json").read_bytes())
        for case in cases:
            record = case["evidence"]
            self.schema.validate(record)
            self.assertEqual(record["reader"]["request_id"], case["request"])
            self.assertEqual(record["literal"], case["literal"])
        examples = {c["case"]: c["evidence"] for c in cases}
        direct = examples["bfcl_on_demand"]["reader"]
        self.assertTrue(any(h["source"] == "task_initial" for h in direct["other_input_matches"]))
        self.assertEqual(direct["exposed_matches"], [])
        self.assertTrue(direct["action"]["matches"])
        bank = examples["banking_first_search"]
        self.assertEqual(bank["reader"]["other_input_matches"], [])
        self.assertTrue(bank["reader"]["exposed_matches"] and bank["reader"]["action"]["matches"])
        self.assertIsNone(bank["outcome"]["success"])
        before, after = (examples[k]["reader"] for k in ("retail_get", "retail_business"))
        self.assertEqual(before["reads_through_response"][-1]["calls"][0]["returned"]["status"], "ok")
        self.assertEqual(before["exposed_matches"], [])
        self.assertEqual([m["source"]["kind"] for m in after["exposed_matches"]], ["memory_return"])
        self.assertTrue(any(h["source"] == "business_result" for h in after["other_input_matches"]))
        if any(not (ROOT / folder / "log.jsonl").is_file() for c in cases for folder in [c["worker"], *c["writers"]]):
            self.skipTest("Native journals are archived outside Git")
        for case in cases:
            with self.subTest(case=case["case"]):
                result = trace_decision_cue(ROOT / case["worker"], case["request"], case["literal"],
                                            writer_folders=[ROOT / p for p in case["writers"]])
                self.assertEqual(result, case["evidence"])
        pooled = next(c for c in cases if len(c["writers"]) > 1)
        participating = [writer for writer in pooled["evidence"]["writers"] if writer["items"]]
        self.assertGreater(len(participating), 1)
        missing = next(p for p in pooled["writers"] if Path(p).name == participating[0]["run"])
        with self.assertRaisesRegex(ContractError, "Every assigned version"):
            trace_decision_cue(ROOT / pooled["worker"], pooled["request"], pooled["literal"],
                               writer_folders=[ROOT / p for p in pooled["writers"] if p != missing])
