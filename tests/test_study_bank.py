import os
from pathlib import Path

# Bind tau2 to the pinned checkout before any module imports it with its default data directory.
os.environ.setdefault("TAU2_DATA_DIR", str((Path(__file__).resolve().parents[1] / "data/tau2-bench/data").resolve()))

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from functools import partial
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments import forms_batch
from agent_memory_eval.models.openrouter import OpenRouterClient
from replay_policy_round import ON_DEMAND_NOTICE

SETUP = "protocols/pilots/tau-banking-memory-v1.json"
WRITER = "protocols/instructions/required-experience-writer-v1.txt"
QUERY = "protocols/diagnostics/bfcl-tool-only-retrieval-v1.json"
TASK = "task_055"
IDENTITY = dict(implementation_commit="scripted-fixture")


def ref(path):
    return dict(path=path, sha256=digest((ROOT / path).read_bytes()))


def bank_plan(plan_id="bank-fixture-01", **changes):
    lines = json.loads((ROOT / "protocols/pilots/tau-banking-required-v1.json").read_bytes())["lines"]
    query = json.loads((ROOT / QUERY).read_bytes())["arms"]["contextual"]["read_tool_descriptions"]
    notice = dict(text=ON_DEMAND_NOTICE)
    plan = dict(
        format="agent-memory-study-plan/v1", id=plan_id, purpose="development",
        question="Does a declared representation run through the shared banking path?",
        benchmark=dict(adapter="tau-banking", setup=ref(SETUP), tasks=[TASK]),
        first_pass=dict(draws=1),
        writing=dict(instruction=ref(WRITER), feedback="result_only", representations={
            "fact": dict(text=lines["fact"]),
            "outcome-first": dict(text="Begin with one line that states the prior outcome. Then record the facts."),
        }),
        reading={"preload": dict(read_mode="preload", notice=notice),
                 "on_demand": dict(read_mode="on_demand", tool_descriptions=query)},
        conditions=[dict(representation="outcome-first", reading="preload"),
                    dict(representation="fact", reading="on_demand")],
        repeats=1, budget=dict(max_usd="1"), retry=dict(http_codes=[503], wait_seconds=[0]),
    )
    plan.update(changes)
    return plan


def endpoint_models(cfg, conn):
    base = json.loads((ROOT / json.loads((ROOT / SETUP).read_bytes())["base"]["path"]).read_bytes())
    models = json.loads((ROOT / base["endpoints"]["path"]).read_bytes())["models"]
    return {role: models[role] for role in ("worker", "user_simulator")}, None


def scripted_message(wire):
    """Store when writing; list then read when memory tools are offered; otherwise ask for clarification."""
    tools = [t["function"]["name"] for t in wire.get("tools", [])]
    name = None
    if tools == ["memory_store"]:
        name, args = "memory_store", dict(title="Prior attempt", body="Prior task outcome: failure. The worker asked for clarification.")
    elif "memory_list" in tools:
        issued = {c["function"]["name"]: c["id"] for m in wire["messages"] for c in m.get("tool_calls") or []}
        if "memory_list" not in issued:
            name, args = "memory_list", {}
        elif "memory_get" not in issued:
            listed = next(m for m in wire["messages"] if m.get("tool_call_id") == issued["memory_list"])
            name, args = "memory_get", {"id": json.loads(listed["content"])["items"][0]["id"]}
    if name is None:
        return dict(role="assistant", content="Could you clarify the request?")
    call = dict(id="fixture-" + name, type="function", function=dict(name=name, arguments=json.dumps(args)))
    return dict(role="assistant", content=None, tool_calls=[call])


def scripted_reply(request, timeout):
    from test_openrouter import Reply, response
    wire = json.loads(request.data)
    raw = response(cost=0)
    raw["model"] = wire["model"]
    msg = scripted_message(wire)
    if wire["model"].startswith("deepseek/"):
        raw["provider"] = "Fireworks"
        msg = dict(role="assistant", content="I need help with my bank account." if len(wire["messages"]) == 2 else "###STOP###")
    raw["choices"][0].update(message=msg, finish_reason="tool_calls" if msg.get("tool_calls") else "stop")
    return Reply(raw)


class FlakyCustomer:
    """Fail the first customer reply after any writer has run, once, with a provider 503."""

    def __init__(self, fail=True):
        self.writer_seen, self.pending = False, fail

    def __call__(self, request, timeout):
        wire = json.loads(request.data)
        if [t["function"]["name"] for t in wire.get("tools", [])] == ["memory_store"]:
            self.writer_seen = True
        if self.pending and self.writer_seen and wire["model"].startswith("deepseek/"):
            self.pending = False
            raise HTTPError(request.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"{}"))
        return scripted_reply(request, timeout)


class BankStudyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / "data").mkdir()
        (self.workspace / "data/tau2-bench").symlink_to((ROOT / "data/tau2-bench").resolve(), target_is_directory=True)
        shutil.copytree(ROOT / "protocols", self.workspace / "protocols")

    def tearDown(self):
        self.tmp.cleanup()

    def adapter(self):
        from agent_memory_eval.study.adapters.tau_banking import TauBanking
        return TauBanking()

    def approve(self, plan):
        from agent_memory_eval.study.adapters.tau_banking import approval_for
        return approval_for(plan, self.adapter().resolve(plan, self.workspace, ROOT)["record"],
                            max_usd="1", user_authorization="Scripted fixture with network disabled")

    def run_plan(self, plan, opener):
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, OPENROUTER_API_KEY="in-process-fixture"))
            stack.enter_context(patch.object(forms_batch, "OpenRouterClient", partial(OpenRouterClient, opener=opener)))
            return self.adapter().run(plan, self.workspace, ROOT, approval=self.approve(plan),
                                      identity=IDENTITY, model_resolver=endpoint_models, sleep=lambda s: None,
                                      pool_factory=lambda n: ThreadPoolExecutor(1))

    def test_resolve_lists_declared_arms_and_counts(self):
        record = self.adapter().resolve(bank_plan(), self.workspace, ROOT)["record"]
        case, = record["cases"]
        self.assertEqual(case["task"], TASK + "__source-1")
        self.assertEqual(case["representations"], ["outcome-first", "fact"])
        self.assertEqual(case["arms"], ["outcome-first__preload", "fact__on_demand"])
        self.assertEqual(record["assigned"], dict(source=1, reused_first_pass=0, write=2, solve=2))

    def test_tool_only_reader_cannot_claim_a_notice(self):
        plan = bank_plan()
        plan["reading"]["on_demand"]["notice"] = dict(text=ON_DEMAND_NOTICE)
        with self.assertRaisesRegex(ContractError, "tool-only on_demand reader shows no notice"):
            self.adapter().resolve(plan, self.workspace, ROOT)

    def test_command_line_resolves_a_plan_in_a_fresh_interpreter(self):
        import subprocess
        import sys
        plan_path = self.workspace / "plan.json"
        plan_path.write_text(json.dumps(bank_plan()), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "TAU2_DATA_DIR"}
        done = subprocess.run([sys.executable, "-m", "agent_memory_eval.study", "plan", "resolve", "--plan", str(plan_path),
                               "--workspace", str(self.workspace)], capture_output=True, text=True, env=env, cwd=ROOT)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(json.loads(done.stdout)["assigned"], dict(source=1, reused_first_pass=0, write=2, solve=2))

    def test_readers_share_one_notice(self):
        plan = bank_plan()
        plan["reading"]["on_demand"] = dict(read_mode="on_demand", notice=dict(text="Another notice."))
        with self.assertRaisesRegex(ContractError, "share one notice"):
            self.adapter().resolve(plan, self.workspace, ROOT)

    def test_spending_needs_an_approval_for_this_resolved_plan(self):
        plan = bank_plan()
        approval = dict(self.approve(plan), plan_sha256="0" * 64)
        with self.assertRaisesRegex(ContractError, "not authorized"):
            self.adapter().run(plan, self.workspace, ROOT, approval=approval, identity=IDENTITY,
                               model_resolver=endpoint_models, pool_factory=lambda n: ThreadPoolExecutor(1))

    def test_round_runs_retries_a_transient_failure_and_scores(self):
        from agent_memory_eval.study.records import sealed_rows
        from agent_memory_eval.study.scorecard import scorecard
        plan = bank_plan()
        done = self.run_plan(plan, FlakyCustomer())
        retried = [name for name in done["stages"] if name.endswith("solve-retry-1")]
        self.assertEqual(retried, [f"task-{TASK}__source-1/solve-retry-1"])
        _, _, original = sealed_rows(self.workspace / "runs" / plan["id"] / f"task-{TASK}__source-1" / "solve")
        self.assertEqual(sorted(r["result"].get("error_code") or "" for r in original), ["", "HTTP_503"])
        card = scorecard(self.adapter().outcomes(plan, self.workspace, ROOT))
        self.assertEqual(set(card["conditions"]), {"no_memory", "outcome-first__preload", "fact__on_demand"})
        self.assertEqual(sum(c["graded_after_retry"] for c in card["conditions"].values()), 1)
        for name in ("outcome-first__preload", "fact__on_demand"):
            self.assertEqual(card["conditions"][name]["assigned"], 1)
            self.assertEqual(card["conditions"][name]["missing"], {})
        self.assertEqual(card["conditions"]["outcome-first__preload"]["preloaded"], 1)

    def test_reused_first_pass_skips_the_source_stage(self):
        from agent_memory_eval.study.scorecard import scorecard
        self.run_plan(bank_plan(), FlakyCustomer(fail=False))
        second = bank_plan("bank-fixture-02", first_pass=dict(draws=1, reuse=[dict(recording="bank-fixture-01")]))
        record = self.adapter().resolve(second, self.workspace, ROOT)["record"]
        self.assertEqual(record["assigned"], dict(source=0, reused_first_pass=1, write=2, solve=2))
        self.run_plan(second, FlakyCustomer(fail=False))
        self.assertFalse((self.workspace / "runs/bank-fixture-02" / f"task-{TASK}__source-1" / "source").exists())
        rows = self.adapter().outcomes(second, self.workspace, ROOT)
        source, = [r for r in rows if r["condition"] == "no_memory"]
        self.assertEqual(source["origin"], "bank-fixture-01")
        self.assertEqual(scorecard(rows)["conditions"]["fact__on_demand"]["assigned"], 1)


if __name__ == "__main__":
    unittest.main()
