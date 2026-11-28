"""Cross-task transfer with real benchmark adapters and an in-process, deterministic model transport."""

import json
import io
import os
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from functools import partial
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments import forms_batch, forms_solve
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.study.adapters import adapter_for
from agent_memory_eval.study.adapters.tau_banking import approval_for
from agent_memory_eval.study.plan import check_plan, load_plan
from agent_memory_eval.study.scorecard import scorecard
from agent_memory_eval.study.transfer import allocation_rows, decision_evidence
from test_openrouter import Reply, response
from test_study_bank import IDENTITY, endpoint_models, scripted_message
from test_study_tau2_domains import judge_model


def study(domain):
    return load_plan(ROOT / f"protocols/studies/{domain}-behavior-transfer-dev-01.json", ROOT)


class ScriptedTransport:
    def __init__(self, fail_target_after_opening=False):
        self.wires = []
        self.stops = 0
        self.fail_target_after_opening = fail_target_after_opening

    def __call__(self, request, timeout):
        wire = json.loads(request.data)
        self.wires.append(wire)
        raw = response(cost=0)
        raw["model"] = wire["model"]
        messages = wire["messages"]
        tools = [t["function"]["name"] for t in wire.get("tools", [])]
        if wire["model"].startswith("deepseek/"):
            raw["provider"] = "Fireworks"
            content = "I need help with my request." if len(messages) == 2 else "###STOP###"
            if content == "###STOP###":
                self.stops += 1
                if self.fail_target_after_opening and self.stops == 2:
                    raise HTTPError(request.full_url, 503, "unavailable", {}, io.BytesIO(b"{}"))
            msg = dict(role="assistant", content=content)
        elif wire["model"].startswith("openai/gpt-5.4-mini"):
            task_path = ROOT / "data/tau2-bench/data/tau2/domains/retail/tasks.json"
            all_assertions = {
                a
                for t in json.loads(task_path.read_bytes())
                for a in t["evaluation_criteria"].get("nl_assertions") or []
            }
            results = [
                dict(expectedOutcome=a, metExpectation=False, reasoning="Fixture")
                for a in all_assertions
                if a in messages[-1]["content"]
            ]
            msg = dict(role="assistant", content=json.dumps(dict(results=results)))
        elif any(
            m["role"] == "tool" and json.loads(m["content"]).get("items") == []
            for m in messages
            if m.get("name", "").startswith("memory_") or m.get("tool_call_id", "").startswith("fixture-memory_")
        ):
            msg = dict(role="assistant", content="Could you clarify the request?")
        elif (
            "memory_get" in tools
            and any(c["function"]["name"] == "memory_get" for m in messages for c in m.get("tool_calls") or [])
            and any(t.lower().startswith("movies") for t in tools)
        ):
            msg = dict(
                role="assistant",
                content=None,
                tool_calls=[
                    dict(id="fixture-business", type="function", function=dict(name=tools[0], arguments="{}"))
                ],
            )
        else:
            msg = scripted_message(wire)
        raw["choices"][0].update(message=msg, finish_reason="tool_calls" if msg.get("tool_calls") else "stop")
        return Reply(raw)


class TransferTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        (self.workspace / "data").mkdir()
        (self.workspace / "data/tau2-bench").symlink_to(
            (ROOT / "data/tau2-bench").resolve(), target_is_directory=True
        )
        shutil.copytree(ROOT / "protocols", self.workspace / "protocols")

    def test_invalid_or_duplicate_pairs_are_rejected(self):
        plan = study("retail")
        for source, target in [("44", "44"), ("58", "37"), ("44", "unknown")]:
            changed = deepcopy(plan)
            changed["transfer"]["pairs"][0].update(source=source, target=target)
            with self.assertRaises(ContractError):
                check_plan(changed, ROOT)
        plan["transfer"]["pairs"].append(plan["transfer"]["pairs"][0])
        with self.assertRaisesRegex(ContractError, "Duplicate transfer"):
            check_plan(plan, ROOT)

    def test_aborted_turn_is_not_credit_for_refraining_from_an_action(self):
        rule = dict(tools=["update"], expected=None)
        self.assertIsNone(decision_evidence(rule, [], completed=False)["correct"])
        self.assertIs(decision_evidence(rule, [], completed=True)["correct"], True)
        called = [dict(request="a", body=False, calls=[dict(name="update", arguments={})])]
        self.assertIs(decision_evidence(rule, called, completed=False)["correct"], False)

    def test_declared_customer_relationship_matches_native_identity(self):
        plan = study("retail")
        pair = plan["transfer"]["pairs"][0]
        # Tasks 44 and 105 involve the same customer but different scenarios.
        pair.update(target="105", customer_relation="same_customer")
        plan["transfer"]["pairs"] = [pair]
        plan["benchmark"]["tasks"] = ["44", "105"]
        adapter = adapter_for(plan)
        record = adapter.resolve(plan, self.workspace, ROOT)["record"]
        (reader,) = [c for c in record["cases"] if "donor" in c]
        self.assertEqual(reader["donor"]["customer"], reader["opening"]["customer"])
        self.assertNotEqual(reader["donor"]["task_sha256"], reader["opening"]["task_sha256"])
        pair["customer_relation"] = "different_customer"
        with self.assertRaisesRegex(ContractError, "customer relationship"):
            adapter.resolve(plan, self.workspace, ROOT)

    def test_distinct_targets_and_missing_bounds(self):
        plan = study("airline")
        rows = allocation_rows(plan, [])
        self.assertEqual(len(rows), 12)
        rows[0].update(status="graded", success=True, decision=dict(correct=True, body_seen=False))
        rows[1].update(status="graded", success=False, decision=dict(correct=False, body_seen=False))
        outcome = scorecard(rows)["comparisons"]["free__on_demand"]["official_success"]
        self.assertEqual((outcome["real_only"], outcome["missing"], outcome["targets"]), (1, 1, 2))
        self.assertEqual(outcome["target_weighted_bounds"], [0, 1])

    def full_run(self, domain, *, fail_target_after_opening=False, initial_ceiling=None):
        plan = study(domain)
        adapter = adapter_for(plan)
        transport = ScriptedTransport(fail_target_after_opening)
        make_client = partial(OpenRouterClient, opener=transport)
        native = {}
        if domain == "bfcl":
            native["checkout"] = ROOT / ".local/benchmark-portability/bfcl-6ea5797"
            if not native["checkout"].is_dir():
                self.skipTest("Pinned native BFCL checkout is unavailable")
            execution = dict(
                model_resolver=lambda worker: endpoint_models(None, None)[0]["worker"], client_factory=make_client
            )
        else:

            def resolve(cfg, conn):
                models, _ = endpoint_models(cfg, conn)
                return models, judge_model() if domain == "retail" else None

            execution = dict(model_resolver=resolve, pool_factory=lambda n: ThreadPoolExecutor(1))
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, OPENROUTER_API_KEY="in-process-fixture"))
            stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("network disabled")))
            stack.enter_context(patch.object(forms_batch, "OpenRouterClient", make_client))
            stack.enter_context(patch.object(forms_solve, "OpenRouterClient", make_client))
            record = adapter.resolve(plan, self.workspace, ROOT, **native)["record"]
            grant = approval_for(plan, record, max_usd="1", user_authorization="Offline fixture")
            run_args = dict(approval=grant)
            if initial_ceiling:
                small = approval_for(
                    plan, record, max_usd=initial_ceiling, user_authorization="Offline smaller ceiling"
                )
                blocked = adapter.run(
                    plan, self.workspace, ROOT, approval=small, identity=IDENTITY, **execution, **native
                )
                self.assertTrue(blocked["budget_deferred"])
                self.assertEqual(transport.wires, [])
                run_args = dict(approval=small, increases=[grant])
            execute = partial(
                adapter.run,
                plan,
                self.workspace,
                ROOT,
                identity=IDENTITY,
                sleep=lambda s: None,
                **execution,
                **native,
                **run_args,
            )
            execute()
            rows = adapter.outcomes(plan, self.workspace, ROOT, **native)
            self.assertEqual(len(rows), 12)
            self.assertTrue(
                all(r["status"] == "graded" for r in rows), [(r["task"], r["missing_reason"]) for r in rows]
            )
            self.assertEqual(len({r["writer_run"] for r in rows}), 1)
            self.assertEqual(len({r["donor_snapshot"] for r in rows}), 1)
            self.assertEqual({r["decision"]["body_seen"] for r in rows if r["bank"] == "real"}, {True})
            self.assertEqual({r["decision"]["body_seen"] for r in rows if r["bank"] == "empty"}, {False})
            for target in plan["transfer"]["pairs"]:
                for policy in ("free__on_demand", "free__when-needed"):
                    pair = [r for r in rows if r["task"] == target["target"] and r["comparison"] == policy]
                    self.assertEqual({r["bank"] for r in pair}, {"real", "empty"})
                    if domain != "bfcl":
                        self.assertEqual(len({r["evidence"]["first_worker"] for r in pair}), 1)
            descriptions = {
                t["function"]["description"]
                for w in transport.wires
                for t in w.get("tools", [])
                if t["function"]["name"] == "memory_list"
            }
            self.assertEqual(
                descriptions,
                {r["tool_descriptions"]["memory_list"] for r in plan["reading"].values() if "tool_descriptions" in r},
            )
            calls = len(transport.wires)
            execute()
            self.assertEqual(len(transport.wires), calls)
            self.assertEqual(rows, adapter.outcomes(plan, self.workspace, ROOT, **native))
        return rows, transport.wires

    def test_retail_transfer(self):
        self.full_run("retail")

    def test_airline_transfer(self):
        self.full_run("airline", fail_target_after_opening=True)

    def test_bfcl_transfer(self):
        rows, wires = self.full_run("bfcl", initial_ceiling="0.000001")
        # No source task, grade or target question can appear in another task's first worker request.
        writers = [w for w in wires if [t["function"]["name"] for t in w["tools"]] == ["memory_store"]]
        self.assertEqual(len(writers), 1)
        readers = [
            w
            for w in wires
            if len(w["messages"]) == 2 and any(t["function"]["name"] == "memory_list" for t in w["tools"])
        ]
        self.assertEqual(len(readers), 8)
        self.assertEqual(sorted(readers.count(w) for w in readers), [2] * 8)
        for w in readers:
            self.assertNotIn(w["messages"][1]["content"], str(writers[0]["messages"]))
        self.assertEqual({r["task"] for r in rows}, {"live_relevance_9-9-0", "irrelevance_54"})
        for pair in study("bfcl")["transfer"]["pairs"]:
            self.assertFalse(any(pair["source_cue"] in str(w) for w in wires))
        # The scripted model calls in both movie cases after reading. This helps one target and harms the other.
        actions = scorecard(rows)["comparisons"]["free__on_demand"]["registered_decision"]
        self.assertEqual((actions["real_only"], actions["empty_only"]), (1, 1))
