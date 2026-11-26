"""Exercise the assigned customer scripts and bank isolation through the native episode controller."""

import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import canonical, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import BatchGuard, execute_prepared
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from scripts.run_airline_allocation import CASES, FORMS, PROTOCOL, prepare, schedule
from test_forms_write import store
from test_openrouter import Reply, response


WORKSPACE = Path(os.environ.get("FIXED_BANK_QA_WORKSPACE", ROOT))


class AllocationTests(unittest.TestCase):
    def test_five_writers_and_three_requests_are_fully_crossed_once(self):
        study = read_json((ROOT / PROTOCOL).read_bytes())
        rows = schedule(study)
        self.assertEqual([r["index"] for r in rows], list(range(1, 21)))
        self.assertEqual({r["arm"] for r in rows[:5] if r["phase"] == "write"}, set(FORMS))
        self.assertEqual({(r["arm"], r["task_variant"]) for r in rows[5:] if r["phase"] == "solve"},
                         {(a, v) for a in FORMS for v in CASES})
        self.assertEqual({r["task_id"] for r in rows}, {"23"})

    @unittest.skipUnless(importlib.util.find_spec("tau2") and (WORKSPACE / "data/tau2-bench/src/tau2").is_dir(),
                         "Needs the pinned airline benchmark")
    def test_receipt_audit_identifies_only_booking_id_order(self):
        from agent_memory_eval.adapters.tau2 import native_modules, verify_checkout
        from scripts.analyze_airline_allocation import booking_audit
        checkout = verify_checkout(WORKSPACE / "data/tau2-bench")
        native_modules(checkout)
        from tau2.data_model.message import ToolCall
        from tau2.domains.airline.environment import get_environment

        tasks = read_json((checkout / "data/tau2/domains/airline/tasks.json").read_bytes())
        task, = [t for t in tasks if t["id"] == "23"]
        actions = task["evaluation_criteria"]["actions"]
        env = get_environment()
        initial = set(env.tools.db.reservations)
        for index in (0, 1, 3, 2):
            step = actions[index]
            self.assertFalse(env.get_response(ToolCall(id=str(index), name=step["name"], arguments=step["arguments"])).error)
        sim = SimpleNamespace(messages=[], reward_info=SimpleNamespace(db_check=SimpleNamespace(db_match=False)))
        for change_baggage in (False, True):
            if change_baggage:
                env.tools.db.reservations["HATHAT"].total_baggages = 1
            before = env.get_db_hash()
            audit = booking_audit(task, sim, env.tools.db, initial)
            self.assertEqual(before, env.get_db_hash())
            self.assertNotEqual(audit["reference_db"], before)
            self.assertEqual(audit["db_if_only_these_ids_transposed"] == audit["reference_db"], not change_baggage)

    @unittest.skipUnless(importlib.util.find_spec("tau2") and (WORKSPACE / "data/tau2-bench/src/tau2").is_dir()
                         and (WORKSPACE / "runs/tau2-baseline-luna-deepseek1017-train-01/log.jsonl").is_file(),
                         "This native smoke test needs the pinned checkout and archived first pass")
    def test_native_variants_reuse_only_the_assigned_bank_with_fresh_sessions(self):
        study, design, plan = prepare(ROOT, WORKSPACE)
        self.assertEqual(plan["summary"]["strict_native_grade_checks"],
                         {case: {"explicit_same": int(case != "explicit_swapped"),
                                 "explicit_swapped": int(case == "explicit_swapped")} for case in CASES})
        self.assertEqual(plan["task_variants"]["23"]["explicit_same"], plan["tasks"]["23"])
        models = read_json(asset_text(ROOT, study["endpoint_ref"]).encode())["models"]
        new_client = OpenRouterClient.__init__
        wires = []
        def local_http(request, timeout):
            wire = json.loads(request.data)
            wires.append(wire)
            raw = response(cost=0.00001)
            is_customer = wire["model"] == models["user_simulator"]["id"]
            raw.update(model=wire["model"], provider=models["user_simulator" if is_customer else "worker"]["selected_endpoint"]["provider_name"])
            names = [t["function"]["name"] for t in wire.get("tools", [])]
            if "memory_store" in names:
                line = wire["messages"][0]["content"].splitlines()[-1]
                arm, = [key for key, value in design["writing"]["lines"].items() if line == value]
                if arm != "episode" and not any(m["role"] == "tool" for m in wire["messages"]):
                    raw["choices"] = store(arm)["choices"]
            elif is_customer:
                text = "###STOP###" if len(wire["messages"]) > 2 else "Hello, please help."
                raw["choices"][0]["message"] = {"role": "assistant", "content": text}
            return Reply(raw)
        def inject(client, *args, **kwargs):
            new_client(client, *args, opener=local_http, **kwargs)
        with TemporaryDirectory() as tmp, patch.object(OpenRouterClient, "__init__", inject), \
                patch.dict("os.environ", OPENROUTER_API_KEY="fixture-only"):
            destination = Path(tmp) / "smoke"
            done = execute_prepared(study, ROOT, destination, {"implementation_commit": "fixture"}, design, plan,
                                    models, BatchGuard(max_calls=150, max_usd=1, max_seconds=300))
            self.assertIsNone(done["stop_reason"])
            rows = read_json((destination / "allocations.json").read_bytes())
            self.assertEqual(len({r["result"]["session_id"] for r in rows}), 20)
            by_form = {r["arm"]: r["result"] for r in rows[:5]}
            self.assertEqual(by_form["episode"]["status"], "empty")
            self.assertEqual(len({r["result"]["bank_id"] for r in rows}), 20)
            for row in rows[5:]:
                self.assertEqual(row["result"]["grading_status"], "graded")
                folder = destination / row["directory"]
                scan, state, _ = read_experiment(folder)
                blobs = BlobStore(folder)
                assignment = state["assignments"][row["run_id"]]
                self.assertEqual(assignment["source_writer_snapshot_id"], by_form[row["arm"]]["snapshot_id"])
                expected_script = plan["task_variants"]["23"][row["task_variant"]]["user_scenario"]["instructions"]["task_instructions"]
                expected_rule = " ".join(expected_script.split())
                for e in scan["events"].values():
                    if e["type"] != "model.request_started":
                        continue
                    p = e["payload"]
                    wire = blobs.get(p["request_ref"])
                    if p["actor_role"] == "user_simulator":
                        self.assertIn(expected_rule, " ".join(wire["messages"][0]["content"].split()))
                        self.assertNotIn("A procedural note", canonical(wire).decode())
                    else:
                        self.assertNotIn(study["past_private_rule"], canonical(wire).decode())
                        items = json.loads(wire["messages"][1]["content"])["items"]
                        self.assertEqual([i["body"] for i in items],
                                         [] if row["arm"] == "episode" else ["A procedural note " + row["arm"]])
            for w in wires:
                if "memory_store" in [t["function"]["name"] for t in w.get("tools", [])]:
                    self.assertEqual(w["messages"][1]["content"], plan["sources"]["23"].public_context)
                    self.assertNotIn(study["past_private_rule"], canonical(w).decode())
            from scripts.analyze_airline_allocation import report, writer_packet
            masked = writer_packet(destination)
            self.assertEqual(canonical(masked["public_experience"]).decode(), plan["sources"]["23"].public_context)
            self.assertEqual(len(masked["writers"]), 5)
            self.assertTrue(all(set(bank) == {"case", "status", "notes"} for bank in masked["writers"]))
            audit = report(destination, "results", WORKSPACE)
            self.assertEqual(len(audit["consultations"]), 15)
            self.assertTrue(all(not r["allocation_correct"] for r in audit["consultations"]))
