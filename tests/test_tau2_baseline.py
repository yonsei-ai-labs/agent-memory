"""No-memory screening contracts and actual native routing using mock HTTP only."""

from collections import Counter
from copy import deepcopy
from contextlib import redirect_stderr
import importlib.util
import io
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.tau2_baseline import audit_baseline, summarize_baseline
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.models.openrouter import ProviderError, SpendGuard
from agent_memory_eval.recording.journal import Journal, read_experiment
from agent_memory_eval.runner.tau2_baseline import (
    ScopedGuard, allocations, build_baseline_orchestrator, compile_baseline, preflight_baseline,
    prepare_baseline, resolved_connection, run_baseline,
)
from test_openrouter import MODEL, Reply, response


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "protocols/drafts/tau2-baseline-screening-v1.json"
AVAILABLE = importlib.util.find_spec("tau2") is not None and (ROOT / "data/tau2-bench/.git").is_dir()


def ready_profile():
    """Only a fake-HTTP test allocation; no live model budget is authorized here."""
    profile = read_json(PROFILE.read_bytes())
    profile.update(status="ready", selection=dict(mode="explicit_train", task_ids=["0", "40"]))
    profile["guards"] = dict(max_model_requests=100, max_reported_cost_usd=2, max_seconds=300)
    return profile


class BaselineCompileTests(unittest.TestCase):
    def test_draft_can_be_inspected_but_cannot_prepare_paid_calls(self):
        profile = read_json(PROFILE.read_bytes())
        plan = compile_baseline(profile, require_ready=False)
        self.assertFalse(plan["ready"])
        self.assertEqual(plan["unresolved_fields"], ["guards.max_reported_cost_usd"])
        with patch("agent_memory_eval.runner.tau2_baseline.model_catalog") as catalog:
            with self.assertRaisesRegex(ContractError, "not ready"):
                prepare_baseline(profile, ROOT / "data/tau2-bench")
            catalog.assert_not_called()

    def test_changed_assets_seeds_selection_and_unbounded_fields_are_rejected(self):
        for kind in ("hash", "seeds", "selection", "empty", "duplicate", "unknown", "unbounded", "zero", "infinite"):
            profile = ready_profile()
            if kind == "hash": profile["connection_profile_ref"]["sha256"] = "0" * 64
            if kind == "seeds": profile["native_seeds"] = [0]
            if kind == "selection": profile["selection"]["mode"] = "all_train"
            if kind == "empty": profile["selection"]["task_ids"] = []
            if kind == "duplicate": profile["selection"]["task_ids"] = ["0", "0"]
            if kind == "unknown": profile["memory_instruction"] = "Read a memory first"
            if kind == "unbounded": profile["run_guards"]["max_seconds"] = None
            if kind == "zero": profile["guards"]["max_reported_cost_usd"] = 0
            if kind == "infinite": profile["guards"]["max_reported_cost_usd"] = float("inf")
            with self.subTest(kind=kind), self.assertRaises(ContractError):
                compile_baseline(profile)

    def test_allocation_covers_each_task_per_repetition_and_records_seed_overrides(self):
        profile = ready_profile()
        plan = compile_baseline(profile)
        rows = allocations(profile, ["0", "40", "17"])
        self.assertEqual(rows, allocations(profile, ["0", "40", "17"]))
        self.assertEqual(Counter((r["repetition"], r["task_id"]) for r in rows),
                         Counter({(rep, task): 1 for rep in (1, 2) for task in ("0", "40", "17")}))
        self.assertEqual(len({r["allocation_id"] for r in rows}), 6)
        row = next(r for r in rows if r["native_seed"] == 1)
        config = resolved_connection(plan, row)
        self.assertEqual(config["benchmark"]["max_steps"], 200)
        self.assertEqual(config["worker"]["parameters"]["seed"], 1)
        self.assertNotIn("seed", config["user_simulator"]["parameters"])
        self.assertEqual(plan["connection"]["benchmark"]["max_steps"], 40)

    @unittest.skipUnless(AVAILABLE, "Pinned tau2 checkout required")
    def test_preflight_checks_all_train_without_catalog_and_rejects_test_tasks(self):
        with patch("agent_memory_eval.runner.tau2_baseline.model_catalog") as catalog:
            plan, _ = preflight_baseline(read_json(PROFILE.read_bytes()), ROOT / "data/tau2-bench")
            self.assertEqual(len(plan["task_ids"]), 30)
            self.assertEqual(len(plan["allocations"]), 60)
            self.assertEqual(set(plan["task_ids"]), set(plan["split"]["train"]))
            self.assertFalse(set(plan["task_ids"]) & set(plan["split"]["test"]))
            profile = ready_profile()
            profile["selection"]["task_ids"] = ["2"]
            with self.assertRaisesRegex(ContractError, "train split"):
                preflight_baseline(profile, ROOT / "data/tau2-bench", require_ready=True)
            catalog.assert_not_called()

    def test_guard_rejections_do_not_consume_calls_in_the_other_scope(self):
        request = dict(messages=[dict(role="user", content="hello")], max_tokens=5)
        for failing_scope in ("RUN", "BATCH"):
            for attribute, value, code in (("calls", 2, "CALL_LIMIT"), ("max_usd", 1e-10, "SPEND_LIMIT"), ("deadline", 0, "TIME_LIMIT")):
                run = SpendGuard(max_calls=2, max_usd=1, max_seconds=30)
                batch = SpendGuard(max_calls=2, max_usd=1, max_seconds=30)
                guard = ScopedGuard(batch, run)
                setattr(run if failing_scope == "RUN" else batch, attribute, value)
                before = (run.calls, batch.calls)
                with self.subTest(scope=failing_scope, limit=code), self.assertRaisesRegex(ProviderError, failing_scope + "_" + code):
                    guard.before(request, MODEL)
                self.assertEqual((run.calls, batch.calls), before)

    def test_live_cli_rejects_nonempty_output_before_metadata_or_model_calls(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "profile.json"
            config.write_bytes(canonical(ready_profile()))
            output = root / "previous-run"
            output.mkdir()
            (output / "sentinel").write_text("preserve")
            args = ["run_tau2_baseline.py", "--live", "--config", str(config), "--output", str(output)]
            with patch("sys.argv", args), redirect_stderr(io.StringIO()), \
                    patch("agent_memory_eval.runner.tau2_baseline.prepare_baseline") as prepare:
                with self.assertRaises(SystemExit) as caught:
                    runpy.run_path(str(ROOT / "scripts/run_tau2_baseline.py"), run_name="__main__")
                self.assertEqual(caught.exception.code, 2)
                prepare.assert_not_called()
            self.assertEqual((output / "sentinel").read_text(), "preserve")


@unittest.skipUnless(AVAILABLE, "Install tau2 extra and pinned checkout for native tests")
class NativeBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = compile_baseline(ready_profile())["connection"]
        cls.catalog = dict(data=[dict(id=config[role]["model_id"], canonical_slug=config[role]["model_id"],
            supported_parameters=list(config[role]["parameters"]), pricing=dict(prompt="0.0000001", completion="0.0000001"))
            for role in ("worker", "user_simulator")])

    @staticmethod
    def endpoints(model_id):
        user = model_id.startswith("deepseek/")
        return dict(data=dict(id=model_id, endpoints=[dict(tag="fireworks" if user else "openai",
            provider_name="Fireworks" if user else "OpenAI", status=0,
            supported_parameters=["reasoning", "max_tokens", "max_completion_tokens", "temperature", "tools", "tool_choice"]
                + ([] if user else ["seed"]), pricing=dict(prompt="0.0000001", completion="0.0000001"))]))

    def execute(self, root, profile=None, *, unknown=False, interrupt=False):
        profile = ready_profile() if profile is None else profile
        with patch("agent_memory_eval.runner.tau2_baseline.model_catalog", return_value=self.catalog), \
                patch("agent_memory_eval.runner.tau2_baseline.model_endpoints", side_effect=self.endpoints):
            prepared = prepare_baseline(profile, ROOT / "data/tau2-bench")
        self.wires, self.environments, counts = [], [], Counter()
        def build(config, task):
            orch, native = build_baseline_orchestrator(config, task)
            self.environments.append(orch)
            return orch, native
        def opener(request, timeout):
            if interrupt:
                raise KeyboardInterrupt()
            wire = read_json(request.data)
            self.wires.append(wire)
            user = wire["model"].startswith("deepseek/")
            key = len(self.environments), user
            counts[key] += 1
            body = response(None if unknown else 0.0001)
            body.update(id="baseline-test-" + str(len(self.wires)), model=wire["model"], provider="Fireworks" if user else "OpenAI")
            message = dict(role="assistant", content=("Hello, I need help." if counts[key] == 1 else "###STOP###")
                           if user else "Please provide your details.")
            if not user and counts[key] == 1:
                message = dict(role="assistant", content=None, tool_calls=[dict(id="business-call", type="function",
                    function=dict(name="list_all_airports", arguments="{}"))])
                body["choices"][0]["finish_reason"] = "tool_calls"
            body["choices"][0]["message"] = message
            return Reply(body)
        with patch.dict("os.environ", OPENROUTER_API_KEY="baseline-mock-secret"), \
                patch("agent_memory_eval.runner.tau2_baseline.build_baseline_orchestrator", side_effect=build):
            with Journal(root, "baseline-test") as journal:
                summary = run_baseline(journal, profile, prepared, opener=opener)
                self.assertEqual(journal.state["provenance"], {})
        (Path(root) / "baseline-summary.json").write_bytes(canonical(summary))
        return summary

    def test_native_runs_are_isolated_seeded_unexposed_and_reauditable(self):
        with TemporaryDirectory() as folder:
            summary = self.execute(folder)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["counts"], dict(planned=4, started=4, graded=4, successful=2, skipped=0, ungraded=0, bounded=0))
            self.assertEqual(summary["http_calls"], 16)
            self.assertEqual(summary["reported_cost_usd_decimal"], "0.0016")
            self.assertEqual(summary["usage_by_role"]["worker"]["reported_cost_usd_decimal"], "0.0008")
            self.assertEqual(summary["usage_by_role"]["user_simulator"]["http_calls"], 8)
            self.assertEqual(len({r["session_id"] for r in summary["runs"]}), 4)
            for field in ("agent", "user", "environment", "agent_state", "user_state"):
                self.assertEqual(len({id(getattr(orch, field)) for orch in self.environments}), 4, field)
            self.assertEqual(len({id(orch.environment.tools.db) for orch in self.environments}), 4)
            workers = [w for w in self.wires if not w["model"].startswith("deepseek/")]
            users = [w for w in self.wires if w["model"].startswith("deepseek/")]
            self.assertEqual({w["seed"] for w in workers}, {0, 1})
            self.assertTrue(all("seed" not in w for w in users))
            self.assertTrue(all(len(w["tools"]) == 14 for w in workers))
            self.assertFalse(any(t["function"]["name"].startswith("memory_") for w in self.wires for t in w.get("tools", [])))
            self.assertTrue(any(m["role"] == "tool" for w in workers for m in w["messages"]))
            before = digest((Path(folder) / "log.jsonl").read_bytes())
            with patch("agent_memory_eval.runner.tau2_baseline.model_catalog", side_effect=AssertionError("audit used network")):
                audit = audit_baseline(folder)
            self.assertEqual(audit["status"], "audit_passed")
            self.assertEqual(digest((Path(folder) / "log.jsonl").read_bytes()), before)
            scan, state, _ = read_experiment(folder)
            self.assertFalse(state["provenance"])
            self.assertEqual(scan["pending_requests"], [])
            tampered = deepcopy(summary)
            tampered["runs"][0]["official_success"] = not tampered["runs"][0]["official_success"]
            (Path(folder) / "baseline-summary.json").write_bytes(canonical(tampered))
            with self.assertRaisesRegex(ContractError, "summary differs"):
                audit_baseline(folder)

    def test_run_request_limit_continues_the_remaining_allocations(self):
        profile = ready_profile()
        profile["run_guards"]["max_model_requests"] = 1
        with TemporaryDirectory() as folder:
            summary = self.execute(folder, profile)
            self.assertEqual(summary["counts"]["started"], 4)
            self.assertEqual(summary["counts"]["ungraded"], 4)
            self.assertEqual(summary["counts"]["skipped"], 0)
            self.assertEqual(summary["http_calls"], 4)
            self.assertEqual({r["error_code"] for r in summary["runs"]}, {"RUN_REQUEST_LIMIT"})
            audit_baseline(folder)

    def test_batch_call_limit_preserves_partial_run_and_unstarted_denominator(self):
        profile = ready_profile()
        profile["guards"]["max_model_requests"] = 2
        with TemporaryDirectory() as folder:
            summary = self.execute(folder, profile)
            self.assertEqual(summary["counts"]["planned"], 4)
            self.assertEqual(summary["counts"]["started"], 1)
            self.assertEqual(summary["counts"]["skipped"], 3)
            self.assertEqual(summary["http_calls"], 2)
            self.assertEqual(summary["runs"][0]["error_code"], "BATCH_CALL_LIMIT")
            audit_baseline(folder)

    def test_missing_cost_stops_the_entire_batch(self):
        with TemporaryDirectory() as folder:
            summary = self.execute(folder, unknown=True)
            self.assertTrue(summary["unknown_cost"])
            self.assertEqual(summary["http_calls"], 1)
            self.assertEqual(summary["counts"]["skipped"], 3)
            audit_baseline(folder)

    def test_native_step_limit_retains_official_score_and_marks_bounded_runs(self):
        profile = ready_profile()
        profile["benchmark_limits"]["max_steps"] = 1
        with TemporaryDirectory() as folder:
            summary = self.execute(folder, profile)
            self.assertEqual(summary["counts"]["graded"], 4)
            self.assertEqual(summary["counts"]["bounded"], 4)
            self.assertEqual({r["termination_reason"] for r in summary["runs"]}, {"max_steps"})
            audit_baseline(folder)

    def test_interrupt_preserves_pending_request_and_refuses_complete_audit(self):
        with TemporaryDirectory() as folder:
            with self.assertRaises(KeyboardInterrupt):
                self.execute(folder, interrupt=True)
            before = (Path(folder) / "log.jsonl").read_bytes()
            with self.assertRaisesRegex(ContractError, "incomplete"):
                summarize_baseline(folder)
            self.assertEqual((Path(folder) / "log.jsonl").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
