from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.forms_results import checked_rows
from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import ordered_jobs
from agent_memory_eval.experiments.prompt_comparison import compile_config, run_comparison
from agent_memory_eval.models.openrouter import OpenRouterClient
from test_forms_solve import AVAILABLE, NativeHTTP
from test_openrouter import Reply, response

CONFIG = ROOT / "protocols/pilots/tau2-fact-recheck-v1.json"


class PromptComparisonTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(CONFIG.read_bytes())

    def test_independent_names_prompts_and_repetitions(self):
        design = compile_config(ROOT, self.config)
        jobs = ordered_jobs(design)
        self.assertEqual(len(jobs), 48)
        self.assertEqual([x["phase"] for x in jobs], ["write"] * 16 + ["solve"] * 32)
        self.assertEqual({c["representation"] for c in design["conditions"]}, {"fact"})
        self.assertEqual(len({c["writer_instruction_sha256"] for c in design["conditions"]}), 2)
        altered = deepcopy(design)
        altered["allocation"]["blocks"][0]["write"].reverse()
        with self.assertRaises(ContractError):
            ordered_jobs(altered)

    def test_collisions_and_customer_overlap_are_rejected(self):
        for change in ("no_memory", "fact_original", "same_text", "group"):
            config = deepcopy(self.config)
            if change == "group":
                config["task_sets"] = {"development": ["3"], "evaluation": ["40"]}
            elif change == "same_text":
                config["conditions"][1]["prompt"] = config["conditions"][0]["prompt"]
            else:
                config["conditions"][1]["id"] = change
            with self.subTest(change=change), self.assertRaises(ContractError):
                compile_config(ROOT, config)

    @unittest.skipUnless(AVAILABLE and (ROOT / "runs/tau2-baseline-luna-deepseek1017-train-01").exists(),
                         "Requires the pinned airline benchmark and archived source")
    def test_normal_entrypoint_reaches_both_writers_and_real_native_scorer(self):
        from agent_memory_eval.experiments.forms_batch import prepare
        config = self.config
        config["task_sets"] = {"development": [], "evaluation": ["3"]}
        config["seed_pairs"] = config["seed_pairs"][:1]
        config["conditions"][1]["prompt"] += "\nKeep observations distinct."
        config["conditions"][1]["id"] = "custom_fact"
        design = compile_config(ROOT, config)
        plan = prepare(ROOT, ROOT, ROOT / "data/tau2-bench", design)
        models = json.loads((ROOT / config["endpoint_ref"]["path"]).read_bytes())["models"]
        writer_wires, solvers = [], NativeHTTP()
        original_init = OpenRouterClient.__init__
        def local_open(request, timeout):
            wire = json.loads(request.data)
            if any(t["function"]["name"] == "memory_store" for t in wire.get("tools", [])):
                writer_wires.append(wire)
                raw = response()
                raw.update(model=wire["model"], provider="OpenAI")
                return Reply(raw)
            # The scripted customer starts and ends each independent native episode.
            if wire["model"].startswith("deepseek/") and len(wire["messages"]) == 1:
                solvers.by_role["user_simulator"].clear()
            return solvers(request, timeout)
        def inject(client, *args, **kwargs):
            original_init(client, *args, opener=local_open, **kwargs)
        ns = "agent_memory_eval.experiments.prompt_comparison."
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "runs").mkdir()
            cfg = workspace / "config.json"
            cfg.write_text(json.dumps(config))
            with patch.dict("os.environ", OPENROUTER_API_KEY="local-mock"), patch(ns + "prepare", return_value=plan), \
                    patch(ns + "implementation_identity", return_value={"implementation_commit": "fixture"}), \
                    patch(ns + "live_models", return_value=models), patch.object(OpenRouterClient, "__init__", inject):
                run_comparison(ROOT, workspace, ROOT / "data/tau2-bench", cfg, live=True)
                with self.assertRaises(ContractError):
                    run_comparison(ROOT, workspace, ROOT / "data/tau2-bench", cfg, live=True)
            _, _, rows, _ = checked_rows(workspace / "runs" / config["name"])
            self.assertEqual(len(writer_wires), 2)
            self.assertEqual({digest(w["messages"][0]["content"].encode()) for w in writer_wires},
                             {c["writer_instruction_sha256"] for c in design["conditions"]})
            self.assertTrue(all(r["status"] == "empty" for r in rows if r["phase"] == "write"))
            self.assertTrue(all(r["result"]["grading_status"] == "graded" for r in rows if r["phase"] == "solve"))
