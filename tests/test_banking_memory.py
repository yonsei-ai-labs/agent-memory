"""Exercise the native bank, customer tools, official grade and imported opening without network."""

import json
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
                study.stage_job((ROOT, output, cfg, {}, conn, case, tasks[case["task"]], stage,
                                 {"fixture": True}, models, None, "1.50"))
                results[stage] = study.sealed_rows(output / ("task-" + case["task"]) / stage)[2]
            self.assertEqual(results["source"][0]["result"]["official_success"], False)
            self.assertEqual(opening_count, 1)
            self.assertEqual(len(results["solve"]), 10)
            for row in results["solve"]:
                self.assertIs(row["result"]["official_success"], True, row)
            self.assertTrue(any("   ID: " in text for text in grep_returns))
