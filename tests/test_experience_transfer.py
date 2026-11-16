"""Synthetic HTTP writing -> frozen bank -> fresh native DB mutation and grading."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.identity import identity_trace
from agent_memory_eval.analysis.memory_writing import audit_memory_writes
from agent_memory_eval.contracts.connection import native_parameters, provider_route
from agent_memory_eval.contracts.core import MemoryPolicy, canonical, new_id
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.memory.context import past_memory_instruction, request_with_past_memory
from agent_memory_eval.memory.writing import WriterFeedback, freeze_public_experience, write_from_experience
from agent_memory_eval.models.openrouter import OpenRouterClient, SpendGuard
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner.tau2_memory import Tau2MemoryRunner, fresh_orchestrator
from test_openrouter import Reply, response
from test_tau2_memory import Conversation, call


ROOT = Path(__file__).resolve().parents[1]
AVAILABLE = importlib.util.find_spec("tau2") is not None and (ROOT / "data/tau2-bench/.git").is_dir()


class IdentityTraceTests(unittest.TestCase):
    def test_past_id_does_not_count_as_current_user_disclosure(self):
        native = [dict(role="user", content="Please help."), dict(role="assistant", content=None,
            tool_calls=[dict(name="get_user_details", arguments=dict(user_id="test_user"))])]
        self.assertEqual(identity_trace(native, "test_user")["status"], "requires_review")
        native[0]["content"] = "My user ID is test_user."
        self.assertEqual(identity_trace(native, "test_user")["status"], "literal_id_seen")
        native[0]["content"] = "My user ID is test_user_other."
        self.assertEqual(identity_trace(native, "test_user")["status"], "requires_review")


@unittest.skipUnless(AVAILABLE, "Install tau2 extra and the pinned checkout")
class NativeExperienceTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agent_memory_eval.adapters.tau2 import prepare_native
        cls.config = json.loads((ROOT / "protocols/checks/openrouter-tau2-smoke.json").read_text())
        cls.config["benchmark"]["task_id"] = "40"
        catalog = dict(data=[dict(id=cls.config[r]["model_id"], canonical_slug=cls.config[r]["model_id"],
            supported_parameters=list(cls.config[r]["parameters"]), pricing=dict(prompt="0.000001", completion="0.000001"))
            for r in ("worker", "user_simulator")])
        def endpoints(model_id):
            user = model_id.startswith("deepseek/")
            return dict(data=dict(id=model_id, endpoints=[dict(tag="fireworks" if user else "openai",
                provider_name="Fireworks" if user else "OpenAI", status=0,
                supported_parameters=["reasoning", "temperature", "max_tokens", "seed", "tools", "tool_choice"],
                pricing=dict(prompt="0.000001", completion="0.000001"))]))
        with patch("agent_memory_eval.adapters.tau2.model_catalog", return_value=catalog), \
                patch("agent_memory_eval.adapters.tau2.model_endpoints", side_effect=endpoints):
            cls.prepared = prepare_native(cls.config, ROOT / "data/tau2-bench")

    def test_optional_writer_memory_drives_scripted_probe_and_native_grade(self):
        # Answer arguments are synthetic test controls and are never supplied to the writer.
        task = json.loads((ROOT / "data/tau2-bench/data/tau2/domains/airline/tasks.json").read_text())
        task = next(t for t in task if t["id"] == "40")
        update = task["evaluation_criteria"]["actions"][1]["arguments"]
        user_id = self.prepared[0].environment.tools.db.reservations["3RK2T9"].user_id
        wires, user_calls, initial_hashes = [], 0, []
        def opener(request, timeout):
            nonlocal user_calls
            wire = json.loads(request.data)
            wires.append(wire)
            body = response()
            body.update(id="transfer-" + str(len(wires)), model=wire["model"])
            names = [t["function"]["name"] for t in wire.get("tools", [])]
            previous = [c["function"]["name"] for m in wire["messages"] for c in m.get("tool_calls") or []]
            if wire["model"].startswith("deepseek/"):
                body["provider"] = "Fireworks"
                user_calls += 1
                message = dict(role="assistant", content=("My ID is " + user_id + ". Please update the passenger; I confirm."
                    if user_calls % 2 else "###STOP###"))
            elif names == ["memory_store"]:
                text = canonical(wire).decode()
                self.assertIn("SOURCE_PUBLIC_SENTINEL", text)
                for secret in ("SOURCE_PRIVATE_REASONING", "Mei", "HIDDEN_CRITIQUE_ORIGIN"):
                    self.assertNotIn(secret, text)
                if "memory_store" not in previous:
                    message = dict(role="assistant", content=None, tool_calls=[call("memory_store", dict(
                        title="Passenger changes", body="WRITER_CREATED_NOTE: Verify the current passenger correction and apply the passenger update tool."), "store")])
                else:
                    message = dict(role="assistant", content="Finished writing.")
            elif "WRITER_CREATED_NOTE" not in canonical(wire).decode():
                message = dict(role="assistant", content="SOURCE_PUBLIC_SENTINEL", reasoning="SOURCE_PRIVATE_REASONING")
            elif "get_reservation_details" not in previous:
                message = dict(role="assistant", content=None,
                    tool_calls=[call("get_reservation_details", dict(reservation_id="3RK2T9"), "lookup")])
            elif "update_reservation_passengers" not in previous:
                message = dict(role="assistant", content=None,
                    tool_calls=[call("update_reservation_passengers", update, "update")])
            else:
                message = dict(role="assistant", content="The requested passenger is updated.")
            body["choices"][0].update(message=message, finish_reason="tool_calls" if message.get("tool_calls") else "stop")
            return Reply(body)
        original_fresh = fresh_orchestrator
        environments = []
        def fresh(prepared):
            orch = original_fresh(prepared)
            initial_hashes.append(orch.environment.get_db_hash())
            environments.append(orch)
            return orch
        with TemporaryDirectory() as folder, Journal(folder, "transfer", contract_version="1.2") as journal, \
                patch.dict("os.environ", OPENROUTER_API_KEY="test-only"), \
                patch("agent_memory_eval.runner.tau2_memory.fresh_orchestrator", side_effect=fresh):
            guard = SpendGuard(max_calls=40, max_usd=2, max_seconds=180)
            runner = Tau2MemoryRunner(journal, self.config, self.prepared, guard, opener=opener)
            source = runner.episode(run_id=new_id("r"), stage="collection",
                policy=MemoryPolicy(False, "none", False, 2, read_mode="none"), solve_instruction="", post_instruction="")
            self.assertFalse(source["official_success"])
            experience = freeze_public_experience(journal, source["run_id"])
            client = OpenRouterClient(self.prepared[2]["worker"], guard,
                route=provider_route(self.config, "worker"), opener=opener)
            written = write_from_experience(journal, experience,
                WriterFeedback("critique", "Check whether a permitted passenger update was omitted.", "HIDDEN_CRITIQUE_ORIGIN"),
                instruction="Write optional transferable observations, without specific customer identifiers.",
                policy=MemoryPolicy(True, "post_task", False, 2, read_mode="none"), client=client,
                model_config=dict(model=self.config["worker"]["model_id"], provider=provider_route(self.config, "worker"),
                    stream=False, **native_parameters(self.config, "worker")), max_requests=4, condition_id="writing")
            self.assertEqual(written["status"], "completed")
            read_policy = MemoryPolicy(True, "none", False, 2, read_mode="preload")
            instruction = "Consult the notes when applicable."
            probes = [runner.episode(run_id=new_id("r"), stage="probe", policy=read_policy,
                solve_instruction=past_memory_instruction(instruction), post_instruction="", parent=written["snapshot_id"],
                sources=[written["run_id"]], condition_id="probe") for _ in range(2)]
            self.assertTrue(all(p["official_success"] and p["memory_input_spans"] > 0 for p in probes))
            self.assertEqual(len(set(initial_hashes)), 1)
            self.assertEqual(len({id(e.environment.tools.db) for e in environments}), 3)
            self.assertEqual(len({journal.scan["starts"][r]["session_id"] for r in journal.scan["starts"]}), 4)
            for probe in probes:
                report = journal.blobs.get(probe["report_ref"])
                self.assertEqual(identity_trace(report["messages"], user_id)["status"], "literal_id_seen")
                self.assertNotIn("SOURCE_PUBLIC_SENTINEL", canonical(report).decode())
                bank = journal.state["banks"][probe["bank_id"]]
                self.assertEqual(bank["start_heads"], journal.state["snapshots"][written["snapshot_id"]]["heads"])
            probe_wire = next(w for w in wires if not w["model"].startswith("deepseek/")
                and "WRITER_CREATED_NOTE" in canonical(w).decode()
                and [t["function"]["name"] for t in w["tools"]] != ["memory_store"])
            items = json.loads(probe_wire["messages"][1]["content"])["items"]
            original = deepcopy(probe_wire)
            original["messages"].pop(1)
            original["messages"][0]["content"] = self.prepared[0].agent.system_prompt
            self.assertEqual(request_with_past_memory(original, read_policy, items, instruction=instruction), probe_wire)
            self.assertEqual(len(audit_memory_writes(folder)["writes"]), 1)
            for wire in wires:
                if wire["model"].startswith("deepseek/"):
                    self.assertNotIn("WRITER_CREATED_NOTE", canonical(wire).decode())
                    self.assertNotIn("evaluation_feedback", canonical(wire).decode())
            def wrong_provider(request, timeout):
                body = response()
                body.update(model=self.config["worker"]["model_id"], provider="Unexpected provider")
                return Reply(body)
            client.opener = wrong_provider
            wrong = write_from_experience(journal, experience, WriterFeedback("none"),
                instruction="Write optional observations.", policy=MemoryPolicy(True, "post_task", False, 2, read_mode="none"),
                client=client, model_config=dict(model=self.config["worker"]["model_id"],
                    provider=provider_route(self.config, "worker"), stream=False, **native_parameters(self.config, "worker")),
                max_requests=4, condition_id="wrong-provider")
            self.assertEqual((wrong["status"], wrong["memory_stores"]), ("error", 0))
            self.assertEqual(len(audit_memory_writes(folder)["writes"]), 2)
            def unknown_cost(request, timeout):
                body = response()
                body.update(model=self.config["worker"]["model_id"], provider="OpenAI", usage=None)
                return Reply(body)
            client.opener = unknown_cost
            unknown = write_from_experience(journal, experience, WriterFeedback("none"),
                instruction="Write optional observations.", policy=MemoryPolicy(True, "post_task", False, 2, read_mode="none"),
                client=client, model_config=dict(model=self.config["worker"]["model_id"],
                    provider=provider_route(self.config, "worker"), stream=False, **native_parameters(self.config, "worker")),
                max_requests=4, condition_id="unknown-cost")
            self.assertEqual((unknown["status"], unknown["error_code"], unknown["unknown_cost"]),
                             ("error", "UNKNOWN_COST_STOP", True))
            self.assertEqual(len(audit_memory_writes(folder)["writes"]), 3)

    def test_policy_limit_assesses_reached_DB_without_claiming_official_success(self):
        task = json.loads((ROOT / "data/tau2-bench/data/tau2/domains/airline/tasks.json").read_text())
        update = next(t for t in task if t["id"] == "40")["evaluation_criteria"]["actions"][1]["arguments"]
        def worker(wire):
            previous = [c["function"]["name"] for m in wire["messages"] for c in m.get("tool_calls") or []]
            name, args = (("get_reservation_details", dict(reservation_id="3RK2T9")) if not previous
                          else ("update_reservation_passengers", update))
            return dict(role="assistant", content=None, tool_calls=[call(name, args, "action-" + str(len(previous)))])
        conversation = Conversation(worker)
        with TemporaryDirectory() as folder, Journal(folder, "reached") as journal, \
                patch.dict("os.environ", OPENROUTER_API_KEY="test-only"):
            runner = Tau2MemoryRunner(journal, self.config, self.prepared,
                SpendGuard(max_calls=20, max_usd=2, max_seconds=180), opener=conversation)
            result = runner.episode(run_id=new_id("r"), stage="probe",
                policy=MemoryPolicy(False, "none", False, 2, read_mode="none"), solve_instruction="", post_instruction="",
                execution_limits=ExecutionLimits(2, 0, 1, 100000), assess_on_policy_limit=True)
            self.assertEqual(result["error_code"], "ROLE_REQUEST_LIMIT")
            self.assertIsNone(result["official_success"])
            self.assertEqual(result["grading_status"], "incomplete")
            assessment = journal.blobs.get(result["reached_state_assessment_ref"])
            self.assertEqual(assessment["status"], "assessed", assessment)
            self.assertTrue(assessment["db_component"]["db_check"]["db_match"])
            self.assertEqual(assessment["model_calls"], 0)
            self.assertEqual(len(conversation.wires), 3)

    def test_user_limit_stays_incomplete_without_policy_state_assessment(self):
        conversation = Conversation(lambda wire: dict(role="assistant", content="Please provide details."))
        with TemporaryDirectory() as folder, Journal(folder, "user-limit") as journal, \
                patch.dict("os.environ", OPENROUTER_API_KEY="test-only"):
            runner = Tau2MemoryRunner(journal, self.config, self.prepared,
                SpendGuard(max_calls=20, max_usd=2, max_seconds=180), opener=conversation)
            result = runner.episode(run_id=new_id("r"), stage="probe",
                policy=MemoryPolicy(False, "none", False, 2, read_mode="none"), solve_instruction="", post_instruction="",
                execution_limits=ExecutionLimits(3, 0, 1, 100000), assess_on_policy_limit=True)
            self.assertEqual(result["error_code"], "ROLE_REQUEST_LIMIT")
            self.assertIsNone(result["reached_state_assessment_ref"])
            self.assertIsNone(result["official_success"])


if __name__ == "__main__":
    unittest.main()
