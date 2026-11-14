"""Native tau2 memory behavior with mock HTTP only; no paid calls."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import MemoryPolicy, canonical, new_id
from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.analysis.tau2_memory import audit_memory_check
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.models.openrouter import SpendGuard
from agent_memory_eval.runner.tau2_memory import (Tau2MemoryRunner, fresh_orchestrator,
    prepare_memory_check, run_memory_check)
from test_openrouter import Reply, response
from urllib.error import URLError


ROOT = Path(__file__).resolve().parents[1]
AVAILABLE = importlib.util.find_spec("tau2") is not None and (ROOT/"data/tau2-bench/.git").is_dir()


def call(name, arguments, identity):
    return dict(id=identity, type="function", function=dict(name=name,
        arguments=json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))))


class Conversation:
    def __init__(self, worker):
        self.worker = worker
        self.wires, self.user_calls = [], 0

    def __call__(self, request, timeout):
        wire = json.loads(request.data)
        self.wires.append(wire)
        body = response()
        body.update(id="native-memory-" + str(len(self.wires)), model=wire["model"])
        if wire["model"].startswith("deepseek/"):
            body["provider"] = "Fireworks"
            self.user_calls += 1
            answer = dict(role="assistant", content="Hello, please help." if self.user_calls % 2 else "###STOP###")
        else:
            answer = self.worker(wire)
        body["choices"][0].update(message=answer, finish_reason="tool_calls" if answer.get("tool_calls") else "stop")
        return Reply(body)


@unittest.skipUnless(AVAILABLE, "Install the tau2 extra and pinned checkout for native tests")
class Tau2MemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = json.loads((ROOT/"protocols/checks/openrouter-tau2-memory-smoke.json").read_text())
        connection = json.loads((ROOT/"protocols/checks/openrouter-tau2-smoke.json").read_text())
        catalog = dict(data=[dict(id=connection[role]["model_id"],canonical_slug=connection[role]["model_id"],
            supported_parameters=list(connection[role]["parameters"]),pricing=dict(prompt="0.000001",completion="0.000001"))
            for role in ("worker","user_simulator")])
        def endpoints(model_id):
            deepseek = model_id.startswith("deepseek/")
            return dict(data=dict(id=model_id,endpoints=[dict(tag="fireworks" if deepseek else "openai",
                provider_name="Fireworks" if deepseek else "OpenAI",status=0,
                supported_parameters=["reasoning","max_tokens","temperature","tools","tool_choice"] + ([] if deepseek else ["seed"]),
                pricing=dict(prompt="0.000001",completion="0.000001"))]))
        with patch("agent_memory_eval.adapters.tau2.model_catalog",return_value=catalog), \
                patch("agent_memory_eval.adapters.tau2.model_endpoints",side_effect=endpoints):
            cls.prepared = prepare_memory_check(cls.profile,ROOT/"data/tau2-bench")

    def setUp(self):
        environment = patch.dict("os.environ", OPENROUTER_API_KEY="native-memory-test-key")
        environment.start()
        self.addCleanup(environment.stop)

    def test_post_write_then_fresh_probe_with_title_body_and_self_authored_spans(self):
        counts = dict(collection=0, writer=0, probe=0)
        def worker(wire):
            names = [tool["function"]["name"] for tool in wire.get("tools", [])]
            phase = "probe" if "memory_list" in names else "writer" if names == ["memory_store"] else "collection"
            counts[phase] += 1
            index = counts[phase]
            if phase == "writer" and index == 1:
                calls = [call("memory_store",dict(title="기억 제목 표식",body='조건: 공개 관측\n본문 "표식"'),"store-one")]
            elif phase == "probe" and index == 1:
                calls = [call("memory_list",{},"list-one")]
            elif phase == "probe" and index == 2:
                listing = json.loads(wire["messages"][-1]["content"])
                calls = [call("memory_get",dict(id=listing["items"][0]["id"]),"get-one")]
            elif (phase == "collection" and index == 1) or (phase == "probe" and index == 3):
                calls = [call("list_all_airports",{},"airports-" + phase)]
            else:
                return dict(role="assistant",content="Please provide your details.")
            return dict(role="assistant",content=None,tool_calls=calls)
        conversation = Conversation(worker)
        environments = []
        def fresh(prepared):
            orch = fresh_orchestrator(prepared)
            environments.append(orch)
            return orch
        with TemporaryDirectory() as folder, Journal(folder,"memory-roundtrip") as journal, \
                patch("agent_memory_eval.runner.tau2_memory.fresh_orchestrator",side_effect=fresh):
            summary = run_memory_check(journal,deepcopy(self.profile),self.prepared,live=True,opener=conversation)
            self.assertEqual(summary["status"],"completed",summary)
            self.assertTrue(summary["memory_roundtrip"])
            self.assertEqual(summary["collection"]["native_steps"],summary["probe"]["native_steps"])
            self.assertEqual(summary["collection"]["native_tool_errors"],0)
            self.assertEqual(summary["probe"]["native_tool_errors"],0)
            self.assertEqual(summary["http_calls"],12)
            for attr in ("agent","user","environment","agent_state","user_state"):
                self.assertIsNot(getattr(environments[0],attr),getattr(environments[1],attr))
            self.assertIsNot(environments[0].environment.tools.db,environments[1].environment.tools.db)
            second_users = len(environments[1].environment.tools.db.users)
            environments[0].environment.tools.db.users.clear()
            self.assertEqual(len(environments[1].environment.tools.db.users),second_users)
            self.assertEqual(environments[0].agent_state.messages[0].content,environments[1].agent_state.messages[0].content)
            starts = [e for e in journal.events.values() if e["type"] == "run.started"]
            self.assertNotEqual(starts[0]["session_id"],starts[1]["session_id"])
            self.assertEqual(starts[1]["payload"]["source_run_ids"],[starts[0]["run_id"]])
            first_bank = journal.state["banks"][summary["collection"]["bank_id"]]
            probe_bank = journal.state["banks"][summary["probe"]["bank_id"]]
            self.assertEqual(first_bank["heads"],probe_bank["start_heads"])
            self.assertNotEqual(summary["collection"]["bank_id"],summary["probe"]["bank_id"])
            included = [e["payload"] for e in journal.events.values() if e["type"] == "memory.input_included"]
            self.assertTrue(any(p["source"]["kind"] == "self_authored" for p in included))
            self.assertTrue(any(p["source"]["kind"] == "memory_return" and p["field"] == "title" for p in included))
            self.assertTrue(any(p["source"]["kind"] == "memory_return" and p["field"] == "body" for p in included))
            for wire in conversation.wires:
                if wire["model"].startswith("deepseek/"):
                    self.assertNotIn("memory_",canonical(wire).decode())
                    self.assertNotIn("기억 제목 표식",canonical(wire).decode())
                else:
                    self.assertNotIn(environments[0].user.system_prompt,canonical(wire).decode())
            for episode in (summary["collection"],summary["probe"]):
                report = journal.blobs.get(episode["report_ref"])
                self.assertFalse(any(call["name"].startswith("memory_") for m in report["messages"] for call in m.get("tool_calls") or []))
            self.assertTrue(all(event["record_kind"] == "observed" for event in journal.events.values()))
            path = Path(folder)/"memory-summary.json"
            path.write_bytes(canonical(summary))
            original = (Path(folder)/"log.jsonl").read_bytes()
            audit = audit_memory_check(folder)
            self.assertEqual(audit["requests"],12)
            self.assertGreater(audit["retrieved_input_spans"],0)
            self.assertEqual((Path(folder)/"log.jsonl").read_bytes(),original)
            changed = deepcopy(summary)
            changed["collection"]["official_success"] = not changed["collection"]["official_success"]
            path.write_bytes(canonical(changed))
            with self.assertRaisesRegex(ContractError,"outcome differs"):
                audit_memory_check(folder)

    def episode(self, worker, policy, *, configure=None):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        journal = Journal(folder.name,new_id("native-memory"))
        self.addCleanup(journal.close)
        conversation = Conversation(worker)
        runner = Tau2MemoryRunner(journal,self.prepared[0],self.prepared[2],
            SpendGuard(max_calls=24,max_usd=0.25,max_seconds=180),opener=conversation)
        original = fresh_orchestrator
        def fresh(prepared):
            orch = original(prepared)
            if configure:
                configure(orch)
            return orch
        with patch("agent_memory_eval.runner.tau2_memory.fresh_orchestrator",side_effect=fresh):
            report = runner.episode(run_id=new_id("r"),stage="collection",policy=policy,
                solve_instruction="Use memory only as allowed.",post_instruction="Write memory; then finish.")
        return journal,runner,report,conversation

    def test_mixed_batch_runs_memory_business_memory_in_original_order(self):
        count = 0
        def worker(wire):
            nonlocal count
            count += 1
            if count == 1:
                return dict(role="assistant",content=None,tool_calls=[
                    call("memory_store",dict(title="before",body="before business"),"before"),
                    call("list_all_airports",{},"business"),
                    call("memory_store",dict(title="after",body="after business"),"after")])
            return dict(role="assistant",content="Please provide details.")
        journal,_,report,conversation = self.episode(worker,MemoryPolicy(True,"during_task",True,4))
        self.assertEqual(report["status"],"completed",report)
        relevant = [e for e in journal.events.values() if e["type"] == "memory.stored"
                    or e["type"] == "business.transition" and e["payload"].get("action") == "list_all_airports"]
        self.assertEqual([e["type"] for e in relevant],["memory.stored","business.transition","memory.stored"])
        worker_wires = [w for w in conversation.wires if not w["model"].startswith("deepseek/")]
        self.assertEqual([m["tool_call_id"] for m in worker_wires[1]["messages"] if m["role"] == "tool"],
                         ["before","business","after"])
        self.assertEqual(report["native_tool_errors"],0)
        self.assertEqual(report["native_steps"],5)

    def test_memory_errors_do_not_consume_native_steps_or_error_quota(self):
        count = 0
        def worker(wire):
            nonlocal count
            count += 1
            if count <= 11:
                return dict(role="assistant",content=None,tool_calls=[call("memory_get",dict(id="unknown"),f"get-{count}")])
            return dict(role="assistant",content="Please provide details.")
        journal,_,report,_ = self.episode(worker,MemoryPolicy(True,"none",False,4))
        self.assertEqual(report["status"],"completed",report)
        self.assertEqual(report["native_tool_errors"],0)
        self.assertEqual(report["native_steps"],3)
        self.assertEqual(report["memory_returns"],dict(get=11))
        traces = [e["payload"] for e in journal.events.values() if e["payload"].get("origin") == "tau2_memory_dispatch"]
        self.assertTrue(all(t["native_step_before"] == t["native_step_after"] and t["native_errors_before"] == t["native_errors_after"] for t in traces))

    def test_configured_stop_with_memory_rejects_all_side_effects(self):
        def worker(wire):
            return dict(role="assistant",content=None,tool_calls=[
                call("memory_store",dict(title="must not exist",body="rejected"),"rejected-memory"),
                call("list_all_airports",{},"terminating")])
        journal,_,report,_ = self.episode(worker,MemoryPolicy(True,"during_task",True,4),
            configure=lambda orch:setattr(orch.agent,"additional_stop_tool_names",frozenset({"list_all_airports"})))
        self.assertEqual(report["status"],"completed",report)
        self.assertEqual(report["termination_reason"],"agent_stop")
        self.assertEqual(report["memory_stores"],0)
        self.assertFalse(any(e["payload"].get("action") == "list_all_airports" for e in journal.events.values()))
        self.assertEqual(sum(e["type"] == "policy.denied" for e in journal.events.values()),2)
        report = journal.blobs.get(report["report_ref"])
        self.assertTrue(all(m["execution_status"] == "rejected" for m in report["messages"] if m["role"] == "tool"))

    def test_post_write_failure_preserves_native_score_and_committed_memory(self):
        writer_calls = 0
        def worker(wire):
            nonlocal writer_calls
            names = [tool["function"]["name"] for tool in wire.get("tools",[])]
            if names == ["memory_store","memory_list","memory_get"]:
                writer_calls += 1
                if writer_calls == 1:
                    return dict(role="assistant",content=None,tool_calls=[call("memory_store",
                        dict(title="keep committed",body="Preserved before the injected interruption."),"keep")])
                raise URLError("injected interruption")
            return dict(role="assistant",content="Please provide details.")
        journal,_,report,_ = self.episode(worker,MemoryPolicy(True,"post_task",False,4))
        self.assertEqual(report["status"],"error",report)
        self.assertEqual(report["post_task_status"],"error")
        self.assertEqual(report["grading_status"],"graded")
        self.assertEqual(report["memory_stores"],1)
        native = journal.blobs.get(report["report_ref"])
        self.assertEqual(native["reward_info"]["reward"],report["official_score"])
        self.assertTrue(journal.state["banks"][report["bank_id"]]["sealed"])
        self.assertEqual(len(journal.state["snapshots"][report["snapshot_id"]]["heads"]),1)
        events = list(journal.events.values())
        outcome = next(e for e in events if e["type"] == "outcome.scored")
        stored = next(e for e in events if e["type"] == "memory.stored")
        self.assertLess(outcome["event_seq"],stored["event_seq"])
        self.assertFalse(any(e["payload"].get("action") for e in events if e["event_seq"] > outcome["event_seq"]))

    def test_during_and_post_update_keeps_the_same_public_worker_context(self):
        solve_calls, writer_calls = 0, 0
        def worker(wire):
            nonlocal solve_calls, writer_calls
            names = [tool["function"]["name"] for tool in wire.get("tools",[])]
            if names == ["memory_store"]:
                writer_calls += 1
                acknowledgements = [json.loads(m["content"]) for m in wire["messages"] if m["role"] == "tool"]
                self.assertTrue(any("during-task draft" in canonical(m).decode() for m in wire["messages"]))
                if writer_calls == 1:
                    ack = acknowledgements[-1]
                    return dict(role="assistant",content=None,tool_calls=[call("memory_store",dict(
                        id=ack["id"],expected_version=ack["version"],title="revised",body="post-task revision"),"revision")])
            else:
                solve_calls += 1
                if solve_calls == 1:
                    return dict(role="assistant",content=None,tool_calls=[call("memory_store",
                        dict(title="draft",body="during-task draft"),"draft")])
            return dict(role="assistant",content="Please provide details.")
        journal,_,report,_ = self.episode(worker,MemoryPolicy(True,"during_and_post",False,4,read_mode="none"))
        self.assertEqual(report["status"],"completed",report)
        self.assertEqual(report["post_task_status"],"completed")
        self.assertEqual(report["memory_stores"],2)
        stores = [e for e in journal.events.values() if e["type"] == "memory.stored"]
        self.assertEqual([e["phase"] for e in stores],["solve","post_task_write"])
        self.assertEqual(stores[1]["payload"]["parent_version_id"],stores[0]["payload"]["version_id"])
        heads = journal.state["snapshots"][report["snapshot_id"]]["heads"]
        self.assertEqual(len(heads),1)
        self.assertEqual(journal.blobs.get(journal.state["versions"][next(iter(heads.values()))])["body"],"post-task revision")

    def test_fork_writes_stay_in_clone_and_fixed_reads_keep_starting_version(self):
        phase, count, selected = "collection", 0, None
        def worker(wire):
            nonlocal count, selected
            count += 1
            if phase == "collection" and count == 1:
                calls = [call("memory_store",dict(title="original",body="source-only-body"),"initial")]
            elif phase != "collection" and count == 1:
                calls = [call("memory_list",{},"list-" + phase)]
            elif phase != "collection" and count == 2:
                selected = json.loads(wire["messages"][-1]["content"])["items"][0]
                if phase == "rewrite":
                    calls = [call("memory_store",dict(id=selected["id"],expected_version=selected["version"],
                        title="clone title",body="clone-only-marker"),"rewrite")]
                else:
                    calls = [call("memory_get",dict(id=selected["id"]),"get-second-clone")]
            elif phase == "rewrite" and count == 3:
                calls = [call("memory_get",dict(id=selected["id"]),"fixed-read")]
            else:
                if phase != "collection":
                    self.assertEqual(json.loads(wire["messages"][-1]["content"])["item"]["body"],"source-only-body")
                return dict(role="assistant",content="Please provide details.")
            return dict(role="assistant",content=None,tool_calls=calls)
        with TemporaryDirectory() as folder, Journal(folder,"clone-isolation") as journal:
            conversation = Conversation(worker)
            runner = Tau2MemoryRunner(journal,self.prepared[0],self.prepared[2],
                SpendGuard(max_calls=36,max_usd=0.25,max_seconds=180),opener=conversation)
            first = runner.episode(run_id=new_id("r"),stage="collection",policy=MemoryPolicy(True,"during_task",False,4),
                solve_instruction="Write during the task.",post_instruction="")
            self.assertEqual(first["status"],"completed",first)
            original = deepcopy(journal.state["snapshots"][first["snapshot_id"]])
            phase,count = "rewrite",0
            rewrite = runner.episode(run_id=new_id("r"),stage="probe",policy=MemoryPolicy(True,"during_task",False,4),
                solve_instruction="Use the permitted memory tools.",post_instruction="",parent=first["snapshot_id"],sources=[first["run_id"]])
            self.assertEqual(rewrite["status"],"completed",rewrite)
            phase,count = "read",0
            offset = len(conversation.wires)
            read = runner.episode(run_id=new_id("r"),stage="probe",policy=MemoryPolicy(True,"none",False,4),
                solve_instruction="Read the supplied bank.",post_instruction="",parent=first["snapshot_id"],sources=[first["run_id"]])
            self.assertEqual(read["status"],"completed",read)
            self.assertEqual(journal.state["snapshots"][first["snapshot_id"]],original)
            self.assertEqual(journal.state["banks"][first["bank_id"]]["heads"],original["heads"])
            self.assertNotEqual(journal.state["banks"][rewrite["bank_id"]]["heads"],original["heads"])
            self.assertNotIn("clone-only-marker",canonical(conversation.wires[offset:]).decode())

    def test_request_limit_closes_run_and_records_skipped_probe(self):
        def worker(wire):
            return dict(role="assistant",content=None,tool_calls=[call("memory_get",dict(id="unknown"),
                "repeat-"+str(len(wire["messages"])))])
        with TemporaryDirectory() as folder, Journal(folder,"bounded-memory") as journal:
            summary = run_memory_check(journal,deepcopy(self.profile),self.prepared,live=True,opener=Conversation(worker))
            self.assertEqual(summary["status"],"error")
            self.assertIsNone(summary["probe"])
            self.assertEqual(summary["probe_skipped_reason"],"collection_error")
            self.assertEqual(summary["http_calls"],24)
            self.assertEqual(len(journal.scan["starts"]),1)
            self.assertEqual(journal.scan["incomplete_runs"],[])
            self.assertEqual(journal.scan["pending_requests"],[])
            (Path(folder)/"memory-summary.json").write_bytes(canonical(summary))
            self.assertEqual(audit_memory_check(folder)["run_count"],1)


if __name__ == "__main__":
    unittest.main()
