"""Exercise the native bank, customer tools, official grade and imported opening without network."""

import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.models.openrouter import OpenRouterClient
from scripts import run_banking_memory as banking
from test_forms_solve import AVAILABLE, ROOT
from test_openrouter import response


@unittest.skipUnless(AVAILABLE, "Requires the pinned τ benchmark")
class BankingMemoryTests(unittest.TestCase):
    def test_real_search_and_graded_memory_trials_start_at_same_public_question(self):
        from importlib.util import find_spec
        if not find_spec("rank_bm25"):
            self.skipTest("Requires the τ knowledge extra")
        _, cfg, conn, cases, tasks, record = banking.prepare(ROOT)
        study = banking.matched
        case = next(c for c in cases if c["stratum"] == "required_action")
        cfg["registration_sha256"] = digest(canonical(record))
        strict = banking.required_design(cfg, banking.matched.read(ROOT / banking.REQUIRED_PROFILE))
        strict["registration_sha256"] = digest(canonical([record, "required-fixture"]))
        models = study.read(ROOT / cfg["endpoints"]["path"])["models"]
        opening_count = 0
        grep_returns = []

        def respond(client, wire):
            nonlocal opening_count
            client.guard.before(wire, client.model)
            names = {t["function"]["name"] for t in wire.get("tools", [])}
            messages = wire["messages"]
            call = None
            text = "Complete."
            if wire["model"] == conn["user_simulator"]["model_id"]:
                self.assertNotIn("private sample note", canonical(messages).decode())
                if len(messages) == 2:
                    opening_count += 1
                    text = "Please help with my card."
                else:
                    text = "###STOP###"
            elif names == {"memory_store"}:
                self.assertEqual(wire["tool_choice"], "required")
                if messages[-1]["role"] != "tool":
                    call = ("memory_store", {"title": "Observation", "body": "private sample note"})
            else:
                self.assertTrue(any(m.get("content") == "Please help with my card." for m in messages))
                seen = {c["function"]["name"] for m in messages for c in m.get("tool_calls") or []}
                if "memory_list" in names and "memory_list" not in seen:
                    call = ("memory_list", {})
                elif "memory_get" in names and "memory_get" not in seen:
                    call = ("memory_get", {"id": json.loads(messages[-1]["content"])["items"][0]["id"]})
                elif "grep" not in seen:
                    call = ("grep", {"pattern": "annual fee"})
                elif messages[-1]["role"] == "tool":
                    grep_returns.append(messages[-1]["content"])
                    if "private sample note" in canonical(messages).decode() and "transfer_to_human_agents" not in seen:
                        # Analyst-only positive control. This code never constructs a live prompt.
                        call = ("transfer_to_human_agents",
                                {"reason": "customer_demands_after_unavailable_offer_refusal", "summary": ""})
            out = response()
            out.update(id=digest(canonical(wire)), model=wire["model"], provider=client.model["selected_endpoint"]["provider_name"])
            msg = dict(role="assistant", content=text)
            if call:
                msg.update(content=None, tool_calls=[dict(id="call-" + call[0], type="function",
                    function=dict(name=call[0], arguments=json.dumps(call[1])))])
                out["choices"][0]["finish_reason"] = "tool_calls"
            out["choices"][0]["message"] = msg
            client.guard.after(out)
            return out

        with TemporaryDirectory() as tmp, patch.object(OpenRouterClient, "respond", respond):
            output = Path(tmp)
            results = {}
            for stage in case["scheduled_stages"]:
                chosen = cfg if stage == "source" else strict
                study.stage_job((ROOT if stage == "source" else workspace, output, chosen, {}, conn, case, tasks[case["task"]], stage,
                                 {"fixture": True}, models, None, "1.50"))
                results[stage] = study.sealed_rows(output / ("task-" + case["task"]) / stage)[2]
                if stage == "source":
                    workspace = output / "workspace"
                    (workspace / "data").mkdir(parents=True)
                    (workspace/"data/tau2-bench").symlink_to(ROOT/"data/tau2-bench", target_is_directory=True)
                    archived = output / ("task-" + case["task"]) / "source"
                    (workspace/"runs").mkdir()
                    archived.rename(workspace/"runs/source")
                    source = study.source_from(workspace/"runs/source", case["task_id"])
                    case = dict(case, imported_first_pass=dict(directory="source", registration_sha256=cfg["registration_sha256"],
                                run_id=source.source_run_id, log_sha256=source.source_log_sha256,
                                public_context_sha256=source.public_context_sha256))
            self.assertEqual(results["source"][0]["result"]["official_success"], False)
            self.assertEqual(opening_count, 1)
            self.assertEqual(len(results["solve"]), 10)
            for row in results["solve"]:
                self.assertIs(row["result"]["official_success"], True, row)
            from agent_memory_eval.analysis.tau2_attribution import graded_episode
            source = results["source"][0]
            folder = output / ("task-" + case["task"])
            ref, _ = graded_episode(workspace/"runs/source", source, case, cfg["delivery_instruction"]["sha256"])
            row = next(r for r in results["solve"] if r["arm"] == "fact__preload")
            second, _ = graded_episode(folder/"solve", row, case, cfg["delivery_instruction"]["sha256"], source)
            self.assertEqual((ref[0].actor, second[0].actor), ("worker", "worker"))
            self.assertEqual(ref[0].history, second[0].history)
            self.assertEqual(second[0].fields, {"title", "body"})
            self.assertTrue(any("   ID: " in text for text in grep_returns))

            from agent_memory_eval.contracts.core import ContractError
            from agent_memory_eval.experiments.forms_batch import run_jobs, verify_required_form_sets
            wdir = folder/"write"
            _, _, writers = study.sealed_rows(wdir)
            source = study.source_from(workspace/"runs/source", case["task_id"])
            design = study.design_for(strict, case, "solve", source)
            jobs = study.jobs_for(case, "solve")
            plan = dict(jobs=jobs, sources={case["task_id"]: source})
            bindings = {(1, case["task_id"], arm): next(r for r in writers if r["arm"] == policy["writer"])
                        for arm, policy in design["delivery_arms"].items()}
            self.assertFalse(verify_required_form_sets(design, plan, wdir, bindings))
            bad = deepcopy(bindings)
            for key in bad:
                if key[-1].startswith("procedure__"):
                    bad[key]["status"] = "empty"
            missing = verify_required_form_sets(design, plan, wdir, bad)
            self.assertEqual(missing, {(1, case["task_id"])})
            unstarted = deepcopy(jobs)
            guard = type("NoSpending", (), {"stop": lambda self: None})()
            run_jobs(unstarted, lambda *_: self.fail("Missing form admitted a worker request"), guard,
                     lambda _: None, initial_writers=bad, incomplete_form_sets=missing)
            self.assertEqual([r["reason"] for r in unstarted], ["required_form_set_incomplete"] * 10)
            key = (1, case["task_id"], "fact__preload")
            bad = {**bindings, key: bindings[(1, case["task_id"], "episode__preload")]}
            with self.assertRaises(ContractError):
                verify_required_form_sets(design, plan, wdir, bad)
            from dataclasses import replace
            with self.assertRaises(ContractError):
                verify_required_form_sets(design, dict(plan, sources={case["task_id"]: replace(source, source_run_id="another")}),
                                          wdir, bindings)
