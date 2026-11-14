"""Snapshot provenance, quotas and isolation without models or network calls."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from agent_memory_eval.contracts.core import ContractError, MemoryFault, MemoryPolicy, RunContext, new_id
from agent_memory_eval.contracts.validation import validate_manifest, validate_preload
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.recording.journal import Journal, read_experiment
from agent_memory_eval.recording.provenance import OfflineGateway, SourceRegistry


class PreloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root, "snapshot-test", contract_version="1.2")
        self.addCleanup(self.journal.close)
        self.memory = MemoryService(self.journal)
        source, bank = self.start(MemoryPolicy(True, "during_task", True, 2), stage="collection")
        for title, body in (("조건 🧠", '한글\n"조건"을 확인한다.'), ("두 번째", "PUBLIC_MEMORY_ONLY")):
            self.memory.execute(source, bank, new_id("op"), "memory_store", dict(title=title, body=body), call_id=new_id("call"))
        self.snapshot = self.memory.freeze(bank)
        self.memory.seal(bank)
        self.journal.append([source.at("seal").event("run.ended", dict(termination_reason="complete",
            grading_status="not_available", end_snapshot_id=self.snapshot))])
        self.ctx, self.bank = self.start(MemoryPolicy(True, "none", False, 2, read_mode="preload"), self.snapshot)

    def start(self, policy, parent=None, stage="probe"):
        ctx = RunContext("snapshot-test", new_id("r"), new_id("s"), "test", stage, "solve")
        bank = self.memory.create_bank(ctx.run_id, policy, parent)
        self.journal.append([ctx.at("prepare").event("run.started", dict(bank_id=bank, start_snapshot_id=parent,
            fresh_session=True, source_run_ids=[], task_ref=self.journal.blobs.put(dict(role="user", content="CURRENT_TASK"))))])
        return ctx, bank

    def registry(self):
        event = self.memory.preload(self.ctx, self.bank)
        registry = SourceRegistry(self.journal, self.ctx)
        source = registry.add("memory_snapshot", event["event_id"])
        template = registry.template(dict(model="synthetic-offline", system=dict(role="system", content="POLICY")))
        return event, registry, template, [(template, "/system"), (source, "")]

    def test_full_unicode_snapshot_is_included_and_reexposed_without_fake_calls(self):
        event, registry, template, messages = self.registry()
        charged = self.journal.state["return_bytes"][self.ctx.run_id]
        class Transport:
            offline = True
            def respond(inner, request):
                self.assertIn("PUBLIC_MEMORY_ONLY", request["messages"][1]["content"])
                self.assertNotIn("public_context_prefix", request["messages"][1]["content"])
                return dict(choices=[dict(message=dict(role="assistant", content="Observed."))])
        for _ in range(2):
            OfflineGateway(self.journal).call(self.ctx, registry, "worker", template, messages, Transport(), max_requests=2)
        exposures = [e for e in self.journal.events.values() if e["type"] == "memory.input_included"]
        self.assertEqual(len(exposures), 8)
        self.assertTrue(all(e["schema_version"] == "1.2" and e["payload"]["source"] ==
            dict(kind="memory_snapshot", preloaded_event_id=event["event_id"]) for e in exposures))
        self.assertFalse(any(e["type"] == "memory.returned" and e["run_id"] == self.ctx.run_id for e in self.journal.events.values()))
        self.assertEqual(self.memory.preload(self.ctx, self.bank), event)
        self.assertEqual(self.journal.state["return_bytes"][self.ctx.run_id], charged)
        self.assertEqual(read_experiment(self.root)[2], "snapshot-test")

    def test_quota_failure_does_not_partially_deliver_or_charge(self):
        ctx, bank = self.start(MemoryPolicy(True, "none", False, 2, read_mode="preload", max_return_bytes=1), self.snapshot)
        before = self.journal.path.read_bytes()
        state = deepcopy(self.journal.state)
        with self.assertRaisesRegex(MemoryFault, "QUOTA_EXCEEDED"):
            self.memory.preload(ctx, bank)
        self.assertEqual(self.journal.path.read_bytes(), before)
        self.assertEqual(self.journal.state, state)

    def test_foreign_bank_and_non_preload_policy_cannot_expose_snapshot(self):
        other, other_bank = self.start(MemoryPolicy(True, "none", False, 2), self.snapshot)
        with self.assertRaisesRegex(MemoryFault, "ACCESS_DENIED"):
            self.memory.preload(other, self.bank)
        with self.assertRaisesRegex(MemoryFault, "POLICY_DENIED"):
            self.memory.preload(other, other_bank)
        with self.assertRaises(ContractError):
            self.memory.preload(self.ctx.at("post_task_write"), self.bank)

    def test_preload_cannot_be_late_or_used_as_a_simulator_source(self):
        event, registry, template, messages = self.registry()
        with self.assertRaisesRegex(ValueError, "role"):
            registry.prepare(self.ctx, "user_simulator", template, messages)
        other, bank = self.start(MemoryPolicy(True, "none", False, 2, read_mode="preload"), self.snapshot)
        r = SourceRegistry(self.journal, other)
        t = r.template(dict(model="synthetic-offline", system=dict(role="system", content="POLICY")))
        OfflineGateway(self.journal).call(other, r, "worker", t, [(t, "/system")], SimpleNamespace(offline=True), max_requests=1, cancel=True)
        with self.assertRaisesRegex(ContractError, "precede"):
            self.memory.preload(other, bank)

    def test_altered_snapshot_rendering_and_omitted_spans_are_rejected(self):
        event, registry, template, messages = self.registry()
        bad = deepcopy(event)
        bad["payload"]["public_message_ref"] = self.journal.blobs.put(dict(role="user", content='{"items":[]}'))
        with self.assertRaisesRegex(ValueError, "content"):
            validate_preload(self.root, bad, self.journal.events, self.journal.state)
        _, manifest, request = registry.prepare(self.ctx, "worker", template, messages)
        manifest["memory_spans"].pop()
        with self.assertRaisesRegex(ValueError, "span manifest"):
            validate_manifest(self.root, manifest, request, registry.sources, self.journal.events, registry.approved_templates)

    def test_start_snapshot_and_version_refs_are_checked_against_immutable_state(self):
        event, _, _, _ = self.registry()
        for change in ("snapshot", "version"):
            state = deepcopy(self.journal.state)
            altered = deepcopy(event)
            if change == "snapshot":
                altered["payload"]["snapshot_id"] = "foreign-snapshot"
            else:
                key = altered["payload"]["versions"][0]["version_id"]
                state["versions"][key] = self.journal.blobs.put(dict(body="PRIVATE_ORACLE"))
            with self.assertRaises(ValueError):
                validate_preload(self.root, altered, self.journal.events, state)

    def test_query_tools_are_an_explicit_independent_preload_choice(self):
        self.assertEqual(MemoryPolicy(True, "none", False, 2, read_mode="preload").tools("solve"), [])
        self.assertEqual(MemoryPolicy(True, "none", False, 2, read_mode="preload_and_on_demand").tools("solve"),
                         ["memory_list", "memory_get"])

    def test_old_journal_contract_is_preserved(self):
        with TemporaryDirectory() as path, Journal(path, "old") as old:
            with self.assertRaisesRegex(ContractError, "v1.2"):
                MemoryService(old).preload(self.ctx, self.bank)


if __name__ == "__main__":
    unittest.main()
