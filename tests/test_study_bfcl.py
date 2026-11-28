import json
from pathlib import Path
import tempfile
import unittest

from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.study.plan import FORMAT, LEGACY_FORMAT, STANDARD_MODELS

SETUP = "protocols/diagnostics/bfcl-memory-cohort-transfer-v1.json"
ON_DEMAND_NOTICE = "Use memory tools when a prior note could help with this task."
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
    def test_removing_custom_tool_wording_changes_the_reader_request(self):
        from agent_memory_eval.memory.tools import memory_tools
        from agent_memory_eval.runner.bfcl_relevance import read_tools, worker_instruction
        from agent_memory_eval.study.adapters.bfcl import BfclRelevance

        plan = fixture_plan()
        adapter = BfclRelevance()
        custom = adapter.compile(plan, ROOT)
        del plan["reading"]["on_demand"]["tool_descriptions"]
        default = adapter.compile(plan, ROOT)
        self.assertEqual(worker_instruction(default, "on_demand"), worker_instruction(custom, "on_demand"))
        self.assertNotEqual(read_tools(default), read_tools(custom))
        self.assertEqual(read_tools(default), memory_tools(["memory_list", "memory_get"], page_size=1))

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


if __name__ == "__main__":
    unittest.main()
