"""Pilot allocation, wire controls and missingness; HTTP is always mocked here."""

from collections import Counter
from copy import deepcopy
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.tau2_autonomy import audit_pilot
from agent_memory_eval.contracts.core import ContractError, canonical, read_json
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner.tau2_autonomy import compile_pilot, make_schedule, prepare_pilot, run_pilot
from agent_memory_eval.runner.tau2_memory import Tau2MemoryRunner
from agent_memory_eval.contracts.core import MemoryPolicy, digest, new_id
from agent_memory_eval.memory.imports import load_source
from agent_memory_eval.models.openrouter import SpendGuard
import shutil
from test_openrouter import Reply, response
from test_tau2_memory import call, Conversation


ROOT = Path(__file__).resolve().parents[1]
AVAILABLE = importlib.util.find_spec("tau2") is not None and (ROOT/"data/tau2-bench/.git").is_dir()
PROFILE = ROOT/"protocols/pilots/tau2-autonomous-memory-v1.json"


class PilotCompileTests(unittest.TestCase):
    def test_frozen_source_plan_allocates_only_nine_evaluations(self):
        profile = read_json((PROFILE.parent/"tau2-autonomous-memory-v1.1.json").read_bytes())
        plan = compile_pilot(profile)
        schedule = make_schedule(plan)
        self.assertEqual(plan["source_mode"],"frozen_external")
        self.assertEqual(len(schedule),9)
        self.assertTrue(all(row["stage"] == "probe" for row in schedule))
        self.assertEqual(set(plan["source_texts"]),{"connection","probe_memory"})

    def test_allocation_and_controls_are_fixed_without_model_calls(self):
        plan = compile_pilot(read_json(PROFILE.read_bytes()))
        self.assertEqual(plan["policies"]["empty_bank"],plan["policies"]["memory_bank"])
        self.assertFalse(plan["policies"]["no_memory"]["enabled"])
        self.assertEqual(plan["policies"]["no_memory"]["read_mode"],"none")
        schedule = make_schedule(plan)
        self.assertEqual(len({row["run_id"] for row in schedule}),10)
        positions = Counter((row["condition"],row["position"]) for row in schedule[1:])
        self.assertEqual(set(positions.values()),{1})
        self.assertEqual(len(positions),9)
        repeated = make_schedule(compile_pilot(read_json(PROFILE.read_bytes())))
        self.assertEqual([(r["condition"],r["block_id"]) for r in schedule],[(r["condition"],r["block_id"]) for r in repeated])
        self.assertNotEqual(schedule[0]["run_id"],repeated[0]["run_id"])

    def test_changed_assets_and_impossible_budget_fail_before_native_preparation(self):
        for changed in ("asset","budget"):
            profile = read_json(PROFILE.read_bytes())
            if changed == "asset":
                profile["instructions"]["probe_memory"]["sha256"] = "0"*64
            else:
                profile["guards"]["max_model_requests"] = 1
            with patch("agent_memory_eval.runner.tau2_autonomy.prepare_native") as native:
                with self.assertRaises(ContractError):
                    prepare_pilot(profile,ROOT/"data/tau2-bench")
                native.assert_not_called()


@unittest.skipUnless(AVAILABLE,"Install tau2 extra and the pinned checkout for native tests")
class NativeAutonomyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = read_json(PROFILE.read_bytes())
        connection = compile_pilot(cls.profile)["connection"]
        catalog = dict(data=[dict(id=connection[role]["model_id"],canonical_slug=connection[role]["model_id"],
            supported_parameters=list(connection[role]["parameters"]),pricing=dict(prompt="0.000001",completion="0.000001"))
            for role in ("worker","user_simulator")])
        def endpoints(model_id):
            user = model_id.startswith("deepseek/")
            return dict(data=dict(id=model_id,endpoints=[dict(tag="fireworks" if user else "openai",
                provider_name="Fireworks" if user else "OpenAI",status=0,
                supported_parameters=["reasoning","max_tokens","temperature","tools","tool_choice"] + ([] if user else ["seed"]),
                pricing=dict(prompt="0.000001",completion="0.000001"))]))
        with patch("agent_memory_eval.adapters.tau2.model_catalog",return_value=catalog), \
                patch("agent_memory_eval.adapters.tau2.model_endpoints",side_effect=endpoints):
            cls.prepared = prepare_pilot(cls.profile,ROOT/"data/tau2-bench")

    def setUp(self):
        environment = patch.dict("os.environ",OPENROUTER_API_KEY="pilot-test-key")
        environment.start()
        self.addCleanup(environment.stop)

    @staticmethod
    def worker(wire):
        names = [tool["function"]["name"] for tool in wire.get("tools",[])]
        previous = [call["function"]["name"] for message in wire["messages"] for call in message.get("tool_calls") or []]
        if names == ["memory_store"] and "memory_store" not in previous:
            calls = [call("memory_store",dict(title="fixed memory",body="PUBLIC_EXPERIENCE_ONLY_TOKEN"),"source-memory")]
        elif "memory_list" in names and "memory_list" not in previous:
            calls = [call("memory_list",{},"optional-list")]
        elif ("memory_get" in names and "memory_get" not in previous
              and wire["messages"][-1].get("tool_call_id") == "optional-list"
              and (listing:=read_json(wire["messages"][-1]["content"].encode())).get("items")):
            calls = [call("memory_get",dict(id=listing["items"][0]["id"]),"optional-get")]
        elif "list_all_airports" in names and "list_all_airports" not in previous:
            calls = [call("list_all_airports",{},"business")]
        else:
            return dict(role="assistant",content="Please provide your details.")
        return dict(role="assistant",content=None,tool_calls=calls)

    def test_three_controls_share_wire_path_and_fixed_bank_with_readonly_audit(self):
        conversation = Conversation(self.worker)
        with TemporaryDirectory() as folder,Journal(folder,"autonomy-mock") as journal:
            summary = run_pilot(journal,deepcopy(self.profile),self.prepared,live=True,opener=conversation)
            self.assertEqual(summary["status"],"completed",summary)
            self.assertEqual(summary["source_eligibility"],"eligible")
            self.assertEqual(len(summary["runs"]),9)
            self.assertEqual(summary["conditions"]["no_memory"]["runs_with_list_calls"],0)
            self.assertEqual(summary["conditions"]["empty_bank"]["runs_with_list_calls"],3)
            self.assertEqual(summary["conditions"]["empty_bank"]["runs_with_body_input"],0)
            self.assertEqual(summary["conditions"]["memory_bank"]["runs_with_body_input"],3)
            self.assertEqual(summary["baseline_repeat_variation"]["pair_count"],3)
            self.assertEqual(summary["baseline_repeat_variation"]["outcome_disagreements"],0)
            self.assertTrue(all(pair["outcome"] == "both_success" for pair in summary["blocked_comparisons"]))
            self.assertEqual(len({row["session_id"] for row in [summary["collection"],*summary["runs"]]}),10)
            worker_inputs = {}
            for event in journal.events.values():
                if event["type"] == "model.request_started" and event["stage"] == "probe" and event["payload"]["actor_role"] == "worker":
                    wire = journal.blobs.get(event["payload"]["request_ref"])
                    worker_inputs.setdefault(event["condition_id"],[]).append(wire)
            left,right = worker_inputs["empty_bank"][0],worker_inputs["memory_bank"][0]
            self.assertEqual(left["messages"][0],right["messages"][0])
            self.assertEqual(left["tools"],right["tools"])
            self.assertTrue(all("memory_policy" not in wire["messages"][0]["content"] and not any(t["function"]["name"].startswith("memory_") for t in wire["tools"]) for wire in worker_inputs["no_memory"]))
            self.assertNotIn("PUBLIC_EXPERIENCE_ONLY_TOKEN",canonical(worker_inputs["no_memory"]+worker_inputs["empty_bank"]).decode())
            source = journal.state["autonomy_pilot"]["source_snapshot"]
            filled = [journal.state["banks"][a["bank_id"]] for a in journal.state["assignments"].values() if a["condition_id"] == "memory_bank"]
            self.assertTrue(all(bank["parent_snapshot"] == source and bank["heads"] == journal.state["snapshots"][source]["heads"] for bank in filled))
            path = Path(folder)/"pilot-summary.json"
            path.write_bytes(canonical(summary))
            original = (Path(folder)/"log.jsonl").read_bytes()
            self.assertEqual(audit_pilot(folder)["audit_status"],"audit_passed")
            self.assertEqual((Path(folder)/"log.jsonl").read_bytes(),original)
            summary["conditions"]["memory_bank"]["successes"] += 1
            path.write_bytes(canonical(summary))
            with self.assertRaisesRegex(ContractError,"summary differs"):
                audit_pilot(folder)

    def test_empty_written_source_skips_every_allocated_probe_without_faking_failures(self):
        def worker(wire):
            return dict(role="assistant",content="Nothing worth retaining.")
        with TemporaryDirectory() as folder,Journal(folder,"empty-source") as journal:
            summary = run_pilot(journal,deepcopy(self.profile),self.prepared,live=True,opener=Conversation(worker))
            self.assertEqual(summary["status"],"incomplete")
            self.assertEqual(summary["source_eligibility"],"source_empty")
            self.assertTrue(all(row["status"] == "skipped" and row["official_success"] is None for row in summary["runs"]))
            self.assertEqual(len(journal.state["skipped_runs"]),9)
            self.assertTrue(all(value["graded"] == 0 and value["success_rate"] is None for value in summary["conditions"].values()))

    def test_frozen_source_preserves_versions_and_archive_without_old_dialogue_in_inputs(self):
        with TemporaryDirectory(dir=ROOT/"runs") as original_folder,TemporaryDirectory() as destination:
            source_path = Path(original_folder)
            def source_worker(wire):
                answer = self.worker(wire)
                if answer.get("content"):
                    answer["content"] += " PRIVATE_SOURCE_TRANSCRIPT_TOKEN"
                return answer
            with Journal(source_path,"frozen-source-fixture") as source_journal:
                runner = Tau2MemoryRunner(source_journal,self.prepared[0]["connection"],self.prepared[1],
                    SpendGuard(max_calls=24,max_usd=0.25,max_seconds=180),opener=Conversation(source_worker))
                result = runner.episode(run_id=new_id("r"),stage="collection",policy=MemoryPolicy(True,"post_task",False,4,read_mode="none"),
                    solve_instruction="Write after the task.",post_instruction="Save reusable public experience.")
                self.assertEqual(result["status"],"completed",result)
                original_snapshot = deepcopy(source_journal.state["snapshots"][result["snapshot_id"]])
            profile = read_json((PROFILE.parent/"tau2-autonomous-memory-v1.1.json").read_bytes())
            profile["frozen_source"] = dict(path=str(source_path.relative_to(ROOT)),
                log_sha256=digest((source_path/"log.jsonl").read_bytes()),experiment_id="frozen-source-fixture",
                snapshot_id=result["snapshot_id"],source_run_id=result["run_id"])
            bad = deepcopy(profile)
            bad["frozen_source"]["log_sha256"] = "0"*64
            with patch("agent_memory_eval.runner.tau2_autonomy.prepare_native") as native:
                with self.assertRaises(ContractError):
                    prepare_pilot(bad,ROOT/"data/tau2-bench")
                native.assert_not_called()
            plan = compile_pilot(profile)
            external = load_source(profile["frozen_source"])
            conversation = Conversation(self.worker)
            with Journal(destination,"imported-pilot") as journal:
                summary = run_pilot(journal,profile,(plan,self.prepared[1],external),live=True,opener=conversation)
                self.assertEqual(summary["status"],"completed",summary)
                self.assertIsNone(summary["collection"])
                self.assertEqual(len(summary["runs"]),9)
                local = journal.state["snapshots"][journal.state["autonomy_pilot"]["source_snapshot"]]
                self.assertEqual(local["heads"],original_snapshot["heads"])
                self.assertEqual(local["order"],original_snapshot["order"])
                self.assertFalse(any(event["type"] == "memory.stored" for event in journal.events.values()))
                self.assertNotIn("PRIVATE_SOURCE_TRANSCRIPT_TOKEN",canonical(conversation.wires).decode())
                self.assertGreater(summary["conditions"]["memory_bank"]["runs_with_body_input"],0)
                (Path(destination)/"pilot-summary.json").write_bytes(canonical(summary))
                shutil.rmtree(source_path)
                self.assertEqual(audit_pilot(destination)["audit_status"],"audit_passed")

    def test_unknown_cost_stops_before_the_remaining_paid_allocation(self):
        def opener(request,timeout):
            wire = read_json(request.data)
            return Reply(dict(response(None),model=wire["model"],provider="Fireworks"))
        with TemporaryDirectory() as folder,Journal(folder,"unknown-pilot-cost") as journal:
            summary = run_pilot(journal,deepcopy(self.profile),self.prepared,live=True,opener=opener)
            self.assertEqual(summary["http_calls"],1)
            self.assertTrue(summary["unknown_cost"])
            self.assertEqual(summary["status"],"incomplete")
            self.assertEqual(len(journal.state["skipped_runs"]),9)
            self.assertTrue(all(row["official_success"] is None for row in summary["runs"]))

    def test_unexpected_transport_error_also_stops_the_remaining_allocation(self):
        def worker(wire):
            names = [tool["function"]["name"] for tool in wire.get("tools",[])]
            if "memory_list" in names and wire["messages"][-1].get("tool_call_id") == "optional-list":
                raise RuntimeError("Injected unexpected transport interruption")
            return self.worker(wire)
        with TemporaryDirectory() as folder,Journal(folder,"unexpected-pilot-error") as journal:
            summary = run_pilot(journal,deepcopy(self.profile),self.prepared,live=True,opener=Conversation(worker))
            self.assertTrue(summary["unknown_cost"])
            self.assertEqual(summary["status"],"incomplete")
            self.assertEqual(len(journal.scan["starts"]),2)
            self.assertEqual(len(journal.state["skipped_runs"]),8)
            self.assertTrue(all(reason == "unknown_cost" for reason in journal.state["skipped_runs"].values()))


if __name__ == "__main__":
    unittest.main()
