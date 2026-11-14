"""Configurable native delivery comparisons with fake HTTP and isolated file banks."""

from collections import Counter
from copy import deepcopy
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.tau2_delivery import audit_delivery
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest, new_id, read_json
from agent_memory_eval.models.openrouter import SpendGuard
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner.tau2_delivery import compile_delivery, make_schedule, preflight_delivery, prepare_delivery, run_delivery
from agent_memory_eval.runner.tau2_memory import Tau2MemoryRunner, fresh_orchestrator
from test_openrouter import Reply, response
from test_tau2_memory import Conversation, call


ROOT = Path(__file__).resolve().parents[1]
AVAILABLE = importlib.util.find_spec("tau2") is not None and (ROOT / "data/tau2-bench/.git").is_dir()
PROFILE = ROOT / "protocols/drafts/tau2-delivery-comparison-v1.json"


def ready_profile():
    """These small numbers belong only to the synthetic/mock test configuration."""
    profile = read_json(PROFILE.read_bytes())
    profile.update(status="ready", tasks=[dict(id="0", source_group_id="source-0"), dict(id="40", source_group_id="shared-reservation")],
        repetitions=1, schedule_seed=18, preload_query_tools=True,
        guards=dict(max_model_requests=100, max_reported_cost_usd=2.0, max_seconds=240),
        frozen_source=dict(path="runs/test-only", log_sha256="0" * 64, experiment_id="test-source", snapshot_id="test-snapshot", source_run_id="test-run"))
    profile["memory_limits"]["page_size"] = 2
    return profile


class DeliveryCompileTests(unittest.TestCase):
    def test_draft_inspection_has_no_hidden_repetition_or_page_size(self):
        plan = compile_delivery(read_json(PROFILE.read_bytes()), require_ready=False)
        self.assertFalse(plan["ready"])
        self.assertIn("tasks", plan["unresolved_fields"])
        self.assertIn("repetitions", plan["unresolved_fields"])
        self.assertIn("preload_query_tools", plan["unresolved_fields"])
        self.assertNotIn("blocks", plan)

    def test_allocation_balances_positions_and_retains_task_groups(self):
        profile = ready_profile()
        profile["repetitions"] = 4
        plan = compile_delivery(profile)
        rows = make_schedule(plan)
        self.assertEqual(len(rows), 32)
        self.assertEqual(len({r["run_id"] for r in rows}), 32)
        counts = Counter((r["task_id"], r["condition"], r["position"]) for r in rows)
        self.assertEqual(set(counts.values()), {1})
        self.assertEqual(plan["blocks"], compile_delivery(profile)["blocks"])
        self.assertEqual(plan["policies"]["empty_bank"], plan["policies"]["on_demand"])
        profile["preload_query_tools"] = False
        self.assertEqual(compile_delivery(profile)["policies"]["full_preload"]["read_mode"], "preload")

    def test_unready_or_invalid_settings_fail_before_native_or_network_preparation(self):
        for kind in ("draft", "repetition", "duplicate", "primary", "instruction", "group", "query"):
            profile = ready_profile()
            if kind == "draft": profile["status"] = "draft"
            if kind == "repetition": profile["repetitions"] = None
            if kind == "duplicate": profile["tasks"].append(deepcopy(profile["tasks"][0]))
            if kind == "primary": profile["primary_comparison"]["treated"] = "on_demand"
            if kind == "instruction": profile["memory_instruction_ref"]["sha256"] = "0" * 64
            if kind == "group": profile["tasks"][0].pop("source_group_id")
            if kind == "query": profile["preload_query_tools"] = None
            with self.subTest(kind=kind), patch("agent_memory_eval.runner.tau2_delivery.prepare_native") as native:
                with self.assertRaises(ContractError):
                    prepare_delivery(profile, ROOT / "data/tau2-bench")
                native.assert_not_called()

    @unittest.skipUnless(AVAILABLE, "Pinned tau2 checkout required")
    def test_test_split_task_is_rejected_before_loading_a_source(self):
        profile = ready_profile()
        profile["tasks"] = [dict(id="2", source_group_id="split-check")]
        with patch("agent_memory_eval.runner.tau2_delivery.load_source") as source:
            with self.assertRaisesRegex(ContractError, "split"):
                preflight_delivery(profile, ROOT / "data/tau2-bench")
            source.assert_not_called()


@unittest.skipUnless(AVAILABLE, "Install tau2 extra and pinned checkout for native tests")
class NativeDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agent_memory_eval.adapters.tau2 import prepare_native
        config = compile_delivery(ready_profile())["connection"]
        cls.catalog = dict(data=[dict(id=config[r]["model_id"], canonical_slug=config[r]["model_id"],
            supported_parameters=list(config[r]["parameters"]), pricing=dict(prompt="0.000001", completion="0.000001")) for r in ("worker", "user_simulator")])
        with patch("agent_memory_eval.adapters.tau2.model_catalog", return_value=cls.catalog), \
                patch("agent_memory_eval.adapters.tau2.model_endpoints", side_effect=cls.endpoints):
            cls.native = prepare_native(config, ROOT / "data/tau2-bench")
        cls.connection = config

    @staticmethod
    def endpoints(model_id):
        user = model_id.startswith("deepseek/")
        return dict(data=dict(id=model_id, endpoints=[dict(tag="fireworks" if user else "openai", provider_name="Fireworks" if user else "OpenAI", status=0,
            supported_parameters=["reasoning", "max_tokens", "temperature", "tools", "tool_choice"] + ([] if user else ["seed"]), pricing=dict(prompt="0.000001", completion="0.000001"))]))

    def setUp(self):
        environment = patch.dict("os.environ", OPENROUTER_API_KEY="delivery-test-key")
        environment.start()
        self.addCleanup(environment.stop)
        (ROOT / "runs").mkdir(exist_ok=True)
        self.source = TemporaryDirectory(dir=ROOT / "runs")
        self.addCleanup(self.source.cleanup)
        source_path = Path(self.source.name)
        def writer(wire):
            names = [t["function"]["name"] for t in wire.get("tools", [])]
            prior = [c["function"]["name"] for m in wire["messages"] for c in m.get("tool_calls") or []]
            if names == ["memory_store"] and "memory_store" not in prior:
                return dict(role="assistant", content=None, tool_calls=[call("memory_store", dict(title="Memo", body="PUBLIC_MEMORY_SENTINEL"), "source-store")])
            return dict(role="assistant", content="PRIVATE_SOURCE_TRANSCRIPT_SENTINEL")
        with Journal(source_path, "delivery-test-source") as journal:
            runner = Tau2MemoryRunner(journal, self.connection, self.native, SpendGuard(max_calls=24, max_usd=1, max_seconds=180), opener=Conversation(writer))
            result = runner.episode(run_id=new_id("r"), stage="collection", policy=MemoryPolicy(True, "post_task", False, 2, read_mode="none"),
                solve_instruction="Write after the task.", post_instruction="Save reusable public observations.")
            self.assertEqual(result["status"], "completed")
        self.profile = ready_profile()
        self.profile["frozen_source"] = dict(path=str(source_path.relative_to(ROOT)), log_sha256=digest((source_path / "log.jsonl").read_bytes()),
            experiment_id="delivery-test-source", snapshot_id=result["snapshot_id"], source_run_id=result["run_id"])

    def prepare(self):
        with patch("agent_memory_eval.adapters.tau2.model_catalog", return_value=self.catalog), \
                patch("agent_memory_eval.adapters.tau2.model_endpoints", side_effect=self.endpoints):
            return prepare_delivery(self.profile, ROOT / "data/tau2-bench")

    @staticmethod
    def worker(wire):
        previous = [c["function"]["name"] for m in wire["messages"] for c in m.get("tool_calls") or []]
        if "list_all_airports" not in previous:
            return dict(role="assistant", content=None, tool_calls=[call("list_all_airports", {}, "business")])
        return dict(role="assistant", content="Please provide your details.")

    def test_multiple_tasks_four_conditions_are_isolated_and_reauditable(self):
        prepared, conversation, environments = self.prepare(), Conversation(self.worker), []
        def fresh(native):
            orch = fresh_orchestrator(native)
            environments.append(orch)
            return orch
        with TemporaryDirectory() as folder, Journal(folder, "delivery-test", contract_version="1.2") as journal, \
                patch("agent_memory_eval.runner.tau2_memory.fresh_orchestrator", side_effect=fresh):
            summary = run_delivery(journal, self.profile, prepared, opener=conversation)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(len(summary["runs"]), 8)
            self.assertEqual(len({r["session_id"] for r in summary["runs"]}), 8)
            for attribute in ("agent", "user", "environment", "agent_state", "user_state"):
                self.assertEqual(len({id(getattr(e, attribute)) for e in environments}), 8)
            self.assertEqual(len({id(e.environment.tools.db) for e in environments}), 8)
            for row in summary["runs"]:
                self.assertEqual(row["official_success"], row["task_id"] == "0")
                self.assertEqual(row["list_calls"] + row["get_calls"], 0)
                self.assertEqual(row["body_input_spans"] > 0, row["condition"] == "full_preload")
            for wire in conversation.wires:
                text = canonical(wire).decode()
                self.assertNotIn("PRIVATE_SOURCE_TRANSCRIPT_SENTINEL", text)
                if wire["model"].startswith("deepseek/"):
                    self.assertNotIn("PUBLIC_MEMORY_SENTINEL", text)
            worker_requests = [e for e in journal.events.values() if e["type"] == "model.request_started" and e["payload"]["actor_role"] == "worker"]
            for event in worker_requests:
                wire = journal.blobs.get(event["payload"]["request_ref"])
                self.assertEqual("PUBLIC_MEMORY_SENTINEL" in canonical(wire).decode(), event["condition_id"] == "full_preload")
            for task in ("0", "40"):
                self.assertEqual(len({r["native_steps"] for r in summary["runs"] if r["task_id"] == task}), 1)
            saved = Path(folder) / "delivery-summary.json"
            saved.write_bytes(canonical(summary))
            original = (Path(folder) / "log.jsonl").read_bytes()
            self.source.cleanup()
            self.assertEqual(audit_delivery(folder)["audit_status"], "audit_passed")
            self.assertEqual((Path(folder) / "log.jsonl").read_bytes(), original)
            summary["runs"][0]["official_success"] = not summary["runs"][0]["official_success"]
            saved.write_bytes(canonical(summary))
            with self.assertRaisesRegex(ContractError, "summary differs"):
                audit_delivery(folder)

    def test_preload_without_query_tools_still_reaches_the_first_worker_request(self):
        self.profile.update(tasks=self.profile["tasks"][:1], conditions=["on_demand", "full_preload"], preload_query_tools=False)
        def worker(wire):
            names = [t["function"]["name"] for t in wire["tools"]]
            previous = [c["function"]["name"] for m in wire["messages"] for c in m.get("tool_calls") or []]
            if "memory_list" in names and "memory_list" not in previous:
                return dict(role="assistant", content=None, tool_calls=[call("memory_list", {}, "list")])
            if "memory_get" in names and "memory_get" not in previous:
                item = read_json(wire["messages"][-1]["content"].encode())["items"][0]["id"]
                return dict(role="assistant", content=None, tool_calls=[call("memory_get", dict(id=item), "get")])
            if "memory_list" not in names and "memory_store" not in previous:
                return dict(role="assistant", content=None, tool_calls=[call("memory_store", dict(title="bad", body="should be denied"), "denied-store")])
            return self.worker(wire)
        conversation = Conversation(worker)
        with TemporaryDirectory() as folder, Journal(folder, "preload-only", contract_version="1.2") as journal:
            summary = run_delivery(journal, self.profile, self.prepare(), opener=conversation)
            full = next(r for r in summary["runs"] if r["condition"] == "full_preload")
            self.assertGreater(full["body_input_spans"], 0)
            self.assertIn("POLICY_DENIED", full["policy_denials"])
            query = next(r for r in summary["runs"] if r["condition"] == "on_demand")
            self.assertEqual((query["list_calls"], query["get_calls"]), (1, 1))
            self.assertGreater(query["body_input_spans"], 0)
            event = next(e for e in journal.events.values() if e["type"] == "model.request_started" and e["condition_id"] == "full_preload" and e["payload"]["actor_role"] == "worker")
            wire = journal.blobs.get(event["payload"]["request_ref"])
            self.assertFalse(any(t["function"]["name"].startswith("memory_") for t in wire["tools"]))

    def test_unknown_cost_preserves_the_unexecuted_allocation(self):
        def opener(request, timeout):
            wire = read_json(request.data)
            return Reply(dict(response(None), model=wire["model"], provider="Fireworks"))
        with TemporaryDirectory() as folder, Journal(folder, "unknown-delivery", contract_version="1.2") as journal:
            summary = run_delivery(journal, self.profile, self.prepare(), opener=opener)
            self.assertEqual(summary["http_calls"], 1)
            self.assertTrue(summary["unknown_cost"])
            self.assertEqual(sum(r["status"] == "skipped" for r in summary["runs"]), 7)
            self.assertTrue(all(r["official_success"] is None for r in summary["runs"]))

    def test_source_storage_and_preload_limits_fail_before_model_preparation(self):
        for key, cap in (("max_items", 0), ("max_payload_bytes", 1), ("max_return_bytes", 1)):
            profile = deepcopy(self.profile)
            profile["memory_limits"][key] = cap
            with self.subTest(limit=key), patch("agent_memory_eval.runner.tau2_delivery.prepare_native") as native:
                with self.assertRaisesRegex(ContractError, "limit"):
                    prepare_delivery(profile, ROOT / "data/tau2-bench")
                native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
