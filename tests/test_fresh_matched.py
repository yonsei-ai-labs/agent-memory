"""Exercise a new series through its actual source, writers and both readers without HTTP."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from scripts import run_fresh_matched as study
from scripts.report_fresh_matched import report
from scripts.run_full_retail import accounting
from test_forms_solve import AVAILABLE, ROOT
from test_openrouter import response


@unittest.skipUnless(AVAILABLE, "Requires the pinned native benchmark")
class FreshMatchedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg, cls.conn, cls.cases, cls.tasks, cls.record = study.prepare(ROOT, ROOT, "development")

    def test_design_has_one_source_and_two_readers_of_each_new_writer(self):
        self.assertEqual(self.record["assigned"], dict(source=2, write=10, solve=20, second_pass_no_memory=0))
        case = self.cases[0]
        self.assertEqual([r["arm"] for r in study.jobs_for(case, "source")], ["no_memory"])
        design = study.design_for(self.cfg, case, "solve")
        for form in study.FORMS:
            self.assertEqual({v["read_mode"] for v in design["delivery_arms"].values() if v["writer"] == form},
                             {"on_demand", "preload"})
        self.assertNotIn("runs/", canonical(design).decode())

    def test_new_source_writer_and_reader_isolation_and_no_resend(self):
        registration = dict(self.record, authorization=dict(user_authorization="fixture only", maximum_usd=0.40))
        cfg = dict(self.cfg, registration_sha256=digest(canonical(registration)))
        models = study.read(ROOT / cfg["endpoints"]["path"])["models"]
        case = self.cases[0]
        wires = []

        def respond(client, wire):
            client.guard.before(wire, client.model)
            wires.append(wire)
            self.assertFalse(any({"reasoning", "reasoning_details", "refusal"} & set(m) for m in wire["messages"]))
            tools = [t["function"]["name"] for t in wire.get("tools", [])]
            content = "I can help."
            call = None
            if wire["model"] == self.conn["user_simulator"]["model_id"]:
                content = {2: "Help me.", 4: "One more question."}.get(len(wire["messages"]), "###STOP###")
            elif tools == ["memory_store"]:
                form, = [f for f, line in cfg["writing"]["lines"].items() if wire["messages"][0]["content"].endswith(line)]
                if form != "free" and wire["messages"][-1]["role"] != "tool":
                    call = ("memory_store", dict(title="Observation", body=f"Note marker {form}"))
                content = "Finished."
            elif "memory_list" in tools and not any(m["role"] == "tool" for m in wire["messages"]):
                call = ("memory_list", {})
            elif "memory_get" in tools and wire["messages"][-1]["role"] == "tool":
                listing = json.loads(wire["messages"][-1]["content"]).get("items", [])
                if listing and all("body" not in item for item in listing):
                    call = ("memory_get", {"id": listing[0]["id"]})
            result = response()
            result.update(id=f"new-fixture-{len(wires)}", model=wire["model"],
                          provider=client.model["selected_endpoint"]["provider_name"])
            message = dict(role="assistant", content=content, reasoning="fixture-provider-only",
                           reasoning_details=[{"type": "fixture"}], refusal=None)
            if call:
                message.update(content=None, tool_calls=[dict(id=f"tc-{len(wires)}", type="function",
                    function=dict(name=call[0], arguments=json.dumps(call[1])))])
                result["choices"][0]["finish_reason"] = "tool_calls"
            result["choices"][0]["message"] = message
            client.guard.after(result)
            return result

        with TemporaryDirectory() as tmp, patch.object(OpenRouterClient, "respond", respond):
            output = Path(tmp)
            args = (ROOT, output, cfg, {}, self.conn, case, self.tasks[case["task"]])
            tail = ({"fixture": True}, models, None, "0.40")
            for stage in case["scheduled_stages"]:
                study.stage_job((*args, stage, *tail))
            task_dir = output / ("task-" + case["task"])
            source_dir, write_dir, solve_dir = [task_dir / s for s in case["scheduled_stages"]]
            _, _, first = study.sealed_rows(source_dir)
            self.assertEqual(first[0]["result"]["grading_status"], "graded")
            _, _, writers = study.sealed_rows(write_dir)
            _, _, readers = study.sealed_rows(solve_dir)
            self.assertEqual(sum(r["status"] == "empty" for r in writers), 1)
            self.assertEqual(sum(r["result"]["grading_status"] == "graded" for r in readers), 10)
            self.assertEqual(len({r["result"]["session_id"] for r in first + writers + readers}), 16)
            by_form = {r["arm"]: r for r in writers}
            for r in readers:
                form, mode = r["arm"].split("__")
                folder = solve_dir / r["directory"]
                scan, state, _ = read_experiment(folder)
                allocation = state["assignments"][r["run_id"]]
                self.assertEqual(allocation["source_writer_run_id"], by_form[form]["run_id"])
                self.assertEqual(allocation["source_writer_snapshot_id"], by_form[form]["result"]["snapshot_id"])
                blobs = BlobStore(folder)
                events = list(scan["events"].values())
                for e in events:
                    if e["payload"].get("origin") == "tau2_worker_output":
                        published = blobs.get(e["payload"]["public_message_ref"])
                        self.assertFalse({"reasoning", "reasoning_details", "refusal"} & set(published))
                receipt, = [blobs.get(e["payload"]["receipt_ref"]) for e in events
                            if e["payload"].get("origin") == "matched_trial.native_control"]
                self.assertEqual(receipt["source_run_id"], first[0]["run_id"])
                inputs = [e["payload"] for e in events if e["type"] == "model.request_started"]
                customer = [blobs.get(p["request_ref"]) for p in inputs if p["actor_role"] == "user_simulator"]
                worker = [blobs.get(p["request_ref"]) for p in inputs if p["actor_role"] == "worker"]
                self.assertEqual(digest(canonical(customer[0])), case["customer_opening_sha256"])
                self.assertNotIn("Note marker", canonical(customer).decode())
                if form != "free":
                    self.assertEqual("Note marker" in canonical(worker[0]).decode(), mode == "preload")
                    self.assertIn("Note marker " + form, canonical(worker[-1]).decode())
            n = len(wires)
            study.stage_job((*args, case["scheduled_stages"][-1], *tail))
            self.assertEqual(len(wires), n)
            with self.assertRaises(ContractError):
                study.stage_job((ROOT, output, dict(cfg, registration_sha256="a" * 64), *args[3:],
                                 case["scheduled_stages"][1], *tail))
            self.assertEqual(len(wires), n)
            write_json(output/"registration.json", dict(preflight=registration))
            seals = {}
            for c in self.cases:
                for name in c["scheduled_stages"]:
                    key = "task-" + c["task"] + "/" + name
                    seals[key] = (accounting(output/key, "0.40") if c == case else
                                  dict(status="not_started", reason="spend_limit", reported="0", held="0", spent="0", http=0))
            total = sum(Decimal(s["spent"]) for s in seals.values())
            write_json(output/"completion.json", dict(registration_sha256=digest((output/"registration.json").read_bytes()),
                       stages=seals, total_covered_usd=str(total)))
            summary = report(output)
            self.assertEqual(summary["distinct_sessions"], 16)
            lookup = summary["contrasts"]["airline/fact/on_demand/no_memory"]
            self.assertEqual(lookup["outcomes"]["assigned"], 2)
            self.assertEqual(lookup["outcomes"]["missing"], 1)
            self.assertEqual((lookup["body_seen"], lookup["read_calls"], lookup["identical_opening_replies"]), (1, 2, 1))

    def test_retail_native_judge_is_private_to_grading(self):
        from tau2.multilingual.runtime import load_run_language
        from tau2.runner.helpers import select_run_tasks
        case, = [c for c in study.read(ROOT/study.PREFLIGHT)["cases"] if c["task"] == "retail-21"]
        cfg = dict(self.cfg, registration_sha256="c" * 64)
        design = study.design_for(cfg, case, "source")
        conn = study.solve_connection(self.conn, design, study.jobs_for(case, "source")[0], cfg["episode_cap_usd"])
        native = study.native_config(conn)
        task, = select_run_tasks(native, load_run_language(native))
        models = study.read(ROOT / cfg["endpoints"]["path"])["models"]
        judge = cfg["judge"]
        judge_model = dict(id=judge["model"], canonical_slug=judge["canonical_slug"],
            pricing=cfg["endpoint_terms"]["grader"]["reservation_pricing"], selected_endpoint=dict(provider_name="OpenAI"))
        judge_inputs = []
        def respond(client, wire):
            client.guard.before(wire, client.model)
            if wire["model"] == judge["model"]:
                judge_inputs.append(wire)
                content = json.dumps(dict(results=[dict(expectedOutcome=a, metExpectation=False, reasoning="PRIVATE_JUDGE")
                                                    for a in task.evaluation_criteria.nl_assertions]))
            elif wire["model"] == conn["worker"]["model_id"]:
                content = "How can I help?"
            else:
                content = "Please help." if len(wire["messages"]) == 2 else "###STOP###"
            out = response()
            out.update(id="mock-" + digest(canonical(wire)), model=wire["model"], provider=client.model["selected_endpoint"]["provider_name"])
            out["choices"][0]["message"]["content"] = content
            client.guard.after(out)
            return out

        with TemporaryDirectory() as tmp, patch.object(OpenRouterClient, "respond", respond):
            stage = case["scheduled_stages"][0]
            study.stage_job((ROOT, Path(tmp), cfg, {}, self.conn, case, task.model_dump(mode="json"), stage,
                             {"fixture": True}, models, judge_model, "0.40"))
            source = study.source_from(Path(tmp)/("task-"+case["task"])/stage, case["task_id"])
            self.assertIsNotNone(source)
            self.assertEqual(len(judge_inputs), 1)
            self.assertNotIn("PRIVATE_JUDGE", source.public_context)
            for assertion in task.evaluation_criteria.nl_assertions:
                self.assertNotIn(assertion, source.public_context)

    def test_changed_customer_task_cannot_send_even_the_first_request(self):
        cfg = dict(self.cfg, registration_sha256="b" * 64)
        models = study.read(ROOT / cfg["endpoints"]["path"])["models"]
        case = self.cases[0]
        altered = deepcopy(self.tasks[case["task"]])
        altered["user_scenario"]["instructions"]["task_instructions"] += " Mention the previous visit."
        with TemporaryDirectory() as tmp, patch.object(OpenRouterClient, "respond", side_effect=AssertionError) as send:
            with self.assertRaises(ContractError):
                study.stage_job((ROOT, Path(tmp), cfg, {}, self.conn, case, altered, case["scheduled_stages"][0],
                                 {"fixture": True}, models, None, "0.40"))
            send.assert_not_called()
