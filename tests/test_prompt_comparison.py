from copy import deepcopy
import json
from pathlib import Path
import re
from shutil import copyfile
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.forms_results import checked_rows
from agent_memory_eval.analysis.prompt_report import _first_pass_check, publish_report
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import ordered_jobs
from agent_memory_eval.experiments.first_pass_match import (
    FirstPassMismatch, check_common_read_instruction, check_native, wire_verifier,
)
from agent_memory_eval.memory.context import memory_system_suffix, snapshot_message
from agent_memory_eval.experiments.prompt_comparison import compile_config, run_comparison
from agent_memory_eval.experiments.prompt_delivery import run_delivery_comparison
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.policies.writer_pairs import paired_writer_request
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from scripts.prepare_memory_candidate import candidate_draft, development_packet
from test_forms_solve import AVAILABLE, NativeHTTP
from test_forms_write import store
from test_openrouter import Reply, response

CONFIG = ROOT / "protocols/pilots/tau2-fact-recheck-v1.json"
FIRST_PASS_DRAFT = ROOT / "protocols/drafts/tau2-airline-payment-memory-v2.json"
FORM_PAIRS_DRAFT = ROOT / "protocols/drafts/tau2-airline-writer-pairs-v3.json"


class PromptComparisonTests(unittest.TestCase):
    def test_common_memory_notice_cannot_add_a_business_hint(self):
        for paired, filename in [(False, "frozen-memory-replay-v1.txt"), (True, "tau2-delivery-memory.txt")]:
            text = (ROOT / "protocols/instructions" / filename).read_text()
            check_common_read_instruction(text, paired_delivery=paired)
            with self.subTest(filename=filename), self.assertRaises(FirstPassMismatch):
                check_common_read_instruction(text + "Use the second gift certificate.", paired_delivery=paired)

    def test_report_requires_runtime_proof_and_actual_customer_and_worker_wires(self):
        row = dict(result=dict(grading_status="graded"))
        receipt = dict(source_run_id="src", source_control_sha256="control", native_config_sha256="native",
                       customer_opening_sha256="hello", native_seed=0)
        cfg = dict(archived_first_pass=dict(source_run_id="src", control_sha256="control"),
                   native_config_ref=dict(sha256="native"))
        proof = dict(type="business.transition", payload=dict(origin="forms.first_pass_match", receipt_ref="proof"))
        def request(role, sha):
            return dict(type="model.request_started", payload=dict(actor_role=role, request_ref=dict(sha256=sha)))
        customer, worker = request("user_simulator", "hello"), request("worker", "worker")
        blobs = SimpleNamespace(get=lambda ref: receipt if ref == "proof" else {"seed": 0 if ref["sha256"] == "worker" else 2})
        def check(events):
            return _first_pass_check(row, cfg, events, blobs, receipt, "src")
        self.assertEqual(check([customer, worker]), "unverified")
        self.assertEqual(check([proof, customer, worker]), "matched_graded")
        for bad in ([customer, proof, worker], [proof, customer], [proof, request("user_simulator", "hint"), worker],
                    [proof, customer, request("worker", "wrong-seed")]):
            with self.subTest(events=bad), self.assertRaises(ContractError):
                check(bad)

    def test_workflow_and_grading_fields_are_compared_to_the_archived_task(self):
        original = dict(id="23", customer="Specify the payment", scoring="DB plus amounts")
        task = deepcopy(original)
        tool = SimpleNamespace(openai_schema={"name": "book"})
        env = SimpleNamespace(get_db_hash=lambda: "initial", get_user_db_hash=lambda: None)
        orch = SimpleNamespace(task=SimpleNamespace(model_dump=lambda **kwargs: task), environment=env,
                               agent=SimpleNamespace(system_prompt="policy", tools=[tool]),
                               user=SimpleNamespace(system_prompt="customer", tools=[]))
        settings = dict(seed=0, max_steps=200)
        native = SimpleNamespace(seed=0, model_dump=lambda **kwargs: dict(settings))
        config = dict(guards=dict(max_seconds=600))
        match = dict(run_id="source", control_sha256="sealed", task_sha256=digest(canonical(original)),
                     initial_db="initial", customer_opening_sha256="hello",
                     control=dict(config=config, native_resolved=dict(settings), systems=dict(worker="policy", user_simulator="customer"),
                                  tools=dict(worker=[{"name": "book"}], user_simulator=[])))
        check_native(match, config, orch, native)
        for key in ("customer", "scoring"):
            with self.subTest(key=key), patch.dict(task, {key: "changed"}), self.assertRaises(FirstPassMismatch):
                check_native(match, config, orch, native)
        for obj, attr, change in ((env, "get_db_hash", lambda: "changed"), (orch.user, "system_prompt", "As before"),
                                  (orch.agent, "tools", [])):
            with self.subTest(field=attr), patch.object(obj, attr, change), self.assertRaises(FirstPassMismatch):
                check_native(match, config, orch, native)

    def test_only_assigned_memory_can_change_the_wire(self):
        policy = MemoryPolicy(True, "none", False, 1, read_mode="preload")
        items = [dict(id="one", version="saved", title="Flight", body="Earlier booking")]
        instruction = "Use past notes when applicable."
        systems = dict(worker="Airline rules", user_simulator="Customer scenario")
        opening = [dict(role="system", content=systems["user_simulator"]), dict(role="user", content="Hello")]
        customer = dict(model="customer", messages=opening)
        match = dict(control=dict(systems=systems), customer_opening_sha256=digest(canonical(customer)),
                     wire=dict(worker=dict(model="worker", seed=0, tools=[]), user_simulator=dict(model="customer")))
        verify = wire_verifier(match, policy, instruction, items)
        hint = [*opening, dict(role="user", content="Remember last time")]
        with self.assertRaises(FirstPassMismatch):
            verify("user_simulator", dict(customer, messages=hint), hint)
        verify("user_simulator", customer, opening)
        followup = [*opening, dict(role="assistant", content="Which flight?")]
        verify("user_simulator", dict(customer, messages=followup), followup)
        native = [dict(role="system", content=systems["worker"] + memory_system_suffix(policy, instruction)),
                  dict(role="user", content="Please find a flight")]
        messages = [native[0], snapshot_message(items), native[1]]
        worker = dict(model="worker", seed=0, tools=[], messages=messages)
        verify("worker", worker, native)
        for change in (dict(seed=2), dict(tools=[{"name": "unrelated"}]), dict(previous_response_id="other"),
                       dict(messages=[native[0], snapshot_message([]), native[1]]),
                       dict(messages=[dict(role="system", content="Extra business advice"), *messages[1:]]),
                       dict(messages=[*messages, dict(role="user", content="Use the previous split")]),
                       dict(messages=[*messages, dict(role="assistant", tool_calls=[{"function": {}}])])):
            with self.subTest(change=change), self.assertRaises(FirstPassMismatch):
                verify("worker", dict(worker, **change), native)

    @unittest.skipUnless(AVAILABLE and (ROOT / "runs/tau2-baseline-luna-deepseek1017-train-01").exists(),
                         "Requires the pinned airline benchmark and archived source")
    def test_different_solver_seed_is_rejected_before_any_model_or_output(self):
        cfg = json.loads(FIRST_PASS_DRAFT.read_bytes())
        cfg.update(status="ready", guards=self.config["guards"])
        cfg["task_sets"] = dict(development=["23"], evaluation=[])
        cfg["seed_pairs"][0]["native_and_solver"] = 2
        with TemporaryDirectory() as tmp, patch("agent_memory_eval.experiments.prompt_comparison.live_models") as catalog:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(cfg))
            with self.assertRaisesRegex(FirstPassMismatch, "config"):
                run_comparison(ROOT, ROOT, ROOT / "data/tau2-bench", path, live=True)
            catalog.assert_not_called()

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

    def test_first_pass_draft_schedules_only_memory_and_requires_limits_for_live(self):
        config = json.loads(FIRST_PASS_DRAFT.read_bytes())
        design = compile_config(ROOT, config)
        jobs = ordered_jobs(design)
        arms = {c["id"] for c in config["conditions"]}
        self.assertEqual(design["second_pass"]["matched_controls"], [])
        self.assertEqual(design["second_pass"]["baseline"], "archived_first_pass")
        for task in config["task_sets"]["development"]:
            for phase in ("write", "solve"):
                self.assertCountEqual((r["arm"] for r in jobs if r["task_id"] == task and r["phase"] == phase), arms)
        for changed in (dict(status="ready"), dict(baseline="new_no_memory")):
            with self.subTest(changed=changed), self.assertRaises(ContractError):
                compile_config(ROOT, dict(config, **changed))
        design["second_pass"]["matched_controls"] = ["no_memory"]
        with self.assertRaises(ContractError):
            ordered_jobs(design)

    def test_proposal_packet_hides_evaluation_and_only_the_cited_writer_is_changed(self):
        config = json.loads(FIRST_PASS_DRAFT.read_bytes())
        config["task_sets"] = {"development": ["21"], "evaluation": ["23"]}
        raw = json.dumps(config).encode()
        public = dict(messages=[dict(role="user", content="Please check the payment method.")])
        source = SimpleNamespace(public_context=json.dumps(public), public_context_sha256=digest(json.dumps(public).encode()),
                                 official_success=False)
        with patch("scripts.prepare_memory_candidate.load_form_sources", return_value={"21": source}) as load_sources:
            packet = development_packet(ROOT, ROOT, raw)
        self.assertEqual([s["task_id"] for s in load_sources.call_args.args[0]["source"]["cohort"]], ["21"])
        self.assertEqual(set(packet["development"]), {"21"})
        self.assertNotIn("evaluation", packet)
        citation = dict(task_id="21", pointer="/messages/0/content", quote="check the payment method")
        proposal = dict(parent_config_sha256=digest(raw), hypothesis="Current payment details can change.",
                        expected_action="Check the current details.", writer_prompt="Record what to recheck.",
                        evidence=[citation])
        draft = candidate_draft(ROOT, raw, packet, proposal)
        self.assertEqual(draft["conditions"][0], config["conditions"][0])
        self.assertEqual(draft["conditions"][1]["prompt"], proposal["writer_prompt"])
        self.assertEqual(draft["task_sets"], {"development": ["21"], "evaluation": []})
        self.assertTrue(all(r["task_id"] == "21" for r in ordered_jobs(compile_config(ROOT, draft))))
        self.assertIsNone(draft["guards"])
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft, candidate_draft(ROOT, raw, packet, proposal))
        for overrides in (dict(parent_config_sha256="0" * 64), dict(guards=config["guards"]),
                          dict(evidence=[dict(citation, task_id="23")]), dict(evidence=[dict(citation, quote="invented")]),
                          dict(evidence=[dict(citation, pointer="/first_pass_success")])):
            with self.subTest(overrides=overrides), self.assertRaises(ContractError):
                candidate_draft(ROOT, raw, packet, dict(proposal, **overrides))

    @unittest.skipUnless(AVAILABLE and (ROOT / "runs/tau2-baseline-luna-deepseek1017-train-01").exists(),
                         "Requires the pinned airline benchmark and archived source")
    def test_normal_entrypoint_reaches_both_writers_and_real_native_scorer(self):
        self.check_native_comparison(self.config, first_pass=False)

    def test_one_proposal_preserves_each_registered_representation(self):
        config = json.loads(FORM_PAIRS_DRAFT.read_bytes())
        config["forms"] = {"fact": "Keep observations.", "timeline": "Keep their order."}
        config["task_sets"] = {"development": ["21"], "evaluation": ["23"]}
        raw = canonical(config)
        source = SimpleNamespace(public_context='{"messages":[{"role":"user","content":"I tried to pay."}]}',
                                 public_context_sha256="1" * 64, official_success=False)
        with patch("scripts.prepare_memory_candidate.load_form_sources", return_value={"21": source}):
            packet = development_packet(ROOT, ROOT, raw)
        self.assertEqual({c["id"] for c in packet["incumbents"]}, set(config["forms"]))
        self.assertEqual(set(packet["development"]), {"21"})
        proposal = dict(parent_config_sha256=digest(raw), writer_clause="Preserve the observed outcome.",
                        hypothesis="Attempts can be mistaken for completion.", expected_action="Check the result.",
                        evidence=[dict(task_id="21", pointer="/messages/0/content", quote="tried to pay")])
        draft = candidate_draft(ROOT, raw, packet, proposal)
        design = compile_config(ROOT, draft)
        self.assertEqual(draft["forms"], config["forms"])
        self.assertEqual(draft["task_sets"], {"development": ["21"], "evaluation": []})
        self.assertIsNone(draft["guards"])
        for form, pair in design["representation_pairs"].items():
            lines = design["writing"]["lines"]
            self.assertEqual(lines[pair["reference"]], config["forms"][form])
            self.assertEqual(lines[pair["candidate"]], proposal["writer_clause"] + "\n" + lines[pair["reference"]])
        self.assertEqual(len(ordered_jobs(design)), 8)
        design["representation_pairs"]["timeline"]["candidate"] = "fact__candidate"
        with self.assertRaises(ContractError):
            ordered_jobs(design)
        for forms in ({"no_memory": "Reserved."}, {"fact__candidate": "A.", "fact": "B."}, {"fact": " "}):
            with self.subTest(forms=forms), self.assertRaises(ContractError):
                compile_config(ROOT, dict(draft, forms=forms))
        with self.assertRaises(ContractError):
            candidate_draft(ROOT, raw, packet, dict(proposal, evidence=[dict(proposal["evidence"][0], task_id="23")]))

    def test_writer_wire_pair_keeps_the_original_feedback_tools_and_generation_settings(self):
        wire = dict(messages=[dict(role="system", content="Save.\nTimeline."), dict(role="user", content="Evidence."),
                              dict(role="user", content="Failed.")], model="worker", seed=3, tools=[{"name": "memory_store"}])
        updated = paired_writer_request(wire, "Save.", "Timeline.", "Distinguish attempts.\nTimeline.")
        self.assertEqual(updated["messages"][0]["content"], "Save.\nDistinguish attempts.\nTimeline.")
        updated["messages"][0] = wire["messages"][0]
        self.assertEqual(updated, wire)
        with self.assertRaises(ContractError):
            paired_writer_request(wire, "Save.", "Facts.", "New instruction.")

    @unittest.skipUnless(all((ROOT / "runs" / name).is_dir() for name in
                             ("bfcl-relevance-two-pass-01-decoded", "tau-banking-multitarget-forms-01")),
                         "Requires the archived BFCL and banking writer inputs")
    def test_candidate_requests_match_the_two_benchmark_source_archives(self):
        expected = json.loads((ROOT / "artifacts/analyses/20261126-writer-pair-inputs.json").read_bytes())
        experiment = json.loads((ROOT / "artifacts/analyses/20261126-bfcl-writer-pairs.json").read_bytes())
        clause = (ROOT / "protocols/instructions/attempt-result-clause-v1.txt").read_text().rstrip("\n")
        self.assertEqual(clause, expected["proposed_shared_writer_clause"])
        requests = {}
        for benchmark, cases in expected["cases"].items():
            for task, case in cases.items():
                submitted = ({r["form"]: r["writer_request_sha256"] for r in experiment["tasks"][task]["allocations"]
                              if r["phase"] == "write"} if benchmark == "BFCL" else {})
                for form, item in case["writers"].items():
                    folder = (ROOT / item["journal"]).parent
                    if folder not in requests:
                        scan, _, _ = read_experiment(folder)
                        requests[folder] = {e["payload"]["request_ref"]["sha256"]: e["payload"]["request_ref"]
                                            for e in scan["events"].values() if e["type"] == "model.request_started"}
                    ref = requests[folder][item["original_request_sha256"]]
                    wire = BlobStore(folder).get(ref)
                    common, line = wire["messages"][0]["content"].rsplit("\n", 1)
                    candidate = paired_writer_request(wire, common, line, clause + "\n" + line)
                    self.assertEqual(digest(canonical(candidate)), item["proposed_unsent_request_sha256"])
                    if submitted:
                        pair = experiment["representation_pairs"][form]
                        self.assertEqual(submitted[pair["candidate"]], digest(canonical(candidate)))
                        self.assertEqual(submitted[pair["reference"]], ref["sha256"])

    @unittest.skipUnless(AVAILABLE and (ROOT / "runs/tau2-baseline-luna-deepseek1017-train-01").exists(),
                         "Requires the pinned airline benchmark and archived source")
    def test_form_pairs_reach_independent_readers_and_native_scores_from_the_original_opening(self):
        from agent_memory_eval.experiments.forms_batch import prepare
        config = json.loads(FORM_PAIRS_DRAFT.read_bytes())
        config.update(status="ready", guards=self.config["guards"], task_sets={"development": ["3"], "evaluation": []},
                      forms={"fact": "Keep observations.", "timeline": "Keep events in order."})
        design = compile_config(ROOT, config)
        plan = prepare(ROOT, ROOT, ROOT / "data/tau2-bench", design)
        models = json.loads((ROOT / config["endpoint_ref"]["path"]).read_bytes())["models"]
        solvers = NativeHTTP(user=lambda n: "###STOP###")
        writers = []
        def local_open(request, timeout):
            wire = json.loads(request.data)
            if wire.get("tool_choice") == "required":
                writers.append(wire)
                raw = response()
                raw.update(model=wire["model"], choices=store("memory-" + str(len(writers)))["choices"])
                return Reply(raw)
            return solvers(request, timeout)
        original_init = OpenRouterClient.__init__
        def inject(client, *args, **kwargs):
            original_init(client, *args, opener=local_open, **kwargs)
        ns = "agent_memory_eval.experiments.prompt_comparison."
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "runs").mkdir()
            cfg = workspace / "config.json"
            cfg.write_bytes(canonical(config))
            with patch.dict("os.environ", OPENROUTER_API_KEY="local-mock"), patch(ns + "prepare", return_value=plan), \
                    patch(ns + "implementation_identity", return_value={"implementation_commit": "fixture"}), \
                    patch(ns + "live_models", return_value=models), patch.object(OpenRouterClient, "__init__", inject):
                run_comparison(ROOT, workspace, ROOT / "data/tau2-bench", cfg, live=True)
            self.assertEqual(len(writers), 4)
            self.assertEqual(len({canonical(w["messages"][1:]) for w in writers}), 1)
            run = workspace / "runs" / config["name"]
            summary = publish_report(run, workspace / "report", source_workspace=ROOT)
            self.assertEqual(summary["first_pass_comparability"]["counts"], {"matched_graded": 4})
            opening = plan["first_pass_matches"]["3"]["opening_source"]["task_start"]["public_input"]["messages"]
            self.assertEqual([w["messages"][-2:] for w in solvers.by_role["worker"]], [opening[-2:]] * 4)
            self.assertTrue(all(len(w["messages"]) > 2 for w in solvers.by_role["user_simulator"]))
            self.assertEqual(summary["representation_pairs"], design["representation_pairs"])
            self.assertEqual(summary["paired_case_pages"], 2)
            self.assertEqual({(c["treatment"], c["control"]) for c in summary["contrasts"]["all"]},
                             {(p["candidate"], p["reference"]) for p in design["representation_pairs"].values()})
            self.assertTrue(all(c["jointly_graded"]["pairs"] == 1 for c in summary["second_pass_efficiency"]["all"]))
            readers = list(summary["retrieval_by_job"].values())
            self.assertTrue(all(r["body_spans"] > 0 for r in readers))
            self.assertEqual(len({r["writer_snapshot"] for r in readers}), 4)

            delivery_summary = self.offline_delivery(workspace, cfg, design, plan, models, inject)
            self.assertEqual(len(writers), 4)
            self.assertEqual(delivery_summary["first_pass_comparability"]["counts"], {"matched_graded": 8})
            by_arm = {r["arm"]: r for r in delivery_summary["retrieval_by_job"].values()}
            for reader in readers:
                for mode in ("preload", "on_demand"):
                    delivered = by_arm[reader["arm"] + "__" + mode]
                    self.assertEqual(delivered["writer_snapshot"], reader["writer_snapshot"])
                    self.assertEqual(delivered["body_spans"] > 0, mode == "preload")
            self.assertEqual(delivery_summary["paired_case_pages"], 8)

    @unittest.skipUnless(AVAILABLE and (ROOT / "runs/tau2-baseline-luna-deepseek1017-train-01").exists(),
                         "Requires the pinned airline benchmark and archived source")
    def test_first_pass_comparison_and_frozen_bank_retrieval_reach_the_offline_report(self):
        config = json.loads(FIRST_PASS_DRAFT.read_bytes())
        config.update(status="ready", guards=self.config["guards"])
        self.check_native_comparison(config, first_pass=True)

    def offline_delivery(self, workspace, cfg, design, plan, models, inject):
        config = json.loads(cfg.read_bytes())
        delivery = json.loads((ROOT / "protocols/pilots/tau2-fact-recheck-delivery-v1.json").read_bytes())
        fixture_root = workspace / "config-root"
        for ref in (config["base_design_ref"], design["writing"]["common_instruction_ref"], delivery["instruction_ref"]):
            path = fixture_root / ref["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            copyfile(ROOT / ref["path"], path)
        copyfile(cfg, fixture_root / "parent.json")
        delivery["parent_config_ref"] = dict(path="parent.json", sha256=digest(cfg.read_bytes()))
        delivery_cfg = workspace / "delivery.json"
        delivery_cfg.write_bytes(canonical(delivery))
        ns = "agent_memory_eval.experiments.prompt_delivery."
        with patch.dict("os.environ", OPENROUTER_API_KEY="local-mock"), patch(ns + "prepare", return_value=dict(plan)), \
                patch(ns + "implementation_identity", return_value={"implementation_commit": "fixture"}), \
                patch(ns + "live_models", return_value=models), patch.object(OpenRouterClient, "__init__", inject):
            run_delivery_comparison(fixture_root, workspace, ROOT / "data/tau2-bench", delivery_cfg, live=True)
        return publish_report(workspace / "runs" / delivery["name"], workspace / "delivery-report")

    def check_native_comparison(self, config, *, first_pass):
        from agent_memory_eval.experiments.forms_batch import prepare
        config["task_sets"] = {"development": [], "evaluation": ["3"]}
        config["seed_pairs"] = config["seed_pairs"][:1]
        config["conditions"][1]["prompt"] += "\nKeep observations distinct."
        config["conditions"][1]["id"] = "custom_fact"
        design = compile_config(ROOT, config)
        plan = prepare(ROOT, ROOT, ROOT / "data/tau2-bench", design)
        models = json.loads((ROOT / config["endpoint_ref"]["path"]).read_bytes())["models"]
        def memory_worker(number, wire):
            if not any(t["function"]["name"] == "memory_list" for t in wire["tools"]):
                return "You may bring 4."
            calls = [c for m in wire["messages"] for c in m.get("tool_calls") or []]
            name, args = "memory_list", {}
            if calls:
                content = json.loads(wire["messages"][-1]["content"])
                if calls[-1]["function"]["name"] != "memory_list" or not content.get("items"):
                    return "You may bring 4."
                name, args = "memory_get", {"id": content["items"][0]["id"]}
            return dict(role="assistant", content=None, tool_calls=[dict(id=f"read-{number}", type="function",
                        function=dict(name=name, arguments=json.dumps(args)))])
        writer_wires, solvers = [], NativeHTTP(worker=memory_worker)
        original_init = OpenRouterClient.__init__
        def local_open(request, timeout):
            wire = json.loads(request.data)
            if any(t["function"]["name"] == "memory_store" for t in wire.get("tools", [])):
                writer_wires.append(wire)
                raw = response()
                raw.update(model=wire["model"], provider="OpenAI")
                if "Keep observations distinct." in wire["messages"][0]["content"] and len(wire["messages"]) == 3:
                    raw["choices"] = store("local-handoff")["choices"]
                return Reply(raw)
            # The scripted customer starts and ends each independent native episode.
            if wire["model"].startswith("deepseek/") and len(wire["messages"]) == 2:
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
            self.assertEqual(len(writer_wires), 3)
            self.assertEqual({digest(w["messages"][0]["content"].encode()) for w in writer_wires},
                             {c["writer_instruction_sha256"] for c in design["conditions"]})
            self.assertEqual({r["arm"]:r["status"] for r in rows if r["phase"] == "write"},
                             {"custom_fact": "completed", "fact_original": "empty"})
            self.assertTrue(all(r["result"]["grading_status"] == "graded" for r in rows if r["phase"] == "solve"))
            directory = workspace / "report"
            summary = publish_report(workspace / "runs" / config["name"], directory, source_workspace=ROOT)
            self.assertEqual(summary["first_pass_comparability"]["counts"],
                             {"matched_graded": 2} if first_pass else {"unverified": 4})
            self.assertEqual(summary["first_pass_comparability"]["matched_source_runs"], int(first_pass))
            self.assertEqual(summary["first_pass_comparability"]["matched_source_groups"], int(first_pass))
            self.assertEqual(summary["paired_case_pages"], 1 if first_pass else 5)
            self.assertEqual(summary["contrasts"]["evaluation"][0]["jointly_graded_pairs"], 1)
            self.assertEqual(summary["costs_by_condition"]["fact_original"]["empty_writers"], 1)
            self.assertEqual(len(summary["native_grade_components"]), 2 if first_pass else 4)
            if first_pass:
                comparison = summary["second_pass_efficiency"]["evaluation"][0]
                self.assertEqual(comparison["jointly_graded"]["pairs"], 1)
                self.assertEqual(comparison["jointly_graded"]["metrics"]["usd"]["observed"], 1)
                previous = summary["first_pass_efficiency"]
                self.assertEqual(previous["status"], "observed")
                self.assertEqual(previous["by_task_set"]["evaluation"][0]["jointly_graded"]["metrics"]["customer_turns"]["observed"], 1)
                self.assertEqual({r["arm"] for r in rows if r["phase"] == "solve"}, {"fact_original", "custom_fact"})
                baseline = summary["baseline"]
                self.assertEqual(baseline["matched_graded_by_task_set"]["evaluation"], baseline["by_task_set"]["evaluation"])
                self.assertEqual(baseline["source_by_task"]["3"]["run"], design["source"]["cohort"][0]["run_id"])
                for arm in ("fact_original", "custom_fact"):
                    reference = baseline["source_by_task"]["3"]["success"]
                    observed = next(r["result"]["official_success"] for r in rows if r["phase"] == "solve" and r["arm"] == arm)
                    counts = baseline["by_task_set"]["evaluation"][arm]
                    self.assertEqual(counts["assigned"], 1)
                    self.assertEqual(counts["gain"], int(observed and not reference))
                    self.assertEqual(counts["loss"], int(reference and not observed))
                    self.assertEqual(counts["missing"], 0)
                self.assertEqual([(p["treatment"], p["control"]) for p in summary["contrasts"]["evaluation"]],
                                 [("custom_fact", "fact_original")])
            for grade in summary["native_grade_components"].values():
                self.assertTrue(grade["db_match"])
                self.assertEqual(grade["communication"][0]["info"], "4")
                self.assertIsNotNone(grade["communication"][0]["cited_turn"])
            for file in directory.rglob("*.md"):
                for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", file.read_text()):
                    self.assertTrue((file.parent / link.split("#")[0]).exists(), link)
            with self.assertRaises(ContractError):
                publish_report(workspace / "runs" / config["name"], directory)
            report = self.offline_delivery(workspace, cfg, design, plan, models, inject)
            self.assertEqual(len(writer_wires), 3, "Frozen-bank comparisons must never run a writer")
            rec = {v["arm"]: v for v in report["retrieval_by_job"].values()}
            if first_pass:
                self.assertEqual(report["baseline"]["source_by_task"], baseline["source_by_task"])
                self.assertEqual(report["first_pass_efficiency"]["status"], "unavailable")
                self.assertTrue(all(c["jointly_graded"]["metrics"]["usd"]["observed"] == 0
                                    for c in report["first_pass_efficiency"]["by_task_set"]["evaluation"]))
                get_job = next(job for job, data in report["retrieval_by_job"].items() if data["arm"] == "custom_fact__on_demand")
                choice = json.loads((workspace / "delivery-report/evidence" / f"choices-{get_job}.json").read_bytes())
                self.assertTrue(choice["first_body_choice"]["get_requested"])
                self.assertEqual(choice["first_body_choice"]["matched_list_entries"][0]["size"], 1)
            for condition in ("custom_fact", "fact_original"):
                q, pre = rec[condition + "__on_demand"], rec[condition + "__preload"]
                self.assertEqual(q["writer_snapshot"], pre["writer_snapshot"])
                self.assertEqual(pre["list_calls"], 0)
                self.assertEqual(q["list_calls"], 1)
            self.assertEqual(rec["custom_fact__on_demand"]["get_calls"], 1)
            self.assertGreater(rec["custom_fact__on_demand"]["body_spans"], 0)
            self.assertEqual(rec["fact_original__on_demand"]["body_spans"], 0)
