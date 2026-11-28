import json
from pathlib import Path
import tempfile
import unittest

from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.study.plan import FORMAT, LEGACY_FORMAT, STANDARD_MODELS
from replay_policy_round import ON_DEMAND_NOTICE, LocalFixture

SETUP = "protocols/diagnostics/bfcl-memory-cohort-transfer-v1.json"
CHECKOUT = ROOT / ".local/benchmark-portability/bfcl-6ea5797"
CHECKPOINT = Path.home() / ".cache/huggingface/hub/models--Qwen--Qwen3.5-4B/snapshots/851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a"
TASKS = ["live_relevance_0-0-0", "irrelevance_1"]


def fixture_plan():
    setup = json.loads((ROOT / SETUP).read_bytes())
    local = "tests/fixtures/study-local-qwen-profile.json"
    return dict(
        format=FORMAT, id="bfcl-fixture-01", purpose="development",
        models=dict(profile=dict(path=local, sha256=digest((ROOT / local).read_bytes())), comparison=True),
        question="Does a new declared representation run through the shared BFCL path?",
        benchmark=dict(adapter="bfcl-relevance", setup=dict(path=SETUP, sha256=digest((ROOT / SETUP).read_bytes())),
                       tasks=TASKS),
        first_pass=dict(draws=1),
        writing=dict(instruction=dict(text=setup["writer_instruction"]), feedback="result_only", representations={
            "fact": dict(text=setup["forms"]["fact"]),
            "outcome-first": dict(text="Begin with one line that states the prior outcome. Then record the facts."),
        }),
        reading={
            "preload": dict(read_mode="preload", notice=dict(text=setup["memory_notice_by_delivery"]["preload"])),
            "on_demand": dict(read_mode="on_demand", notice=dict(text=ON_DEMAND_NOTICE),
                              tool_descriptions=setup["read_tool_descriptions"]),
        },
        conditions=[dict(representation="outcome-first", reading="preload"),
                    dict(representation="fact", reading="on_demand")],
        repeats=1, budget=dict(max_usd="0"), retry=dict(http_codes=[], wait_seconds=[]),
    )


class BfclCompileTests(unittest.TestCase):
    def test_profile_carries_only_the_declared_policies(self):
        from agent_memory_eval.study.adapters.bfcl import BfclRelevance
        cfg = BfclRelevance().compile(fixture_plan(), ROOT)
        self.assertEqual(cfg["name"], "bfcl-fixture-01")
        self.assertEqual(cfg["tasks"], {"live_relevance": ["live_relevance_0-0-0"], "irrelevance": ["irrelevance_1"]})
        self.assertEqual(set(cfg["forms"]), {"fact", "outcome-first"})
        self.assertEqual(cfg["conditions"], [["outcome-first", "preload"], ["fact", "on_demand"]])
        self.assertEqual(cfg["memory_notice_by_delivery"]["on_demand"], ON_DEMAND_NOTICE)
        self.assertEqual(cfg["writer_feedback"], "result_only")

    def test_first_pass_only_profile_has_no_writers_or_readers(self):
        from agent_memory_eval.study.adapters.bfcl import BfclRelevance
        plan = {k: v for k, v in fixture_plan().items() if k not in ("writing", "reading")}
        plan["conditions"] = []
        cfg = BfclRelevance().compile(plan, ROOT)
        self.assertEqual((cfg["forms"], cfg["delivery"], cfg["conditions"]), ({}, [], []))

    def test_task_outside_the_setup_is_rejected(self):
        from agent_memory_eval.study.adapters.bfcl import BfclRelevance
        plan = fixture_plan()
        plan["benchmark"]["tasks"] = ["simple_0"]
        with self.assertRaisesRegex(ContractError, "not in the benchmark setup"):
            BfclRelevance().compile(plan, ROOT)

    def test_standard_worker_and_legacy_run_stop_before_local_inference(self):
        from agent_memory_eval.study.adapters.bfcl import BfclRelevance
        plan = fixture_plan()
        plan["models"] = dict(profile=dict(path=STANDARD_MODELS, sha256=digest((ROOT / STANDARD_MODELS).read_bytes())))
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            for kind, error in ((FORMAT, "declared worker is openai/gpt-6-luna"),
                                (LEGACY_FORMAT, "Declare the models under study")):
                plan["format"] = kind
                if kind == LEGACY_FORMAT:
                    del plan["models"]
                with self.subTest(kind=kind), self.assertRaisesRegex(ContractError, error):
                    BfclRelevance().run(plan, ws, ROOT, checkout=ws, model_python=ws, checkpoint=ws)
            self.assertFalse((ws / "runs").exists())


@unittest.skipUnless(CHECKOUT.is_dir() and CHECKPOINT.is_dir(), "Pinned BFCL checkout and checkpoint required")
class BfclRunTests(unittest.TestCase):
    def test_declared_round_runs_and_scores_without_new_code(self):
        from agent_memory_eval.study.adapters.bfcl import BfclRelevance
        from agent_memory_eval.study.scorecard import scorecard
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = BfclRelevance()
            plan = fixture_plan()
            adapter.run(plan, workspace, ROOT, checkout=CHECKOUT, model_python=Path("python"), checkpoint=CHECKPOINT,
                        client_factory=LocalFixture)
            rows = adapter.outcomes(plan, workspace, ROOT, checkout=CHECKOUT)
            card = scorecard(rows)
            self.assertEqual(set(card["conditions"]), {"no_memory", "outcome-first__preload", "fact__on_demand"})
            self.assertEqual(card["sources"], 2)
            for name in ("outcome-first__preload", "fact__on_demand"):
                self.assertEqual(card["conditions"][name]["assigned"], 2)
            self.assertEqual(card["conditions"]["outcome-first__preload"]["preloaded"],
                             card["conditions"]["outcome-first__preload"]["graded"])
            self.assertGreater(card["conditions"]["fact__on_demand"]["reads"]["list_calls"], 0)
            stored = [r for r in rows if r["condition"] == "outcome-first__preload"]
            self.assertTrue(all(r["memory_body"] == "The worker asked for clarification." for r in stored))
            self.assertEqual({r["first_decision"] for r in rows if r["status"] == "graded"}, {"no_call"})
            folder = workspace / "runs/bfcl-fixture-01"
            self.assertTrue((folder / "registration.json").is_file())
            changed = dict(plan, question="Another question")
            with self.assertRaisesRegex(ContractError, "registered with another plan"):
                adapter.outcomes(changed, workspace, ROOT, checkout=CHECKOUT)
            with self.assertRaisesRegex(ContractError, "different contents"):
                adapter.run(changed, workspace, ROOT, checkout=CHECKOUT, model_python=Path("python"),
                            checkpoint=CHECKPOINT, client_factory=LocalFixture)
            # Preserve a graded first task while the other has no journal, then stop inside the first task.
            (folder / TASKS[1]).rename(workspace / "untouched-fixture")
            partial = adapter.outcomes(plan, workspace, ROOT, checkout=CHECKOUT)
            self.assertEqual([r["missing_reason"] for r in partial if r["task"] == TASKS[1]], ["not_started"] * 3)
            self.assertEqual([r for r in partial if r["task"] == TASKS[0]], [r for r in rows if r["task"] == TASKS[0]])
            log = folder / TASKS[0] / "log.jsonl"
            lines = log.read_bytes().splitlines(keepends=True)
            names = {"-fact-on_demand": "fact__on_demand", "-outcome-first-preload": "outcome-first__preload"}
            final, interrupted = max((i, condition) for i, line in enumerate(lines) for e in json.loads(line)["events"]
                                     for suffix, condition in names.items()
                                     if e["type"] == "run.started" and e["run_id"].endswith(suffix))
            log.write_bytes(b"".join(lines[:final + 1]))
            partial = adapter.outcomes(plan, workspace, ROOT, checkout=CHECKOUT)
            counts = {r["condition"]: r for r in partial if r["task"] == TASKS[0]}
            self.assertEqual(counts[interrupted]["missing_reason"], "interrupted")
            closed = [r for r in rows if r["task"] == TASKS[0] and r["condition"] != interrupted]
            self.assertEqual([counts[r["condition"]] for r in closed], closed)


if __name__ == "__main__":
    unittest.main()
