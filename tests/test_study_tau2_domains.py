import os
from pathlib import Path

# Bind tau2 to the pinned checkout before any module imports it with its default data directory.
os.environ.setdefault("TAU2_DATA_DIR", str((Path(__file__).resolve().parents[1] / "data/tau2-bench/data").resolve()))

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from functools import partial
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments import forms_batch
from agent_memory_eval.models.openrouter import OpenRouterClient
from test_study_bank import IDENTITY, QUERY, WRITER, endpoint_models, ref, scripted_reply

BASE = "protocols/pilots/tau2-fresh-matched-v1.json"


def domain_plan(adapter, setup, task, plan_id):
    lines = json.loads((ROOT / "protocols/pilots/tau-banking-required-v1.json").read_bytes())["lines"]
    query = json.loads((ROOT / QUERY).read_bytes())["arms"]["contextual"]["read_tool_descriptions"]
    return dict(
        format="agent-memory-study-plan/v1", id=plan_id, purpose="development",
        question="Does the shared τ² path run this domain end to end?",
        benchmark=dict(adapter=adapter, setup=ref(setup), tasks=[task]),
        first_pass=dict(draws=1),
        writing=dict(instruction=ref(WRITER), feedback="result_only", representations={"fact": dict(text=lines["fact"])}),
        reading={"preload": dict(read_mode="preload", notice=ref("protocols/instructions/matched-memory-read-v1.txt")),
                 "on_demand": dict(read_mode="on_demand", tool_descriptions=query)},
        conditions=[dict(representation="fact", reading="preload"), dict(representation="fact", reading="on_demand")],
        repeats=1, budget=dict(max_usd="1"), retry=dict(http_codes=[503], wait_seconds=[0]),
    )


def judge_model():
    terms = json.loads((ROOT / BASE).read_bytes())["endpoint_terms"]["grader"]
    return dict(id=terms["model"], canonical_slug="openai/gpt-5.4-mini-20260317",
                selected_endpoint=dict(tag=terms["provider"], provider_name=terms["provider_name"], pricing=terms["pricing"]),
                reservation_pricing=terms["reservation_pricing"])


class Tau2DomainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / "data").mkdir()
        (self.workspace / "data/tau2-bench").symlink_to((ROOT / "data/tau2-bench").resolve(), target_is_directory=True)
        shutil.copytree(ROOT / "protocols", self.workspace / "protocols")

    def tearDown(self):
        self.tmp.cleanup()

    def run_domain(self, adapter_name, setup, task, resolver):
        from agent_memory_eval.study.adapters import adapter_for
        from agent_memory_eval.study.adapters.tau_banking import approval_for
        from agent_memory_eval.study.scorecard import scorecard
        adapter = adapter_for(adapter_name)
        plan = domain_plan(adapter_name, setup, task, adapter_name + "-fixture-01")
        record = adapter.resolve(plan, self.workspace, ROOT)["record"]
        approval = approval_for(plan, record, max_usd="1", user_authorization="Scripted fixture with network disabled")
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, OPENROUTER_API_KEY="in-process-fixture"))
            stack.enter_context(patch.object(forms_batch, "OpenRouterClient", partial(OpenRouterClient, opener=scripted_reply)))
            done = adapter.run(plan, self.workspace, ROOT, approval=approval, identity=IDENTITY, model_resolver=resolver,
                               sleep=lambda s: None, pool_factory=lambda n: ThreadPoolExecutor(1))
        return record, done, scorecard(adapter.outcomes(plan, self.workspace, ROOT))

    def test_airline_runs_through_the_shared_path(self):
        record, done, card = self.run_domain("tau2-airline", "protocols/studies/setups/tau2-airline-v1.json", "0",
                                             endpoint_models)
        self.assertEqual((record["domain"], record["split"]), ("airline", "train"))
        self.assertEqual(record["assigned"], dict(source=1, reused_first_pass=0, write=1, solve=2))
        self.assertEqual(set(card["conditions"]), {"no_memory", "fact__preload", "fact__on_demand"})
        self.assertEqual(card["conditions"]["no_memory"]["graded"], 1)
        self.assertEqual(card["conditions"]["fact__preload"]["preloaded"], card["conditions"]["fact__preload"]["graded"])

    def test_retail_binds_the_native_judge(self):
        resolver = lambda cfg, conn: (endpoint_models(cfg, conn)[0], judge_model())
        record, done, card = self.run_domain("tau2-retail", "protocols/studies/setups/tau2-retail-v1.json", "0", resolver)
        self.assertEqual(record["domain"], "retail")
        self.assertEqual(card["conditions"]["no_memory"]["graded"], 1)
        self.assertEqual(card["conditions"]["fact__on_demand"]["assigned"], 1)

    def test_retail_refuses_to_run_without_its_judge(self):
        with self.assertRaisesRegex(ContractError, "judge endpoint terms changed"):
            self.run_domain("tau2-retail", "protocols/studies/setups/tau2-retail-v1.json", "0", endpoint_models)

    def test_command_line_resolves_every_tau2_domain(self):
        import subprocess
        import sys
        for adapter, setup, task in (("tau2-airline", "protocols/studies/setups/tau2-airline-v1.json", "0"),
                                     ("tau2-retail", "protocols/studies/setups/tau2-retail-v1.json", "2")):
            path = self.workspace / f"{adapter}.json"
            path.write_text(json.dumps(domain_plan(adapter, setup, task, adapter + "-cli-01")), encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "TAU2_DATA_DIR"}
            done = subprocess.run([sys.executable, "-m", "agent_memory_eval.study", "plan", "resolve", "--plan", str(path),
                                   "--workspace", str(self.workspace)], capture_output=True, text=True, env=env, cwd=ROOT)
            with self.subTest(adapter=adapter):
                self.assertEqual(done.returncode, 0, done.stderr[-1500:])
                self.assertEqual(json.loads(done.stdout)["assigned"], dict(source=1, reused_first_pass=0, write=1, solve=2))

    def test_step_capped_episode_is_an_official_failure(self):
        from agent_memory_eval.study.adapters.tau_banking import outcome_fields
        self.assertEqual(outcome_fields(dict(success=None, status="error", reason="NATIVE_MAX_STEPS")),
                         ("graded", False, None, True))
        self.assertEqual(outcome_fields(dict(success=None, status="error", reason="HTTP_503")),
                         ("missing", None, "HTTP_503", False))
        self.assertEqual(outcome_fields(dict(success=True, status="completed", reason=None)), ("graded", True, None, False))

    def test_task_outside_the_split_is_rejected(self):
        from agent_memory_eval.study.adapters import adapter_for
        plan = domain_plan("tau2-airline", "protocols/studies/setups/tau2-airline-v1.json", "19", "airline-split-01")
        with self.assertRaisesRegex(ContractError, "not in the tau2-airline setup's split"):
            adapter_for("tau2-airline").resolve(plan, self.workspace, ROOT)


if __name__ == "__main__":
    unittest.main()
