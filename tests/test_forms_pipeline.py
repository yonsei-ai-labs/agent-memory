"""Verify the actual cross-journal writer-to-solver handoff with local HTTP fixtures."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import canonical, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import BatchGuard, DESIGN, prepare, request_usage, solve_connection
from agent_memory_eval.experiments.forms_solve import run_forms_solve
from agent_memory_eval.experiments.forms_write import read_form_bank, run_form_writer
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner.tau2_baseline import build_baseline_orchestrator
from test_forms_solve import AVAILABLE, NativeHTTP
from test_forms_write import store
from test_openrouter import Reply, response


@unittest.skipUnless(AVAILABLE and (ROOT / "runs/tau2-baseline-luna-deepseek1017-train-01/log.jsonl").is_file(),
                     "Requires the pinned native checkout and archived first pass")
class FormsPipelineTests(unittest.TestCase):
    def test_archived_source_to_verified_writer_bank_to_fresh_native_episode(self):
        design = read_json((ROOT / DESIGN).read_bytes())
        plan = prepare(ROOT, ROOT, ROOT / "data/tau2-bench", design)
        task, arm, rep = "3", "procedure", 1
        src = plan["sources"][task]
        cfg = solve_connection(plan["connection"], design, {"task_id": task, "repetition": rep}, 5)
        models = {r: dict(id=cfg[r]["model_id"], pricing=dict(prompt=0, completion=0),
            selected_endpoint=dict(tag=cfg[r]["provider"]["only"][0], provider_name="OpenAI" if r == "worker" else "Fireworks"))
            for r in ("worker", "user_simulator")}
        guard = BatchGuard(max_calls=10, max_usd=1, max_seconds=150)
        wires = []
        def writer_http(request, timeout):
            wires.append(json.loads(request.data))
            raw = response()
            raw.update(model=cfg["worker"]["model_id"], provider="OpenAI")
            if len(wires) == 1:
                raw["choices"] = store("handoff")["choices"]
            return Reply(raw)
        with TemporaryDirectory() as tmp, patch.dict("os.environ", OPENROUTER_API_KEY="test-local-transport-only"):
            root = Path(tmp)
            with Journal(root / "writer", "writer-local-http", contract_version="1.2") as j:
                result = run_form_writer(j, design, src, repetition=rep, arm=arm, root=ROOT, guard=guard,
                    run_id="assigned-writer", client=OpenRouterClient(models["worker"], guard, route=cfg["worker"]["provider"],
                    opener=writer_http, max_input_bytes=128000))
                self.assertEqual(request_usage(j)["writer"]["http_calls"], 2)
            self.assertEqual(wires[0]["messages"][1]["content"], src.public_context)
            self.assertEqual(len(wires), 2)
            bank = read_form_bank(root / "writer", expected_log_sha256=result["log_sha256"])
            self.assertEqual(bank["run_id"], "assigned-writer")
            prepared = (*build_baseline_orchestrator(cfg, plan["tasks"][task]), models, plan["modules"])
            http = NativeHTTP()
            with Journal(root / "solver", "solver-local-http", contract_version="1.2") as j:
                solved = run_forms_solve(j, arm=arm, task_id=task, repetition=rep, run_id="assigned-solver",
                    connection=cfg, prepared=prepared, guard=guard, limits=design["second_pass"]["proposed_limits"],
                    instruction=plan["instruction"], writer_bank=bank, public_context_sha256=src.public_context_sha256, opener=http)
            self.assertEqual(solved["grading_status"], "graded")
            self.assertEqual(solved["unknown_cost"], False)
            self.assertEqual(guard.calls, 2 + len(http.wires))
            for wire in http.by_role["worker"]:
                self.assertEqual(json.loads(wire["messages"][1]["content"]), {"items": bank["items"]})
                self.assertNotIn("assigned-writer", canonical(wire).decode())
            for wire in http.by_role["user_simulator"]:
                self.assertNotIn("A procedural note handoff", canonical(wire).decode())
            self.assertGreater(audit_requests(root / "solver")["normal_response_memory_spans"], 0)

    def test_production_controller_preserves_all_rows_after_unpriced_local_http(self):
        from shutil import copyfile
        from agent_memory_eval.analysis.forms_results import summarize
        from agent_memory_eval.contracts.core import digest
        from agent_memory_eval.experiments.forms_batch import run_batch
        design = read_json((ROOT / DESIGN).read_bytes())
        plan = prepare(ROOT, ROOT, ROOT / "data/tau2-bench", design)
        def ref(name):
            return dict(path=name, sha256=digest((ROOT / name).read_bytes()))
        endpoint = "artifacts/pilots/20261118-two-pass-forms-endpoints.json"
        models = read_json((ROOT / endpoint).read_bytes())["models"]
        original = OpenRouterClient.__init__
        attempts = []
        def opener(request, timeout):
            attempts.append(request.data)
            raw = response(cost=0.0001 if len(attempts) == 1 else None)
            raw.update(model=plan["connection"]["worker"]["model_id"], provider="OpenAI")
            return Reply(raw)
        def inject(client, *args, **kwargs):
            original(client, *args, opener=opener, **kwargs)
        with TemporaryDirectory() as folder:
            workspace = Path(folder)
            (workspace / endpoint).parent.mkdir(parents=True)
            copyfile(ROOT / endpoint, workspace / endpoint)
            (workspace / "runs").mkdir()
            profile = dict(format="agent-memory-two-pass-memory-forms-ready/v1", status="ready",
                implementation_commit="local-transport-fixture", endpoint_ref=ref(endpoint), design_ref=ref(DESIGN),
                research_ref=ref("docs/research/two-pass-memory-forms-v1.md"), output_root="runs/fixture",
                guards=dict(max_calls=10, max_usd=1, max_seconds=60))
            ns = "agent_memory_eval.experiments.forms_batch."
            with patch.dict("os.environ", OPENROUTER_API_KEY="never-a-live-key"), patch.object(OpenRouterClient, "__init__", inject), \
                    patch(ns + "implementation_identity", return_value=dict(implementation_commit="local-transport-fixture")), \
                    patch(ns + "prepare", return_value=plan), patch(ns + "live_models", return_value=models):
                done = run_batch(profile, workspace, ROOT / "data/tau2-bench")
            self.assertEqual(len(attempts), 2)
            self.assertEqual((done["http_calls"], done["unknown_cost"], done["stop_reason"]), (2, True, "unknown_cost"))
            audited = summarize(workspace / profile["output_root"])
            self.assertEqual(audited["assignment_statuses"], dict(empty=1, error=1, not_started=582))
            self.assertEqual(audited["paired_contrasts"][0]["possible_difference_pp"], [-100, 100])
