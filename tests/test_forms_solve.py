"""Forms solve behavior through native τ², its real DB/scorer and a scripted HTTP transport."""

from copy import deepcopy
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError

from agent_memory_eval.adapters.tau2 import native_modules
from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest, new_id
from agent_memory_eval.experiments.forms_solve import run_forms_solve
from agent_memory_eval.memory.context import request_with_past_memory
from agent_memory_eval.models.openrouter import OpenRouterClient, SpendGuard
from agent_memory_eval.recording.journal import Journal, read_experiment
from agent_memory_eval.runner.tau2_baseline import build_baseline_orchestrator
from test_openrouter import Reply, response


ROOT = Path(__file__).resolve().parents[1]
CHECKOUT = ROOT / "data/tau2-bench"
AVAILABLE = importlib.util.find_spec("tau2") is not None and (CHECKOUT / "src/tau2").is_dir()
DESIGN = json.loads((ROOT / "protocols/drafts/tau2-two-pass-memory-forms-v1.json").read_text())
INSTRUCTION = (ROOT / DESIGN["second_pass"]["solve_instruction_ref"]["path"]).read_text()
RECORD = canonical(dict(kind="past_public_experience", task_context=dict(role="system", content="Earlier public policy."),
    messages=[dict(role="user", content="Earlier public traveler message – 공개 자료")])).decode()
SOURCE_HASH = digest(RECORD.encode())
ITEMS = [
    dict(id="m_a", version="v_a", title="What I saw", body="A public observation.\n다음 줄."),
    dict(id="m_b", version="v_b", title="A later note", body='He said "check again".'),
]


def bank(arm, task_id, items):
    return dict(task_id=task_id, repetition=1, arm=arm, run_id="writer-private-id",
                snapshot_id="writer-private-snapshot", status="completed" if items else "empty",
                items=deepcopy(items), head_revision=len(items), public_context_sha256=SOURCE_HASH)


class NativeHTTP:
    def __init__(self, *, worker=None, user=None, change_response=None):
        self.wires = []
        self.worker = worker or (lambda n, wire: "You may bring 4.")
        self.user = user or (lambda n: "Hello, please help." if n == 1 else "###STOP###")
        self.change_response = change_response
        self.by_role = {"worker": [], "user_simulator": []}

    def __call__(self, request, timeout):
        wire = json.loads(request.data)
        self.wires.append(wire)
        role = "user_simulator" if wire["model"].startswith("deepseek/") else "worker"
        self.by_role[role].append(wire)
        n = len(self.by_role[role])
        answer = self.user(n) if role == "user_simulator" else self.worker(n, wire)
        if isinstance(answer, str):
            answer = dict(role="assistant", content=answer)
        raw = response()
        raw.update(id=f"forms-{len(self.wires)}", model=wire["model"],
                   provider="Fireworks" if role == "user_simulator" else "OpenAI")
        raw["choices"][0].update(message=answer, finish_reason="tool_calls" if answer.get("tool_calls") else "stop")
        if self.change_response:
            self.change_response(raw, role)
        return Reply(raw)


@unittest.skipUnless(AVAILABLE, "Requires the installed pinned tau2 and its local checkout")
class FormsSolveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = native_modules(CHECKOUT)
        cls.tasks = {t["id"]: t for t in json.loads((CHECKOUT / "data/tau2/domains/airline/tasks.json").read_text())}
        cls.base = json.loads((ROOT / DESIGN["model"]["connection_profile_ref"]["path"]).read_text())
        cls.models = {r: dict(id=cls.base[r]["model_id"], canonical_slug=cls.base[r]["model_id"],
            pricing=dict(prompt="0.000001", completion="0.000001"),
            selected_endpoint=dict(tag=cls.base[r]["provider"]["only"][0],
                                   provider_name="OpenAI" if r == "worker" else "Fireworks"))
            for r in ("worker", "user_simulator")}
        from loguru import logger
        logger.disable("tau2")

    def setUp(self):
        patcher = patch.dict("os.environ", OPENROUTER_API_KEY="only-a-local-mock-key")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.guard = SpendGuard(max_calls=85, max_usd=5, max_seconds=150)
        self.limits = deepcopy(DESIGN["second_pass"]["proposed_limits"])

    @classmethod
    @lru_cache(maxsize=20)
    def prepared(cls, task, seed, steps, errors, seconds, total):
        conn = deepcopy(cls.base)
        conn["benchmark"].update(task_id=task, seed=seed, max_steps=steps, max_errors=errors)
        conn["worker"]["parameters"]["seed"] = seed
        conn["guards"].update(max_seconds=seconds, max_model_requests=total)
        template, native = build_baseline_orchestrator(conn, cls.tasks[task])
        return conn, (template, native, cls.models, cls.modules)

    def setup_job(self, arm, task="3", http=None, **overrides):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        journal = Journal(tmp.name, new_id("test"), contract_version="1.2")
        self.addCleanup(journal.close)
        http = http or NativeHTTP()
        limits = self.limits
        conn, native = self.prepared(task, 2, limits["native_steps"], limits["native_errors"],
                                     limits["episode_seconds"], limits["max_total_requests_per_episode"])
        kwargs = dict(arm=arm, task_id=task, repetition=1, run_id=new_id("solve"), connection=conn,
                      prepared=native, guard=self.guard, limits=deepcopy(limits), opener=http,
                      instruction="" if arm == "no_memory" else INSTRUCTION, public_context_sha256=SOURCE_HASH)
        kwargs.update(overrides)
        return journal, kwargs, http

    def test_stock_wire_and_all_forms_share_native_inputs_and_readonly_delivery(self):
        links = {}
        for arm in ("no_memory", "empty_preload", "fact", "episode", "lesson", "procedure", "public_record", "summary"):
            with self.subTest(arm=arm):
                journal, kw, http = self.setup_job(arm)
                if arm == "public_record":
                    kw["public_record"] = RECORD
                elif arm not in {"no_memory", "empty_preload"}:
                    kw["writer_bank"] = bank(arm, "3", [] if arm == "episode" else ITEMS)
                result = run_forms_solve(journal, **kw)
                links[arm] = (http, result)
                self.assertIs(result["official_success"], True, result)
                native = journal.blobs.get(result["report_ref"])
                self.assertEqual(native["reward_info"]["reward"], 1)
                self.assertEqual(native["termination_reason"], "user_stop")
                self.assertEqual(result["grading_status"], "graded")
                self.assertEqual(result["reported_cost_usd_decimal"], "0.0003")
                self.assertEqual(result["usage_by_role"]["worker"]["reported_tokens"], 17)
                self.assertEqual(result["http_calls"], 3)
                self.assertFalse(result["unknown_cost"])
                self.assertEqual(audit_requests(journal.root)["memory_input_spans"], result["memory_input_spans"])
                worker = http.by_role["worker"][0]
                self.assertEqual(worker["seed"], 2)
                for w in http.by_role["user_simulator"]:
                    self.assertNotIn("seed", w)
                    self.assertNotIn("Earlier public traveler", canonical(w).decode())
                    self.assertNotIn("A public observation", canonical(w).decode())
                self.assertTrue(all(not t["function"]["name"].startswith("memory_") for t in worker["tools"]))
                self.assertNotIn("writer-private", canonical(http.wires).decode())
                if arm == "no_memory":
                    self.assertEqual(journal.state["banks"], {})
                    self.assertFalse(any(e["type"].startswith("memory.") for e in journal.events.values()))
                    self.assertEqual(worker["messages"][0]["content"], kw["prepared"][0].agent.system_prompt)
                    continue
                expected = ITEMS
                if arm in {"empty_preload", "episode"}:
                    expected = []
                if arm == "public_record":
                    item = json.loads(worker["messages"][1]["content"])["items"][0]
                    self.assertEqual((item["title"], item["body"]), ("Earlier public interaction", RECORD))
                    expected = [item]
                assignment = journal.state["assignments"][kw["run_id"]]
                policy = MemoryPolicy(**assignment["policy"])
                baseline = links["no_memory"][0]
                self.assertEqual(worker, request_with_past_memory(baseline.by_role["worker"][0], policy, expected,
                                                                   instruction=INSTRUCTION))
                self.assertEqual(http.by_role["user_simulator"], baseline.by_role["user_simulator"])
                self.assertEqual(result["memory_input_spans"], 2 * len(expected))
                self.assertEqual(result["normal_response_memory_spans"], 2 * len(expected))
                self.assertTrue(journal.state["banks"][result["bank_id"]]["sealed"])
                self.assertNotEqual(result["snapshot_id"], result["start_snapshot_id"])
        self.assertEqual(links["empty_preload"][1]["first_worker_request_sha256"],
                         links["episode"][1]["first_worker_request_sha256"])
        self.assertEqual(len({row["session_id"] for _, row in links.values()}), 8)

    def test_native_database_change_after_confirmed_action_and_fresh_failed_retry(self):
        def user(n):
            return ["Please change Mei Lee to Mei Garcia on 3RK2T9, user anya_garcia_5901.",
                    "Yes, make the name change.", "###STOP###"][n - 1]

        def worker(n, wire):
            if n == 1:
                return "Please confirm changing Mei Lee to Mei Garcia on your reservation."
            if n == 2:
                return dict(role="assistant", content=None, tool_calls=[dict(id="change-name", type="function",
                    function=dict(name="update_reservation_passengers", arguments=json.dumps(
                        dict(reservation_id="3RK2T9", passengers=[
                            dict(first_name="Anya", last_name="Garcia", dob="1992-11-12"),
                            dict(first_name="Mei", last_name="Garcia", dob="1989-12-13")]))))])
            self.assertEqual(wire["messages"][-1]["role"], "tool")
            return "The name change is complete."

        http = NativeHTTP(worker=worker, user=user)
        journal, kw, _ = self.setup_job("fact", "40", http, writer_bank=bank("fact", "40", ITEMS))
        succeeded = run_forms_solve(journal, **kw)
        self.assertIs(succeeded["official_success"], True, succeeded)
        self.assertEqual(succeeded["memory_input_fields"], {"title": 6, "body": 6})
        retry, kw, _ = self.setup_job("no_memory", "40")
        failed = run_forms_solve(retry, **kw)
        self.assertIs(failed["official_success"], False)
        self.assertEqual(failed["official_score"], 0)
        self.assertNotEqual(succeeded["final_db_hash"], failed["final_db_hash"])
        messages = journal.blobs.get(succeeded["report_ref"])["messages"]
        self.assertTrue(any(m["role"] == "tool" for m in messages))
        self.assertNotIn("A public observation", canonical(messages).decode())

    def test_oversized_wire_retains_prepared_spans_without_http_or_official_score(self):
        body = RECORD.replace("Earlier public traveler message", "a" * 48000)
        self.limits["max_input_bytes"] = 35000
        journal, kw, http = self.setup_job("public_record", public_record=body, public_context_sha256=digest(body.encode()))
        result = run_forms_solve(journal, **kw)
        self.assertEqual(result["error_code"], "INPUT_SIZE_LIMIT", result)
        self.assertIsNone(result["official_score"])
        self.assertEqual(len(http.by_role["worker"]), 0)
        self.assertEqual(len(http.by_role["user_simulator"]), 1)
        self.assertEqual(result["memory_input_spans_by_delivery"]["not_submitted"], 2)
        self.assertEqual(result["normal_response_memory_spans"], 0)
        self.assertGreater(result["usage_by_role"]["worker"]["input_bytes_by_delivery"]["not_submitted"], 35000)
        self.assertFalse(result["unknown_cost"])
        audit_requests(journal.root)

    def test_source_mismatch_partial_bank_and_private_raw_record_refused_before_job_starts(self):
        for kind in ("source", "task", "rep", "arm", "partial", "private_record"):
            with self.subTest(kind=kind):
                stored = bank("fact", "3", ITEMS)
                journal, kw, http = self.setup_job("fact", writer_bank=stored)
                if kind == "source":
                    stored["public_context_sha256"] = "a" * 64
                elif kind == "task":
                    stored["task_id"] = "17"
                elif kind == "rep":
                    stored["repetition"] = 2
                elif kind == "arm":
                    stored["arm"] = "lesson"
                elif kind == "partial":
                    stored["status"] = "error"
                else:
                    leaked = json.loads(RECORD)
                    leaked["messages"][0]["private_goal"] = "private"
                    wire = canonical(leaked).decode()
                    kw.update(arm="public_record", writer_bank=None, public_record=wire, public_context_sha256=digest(wire.encode()))
                with self.assertRaises(ContractError):
                    run_forms_solve(journal, **kw)
                self.assertEqual(journal.scan["records"], [])
                self.assertEqual(http.wires, [])

    def test_shared_guard_unknown_cost_and_provider_identity_leave_an_ungraded_episode(self):
        def wrong_provider(raw, role):
            if role == "user_simulator":
                raw["provider"] = "Other"

        def wrong_model(raw, role):
            if role == "worker":
                raw["model"] = "other-model"

        def missing_cost(raw, role):
            raw["usage"].pop("cost")

        for change, expected, unknown in ((wrong_provider, 1, False), (wrong_model, 2, False), (missing_cost, 1, True)):
            with self.subTest(change=change.__name__):
                http = NativeHTTP(change_response=change)
                journal, kw, _ = self.setup_job("empty_preload", http=http)
                result = run_forms_solve(journal, **kw)
                self.assertEqual(len(http.wires), expected)
                self.assertEqual(self.guard.unknown_cost, unknown)
                self.assertEqual(result["unknown_cost"], unknown)
                self.assertEqual(result["grading_status"], "incomplete")
                self.assertIsNone(result["official_success"])
                self.assertIsNone(result["report_ref"])
                self.assertIsNotNone(result["partial_trajectory_ref"])
                self.assertEqual(audit_requests(journal.root)["unknown_cost_requests"], int(unknown))
        self.assertEqual(self.guard.calls, 4)

    def test_cost_unknown_on_the_terminal_reply_retains_the_official_grade(self):
        def final_unknown(raw, role):
            if role == "user_simulator" and raw["choices"][0]["message"]["content"] == "###STOP###":
                raw["usage"] = None
        journal, kw, http = self.setup_job("empty_preload", http=NativeHTTP(change_response=final_unknown))
        result = run_forms_solve(journal, **kw)
        self.assertEqual(len(http.wires), 3)
        self.assertEqual(result["grading_status"], "graded")
        self.assertIs(result["official_success"], True)
        self.assertTrue(result["unknown_cost"])
        self.assertTrue(self.guard.unknown_cost)
        self.assertEqual(result["usage_by_role"]["user_simulator"]["unknown_token_requests"], 1)

    def test_completed_native_dialogue_with_failed_grading_retains_its_termination_without_a_score(self):
        journal, kw, _ = self.setup_job("no_memory")
        with patch("tau2.runner.simulation.evaluate_simulation", side_effect=ValueError("mock scoring error")):
            result = run_forms_solve(journal, **kw)
        self.assertEqual(result["termination_reason"], "user_stop")
        self.assertEqual(result["grading_status"], "error")
        self.assertIsNone(result["official_score"])
        self.assertIsNone(result["report_ref"])
        self.assertGreater(len(journal.blobs.get(result["partial_trajectory_ref"])), 1)
        self.assertEqual(journal.scan["incomplete_runs"], [])

    def test_native_premature_zero_is_distinct_from_a_completed_official_failure(self):
        self.limits["native_steps"] = 1
        journal, kw, _ = self.setup_job("no_memory")
        result = run_forms_solve(journal, **kw)
        self.assertEqual(result["termination_reason"], "max_steps", result)
        self.assertEqual(result["native_reward"], 0)
        self.assertIsNotNone(result["native_report_ref"])
        self.assertIsNone(journal.blobs.get(result["reached_state_assessment_ref"])["official_success"])
        self.assertIsNone(result["official_score"])
        self.assertIsNone(result["official_success"])
        self.assertEqual(result["grading_status"], "incomplete")
        self.assertEqual(read_experiment(journal.root)[0]["incomplete_runs"], [])

    def test_role_and_aggregate_caps_apply_across_both_injected_http_clients(self):
        self.limits["max_total_requests_per_episode"] = 2
        journal, kw, http = self.setup_job("no_memory")
        kw.pop("opener")
        clients = {r: OpenRouterClient(self.models[r], self.guard, opener=http, max_input_bytes=self.limits["max_input_bytes"],
                                      route=kw["connection"][r]["provider"]) for r in self.models}
        result = run_forms_solve(journal, **kw, clients=clients)
        self.assertEqual(result["error_code"], "EPISODE_REQUEST_LIMIT")
        self.assertEqual((result["http_calls"], self.guard.calls, len(http.wires)), (2, 2, 2))
        self.assertTrue(all(c.guard is self.guard for c in clients.values()))
        self.limits["worker_requests"] = 0
        second, kw, http = self.setup_job("fact", writer_bank=bank("fact", "3", ITEMS))
        result = run_forms_solve(second, **kw)
        self.assertEqual(result["error_code"], "ROLE_REQUEST_LIMIT")
        self.assertIsNone(result["official_score"])
        self.assertEqual(len(http.by_role["worker"]), 0)
        self.assertEqual(self.guard.calls, 3)

    def test_unconfirmed_delivery_separates_unknown_cost_and_exposure(self):
        def disconnect(raw, role):
            if role == "worker":
                raise URLError("mock disconnect")
        journal, kw, _ = self.setup_job("fact", http=NativeHTTP(change_response=disconnect),
                                        writer_bank=bank("fact", "3", ITEMS))
        result = run_forms_solve(journal, **kw)
        self.assertEqual(result["error_code"], "TRANSPORT_UNKNOWN")
        self.assertEqual(result["memory_input_spans_by_delivery"]["unknown"], 4)
        self.assertEqual(result["normal_response_memory_spans"], 0)
        self.assertTrue(result["unknown_cost"])
        self.assertTrue(self.guard.unknown_cost)
        self.assertEqual(result["reported_cost_usd_decimal"], "0.0001")
        self.assertIsNone(result["official_success"])

    def test_oversized_read_bank_and_failed_environment_initialization_are_closed_without_sending(self):
        self.limits["read_bank_return_bytes"] = 8
        journal, kw, http = self.setup_job("empty_preload")
        rejected = run_forms_solve(journal, **kw)
        self.assertEqual(rejected["error_code"], "SNAPSHOT_BUDGET_EXCEEDED")
        self.assertEqual(rejected["bank_sizes"], {"items": 0, "payload_bytes": 0, "return_bytes": 12})
        self.assertTrue(journal.state["banks"][rejected["bank_id"]]["sealed"])
        self.assertIsNone(rejected["official_score"])
        self.assertEqual(http.wires, [])
        self.assertEqual(journal.scan["incomplete_runs"], [])
        second, kw, http = self.setup_job("no_memory")
        with patch("agent_memory_eval.experiments.forms_solve.fresh_orchestrator", side_effect=RuntimeError("native fixture")):
            unavailable = run_forms_solve(second, **kw)
        self.assertEqual(unavailable["error_code"], "RuntimeError")
        self.assertEqual(unavailable["http_calls"], 0)
        self.assertIsNone(unavailable["final_db_hash"])
        self.assertIsNone(unavailable["official_score"])
        self.assertEqual(second.scan["incomplete_runs"], [])

if __name__ == "__main__":
    unittest.main()
