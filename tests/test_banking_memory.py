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


class BankingCohortTests(unittest.TestCase):
    def test_failed_episode_only_reports_the_observed_public_prefix(self):
        from agent_memory_eval.analysis.tau2_attribution import recorded_episode
        from agent_memory_eval.recording.batch import sealed_rows
        study = ROOT / "runs/tau-banking-source-screen-01"
        if not (study / "completion.json").is_file():
            self.skipTest("Requires the archived interrupted banking first pass")
        read = banking.matched.read
        case = next(c for c in read(study / "registration.json")["preflight"]["cases"]
                    if c["task"] == "task_018__source-1")
        stage = "task-" + case["task"] + "/source"
        seal = read(study / "completion.json")["stages"][stage]["seal"]
        _, _, (row,) = sealed_rows(study / stage, seal)
        neutral = read(ROOT / "protocols/pilots/tau2-fresh-matched-v1.json")["delivery_instruction"]["sha256"]
        self.assertIsNone(row["result"]["official_success"])
        steps, effort = recorded_episode(study / stage, row, case, neutral, include_partial=True)
        self.assertEqual(steps[0].actor, "worker")
        self.assertGreater(len(steps), 1)
        self.assertIsNone(effort["business_calls"])

    @unittest.skipUnless(AVAILABLE and (ROOT / "runs/tau-banking-source-screen-01/completion.json").exists(),
                         "Requires the pinned native bank and preserved first passes")
    def test_business_replay_distinguishes_customer_errors_and_executed_disputes(self):
        from agent_memory_eval.adapters.banking_business import BankingBusiness
        from agent_memory_eval.recording.batch import sealed_rows
        from agent_memory_eval.recording.journal import BlobStore
        banking.matched.modules_for(ROOT)
        from tau2.domains.banking_knowledge.environment import get_tasks
        study = ROOT / "runs/tau-banking-source-screen-01"
        tasks = {t.id: t for t in get_tasks()}
        registered = json.loads((study / "registration.json").read_bytes())["preflight"]["cases"]
        close = json.loads((study / "completion.json").read_bytes())
        observed = {}
        for case in registered:
            task = case["task_id"]
            if task not in {"task_037", "task_083"}:
                continue
            name = "task-" + case["task"] + "/source"
            _, _, (assignment,) = sealed_rows(study / name, close["stages"][name]["seal"])
            result = assignment["result"]
            native = BlobStore(study / name / assignment["directory"]).get(result["report_ref"])
            control = BankingBusiness(tasks[task], case)
            observed[task, case["source_draw"]] = control.observe(native["messages"], result["final_db_hash"],
                                                                  native["reward_info"])
            if task == "task_083":
                self.assertEqual(len(control.reference_errors), 4)
        user_failure = observed["task_037", 2]
        self.assertEqual(user_failure["customer_extra_final_rows"], 1)
        self.assertTrue(any(c["actor"] == "user" and c["receipt"] == "reported_error" and c["changed_rows"]
                            and c.get("matches_suggested_arguments") is False for c in user_failure["calls"]))
        success = observed["task_037", 3]
        self.assertEqual((success["customer_extra_final_rows"], success["official_action_matches"],
                          success["normalized_actor_matches"]), (0, 4, 7))
        self.assertEqual([observed["task_083", i]["debit_disputes"]["target_opened"] for i in (1, 2, 3)], [4, 2, 3])

    def test_registration_and_initial_release_ignore_outcomes(self):
        from agent_memory_eval.adapters.banking_memory_cohort import allocation_counts, cohort_slots, first_release
        read = banking.matched.read
        spec = read(ROOT / banking.COHORT_PROFILE)
        split = read(ROOT / spec["population"]["path"])["split"]
        history = read(ROOT / spec["previous_results"]["path"])
        parent = dict(cases=[dict(task=f"{r['task']}__source-{r['draw']}", task_id=r["task"], source_draw=r["draw"],
                                  customer=split[r["task"]]["customer"] or "unresolved-" + r["task"])
                             for r in history["observations"]])
        slots, missing = cohort_slots(spec, split, history, parent)
        self.assertEqual((len(slots), len(missing)), (276, 6))
        initial = first_release(slots)
        self.assertEqual(len(initial), 45)
        self.assertEqual(allocation_counts([r for r in slots if r["task"] in initial], 1),
                         dict(reused_first_pass=45, source=0, write=225, solve=450, second_pass_no_memory=0))
        for row in history["observations"]:
            if row["success"] is not None:
                row["success"] = not row["success"]
        self.assertEqual(cohort_slots(spec, split, history, parent), (slots, missing))
        self.assertEqual(first_release(list(reversed(slots))), initial)

    def test_release_check_uses_coverage_and_exposure(self):
        from agent_memory_eval.adapters.banking_memory_cohort import release_check
        rules = banking.matched.read(ROOT / banking.COHORT_PROFILE)["release_check"]
        arm = dict(assigned=225, graded=225, dispatched=225, body_at_open=225, tasks_exposed=45)
        arms = dict(preload=arm, on_demand=dict(arm, body_at_open=0, tasks_exposed=0))
        self.assertEqual(release_check(arms, rules)["reasons"], ["optional_body_task_coverage"])
        arms["on_demand"]["tasks_exposed"] = 1
        self.assertEqual(release_check(arms, rules)["reasons"], ["optional_body_task_coverage"])
        arms["on_demand"].update(tasks_exposed=5, graded=200)
        self.assertEqual(release_check(arms, rules)["reasons"], ["grade_coverage"])
        arms["on_demand"]["graded"] = 210
        arm["body_at_open"] = 224
        self.assertEqual(release_check(arms, rules)["reasons"], ["preload_body_coverage"])
        arm["body_at_open"] = 225
        self.assertTrue(release_check(arms, rules)["meets_readiness_targets"])

    def test_cohort_scorer_reproduces_the_published_forty_consultations(self):
        import os
        from agent_memory_eval.adapters.banking_memory_cohort import arm_summary, observation
        from agent_memory_eval.experiments.forms_write import read_form_bank
        from agent_memory_eval.recording.batch import sealed_rows
        runs = Path(os.environ.get("AGENT_MEMORY_ARCHIVE", ROOT / "runs"))
        root = runs / "tau-banking-required-01"
        if not (root / "completion.json").is_file():
            self.skipTest("Requires the sealed recorded banking pilot")
        published = banking.matched.read(ROOT / "artifacts/analyses/20261124-banking-required-results.json")
        reg = banking.matched.read(root / "registration.json")
        self.assertEqual(digest(canonical(reg["preflight"])), published["registration_sha256"])
        self.assertEqual(digest((root / "registration.json").read_bytes()),
                         banking.matched.read(root / "completion.json")["registration_sha256"])
        checked = {"preload": [], "on_demand": []}
        for case in reg["preflight"]["cases"]:
            tid = case["task_id"]
            old = case["imported_first_pass"]
            source_dir = runs / old["directory"]
            _, _, (source_row,) = sealed_rows(source_dir)
            self.assertEqual(source_row["result"]["log_sha256"], old["log_sha256"])
            source = observation(source_dir, source_row)
            task = root / ("task-" + case["task"])
            _, _, written = sealed_rows(task / "write")
            banks = {w["arm"]: read_form_bank(task / "write" / w["directory"], expected_log_sha256=w["result"]["log_sha256"])
                     for w in written}
            _, _, rows = sealed_rows(task / "solve")
            for row in rows:
                form, mode = row["arm"].split("__")
                obs = observation(task / "solve", row, writer=banks[form])
                self.assertEqual((obs["start"], obs["start_source"]), (source["start"], source["run"]))
                checked[mode].append(dict(obs, task=tid))
        summaries = {k: arm_summary(v) for k, v in checked.items()}
        for mode, wins, exposed in (("preload", 8, 20), ("on_demand", 7, 0)):
            self.assertEqual([summaries[mode][k] for k in ("graded", "successes", "with_body")], [20, wins, exposed])


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
        omit_procedure = False
        feedback_messages = []
        tool_only_questions = []
        descriptions = {"memory_list": "Browse earlier experience when relevant.", "memory_get": "Read one experience by id."}

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
                if len(messages) == 3:
                    feedback_messages.append(json.loads(messages[2]["content"]))
                if omit_procedure and messages[0]["content"].endswith(strict["writing"]["lines"]["procedure"]):
                    text = "First clarify the terms. Then look up the current policy."
                elif messages[-1]["role"] != "tool":
                    call = ("memory_store", {"title": "Observation", "body": "private sample note"})
            else:
                self.assertNotIn('"kind":"evaluation_feedback"', str(messages))
                self.assertTrue(any(m.get("content") == "Please help with my card." for m in messages))
                schemas = {t["function"]["name"]: t["function"]["description"] for t in wire.get("tools", [])}
                if schemas.get("memory_list") == descriptions["memory_list"]:
                    self.assertEqual(schemas["memory_get"], descriptions["memory_get"])
                    self.assertNotIn("<memory_policy>", messages[0]["content"])
                    if len(messages) == 3:
                        tool_only_questions.append(messages)
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
            wrong_prompt = deepcopy(design)
            wrong_prompt["writing"]["lines"]["fact"] = wrong_prompt["writing"]["lines"]["procedure"]
            with self.assertRaises(ContractError):
                verify_required_form_sets(wrong_prompt, plan, wdir, bindings)
            from dataclasses import replace
            with self.assertRaises(ContractError):
                verify_required_form_sets(design, dict(plan, sources={case["task_id"]: replace(source, source_run_id="another")}),
                                          wdir, bindings)

            from agent_memory_eval.adapters.banking_memory_cohort import allocation_counts, cohort_results
            from scripts.run_full_retail import accounting
            from scripts.run_recheck_rounds import fixed
            imported = dict(case, source_draw=1, cohort="development", scheduled_stages=["write", "solve"])
            frozen = dict(series="fixture-cohort", protocol_sha256=digest(b"scripted bank comparison"), historical_missing=[])
            release_plan = dict(frozen, cases=[imported], assigned=allocation_counts([imported], 1), release="first",
                                authorization=dict(total_usd="1.50"), prior_release=None, prior_covered_usd="0")
            result_cfg = dict(strict, writer_feedback_level="result_only", query_tool_descriptions=descriptions,
                              registration_sha256=digest(canonical(release_plan)))
            binary_design = study.design_for(result_cfg, case, "solve", source)
            with self.assertRaisesRegex(ContractError, "another feedback condition"):
                verify_required_form_sets(binary_design, plan, wdir, bindings)
            self.assertEqual(feedback_messages, [source.feedback_message("none")] * 5)
            binary = workspace/"runs/fixture-cohort-first"
            binary.mkdir()
            for stage in ("write", "solve"):
                study.stage_job((workspace, binary, result_cfg, {}, conn, imported, tasks[case["task"]], stage,
                                 {"fixture": True}, models, None, "1.50"))
            self.assertEqual(feedback_messages[5:], [source.feedback_message("result_only")] * 5)
            _, _, binary_readers = study.sealed_rows(binary/("task-" + case["task"])/"solve")
            self.assertEqual(len(binary_readers), 10)
            self.assertEqual([(r["arm"], r["result"]["error_code"]) for r in binary_readers
                              if r["result"]["grading_status"] != "graded"], [])
            self.assertEqual(len(tool_only_questions), 5)
            self.assertTrue(all(q == tool_only_questions[0] for q in tool_only_questions))
            queried = next(r for r in binary_readers if r["arm"] == "fact__on_demand")
            steps, _ = graded_episode(binary/("task-" + case["task"])/"solve", queried, case,
                                      cfg["delivery_instruction"]["sha256"], results["source"][0],
                                      query_tool_descriptions=descriptions)
            self.assertEqual(ref[0].history, steps[0].history)
            from decimal import Decimal
            metrics = {f"task-{case['task']}/{s}": accounting(binary/("task-" + case["task"])/s, "1.50")
                       for s in ("write", "solve")}
            fixed(binary/"registration.json", dict(preflight=release_plan))
            fixed(binary/"completion.json", dict(stages=metrics, registration_sha256=digest((binary/"registration.json").read_bytes()),
                                                total_covered_usd=str(sum(Decimal(v["spent"]) for v in metrics.values()))))
            report = cohort_results(workspace, frozen, dict(minimum_graded_fraction_per_delivery=0.9,
                require_every_dispatched_preload_has_body=True, minimum_distinct_tasks_with_on_demand_body=1), full=False,
                decision_interface=dict(read_instruction_sha=cfg["delivery_instruction"]["sha256"],
                                        query_tool_descriptions=descriptions))
            self.assertEqual(report["delivery"]["on_demand"]["with_body"], 5)
            self.assertEqual(report["delivery"]["on_demand"]["with_body_and_response"], 5)
            self.assertEqual(report["delivery"]["on_demand"]["body_at_open"], 0)
            self.assertEqual(report["delivery"]["preload"]["body_at_open"], 5)
            self.assertTrue(report["first_release_check"]["meets_readiness_targets"])
            self.assertEqual(report["sensitivity_without_083"], report["tables"]["preload_minus_on_demand"])
            trials = report["releases"]["first"]["readers"]
            decisions = report["releases"]["first"]["decision_pairs"]
            self.assertEqual(len(decisions), 5)
            self.assertTrue(all(p["first_worker_choice"]["status"] == "matched_visible_memory" for p in decisions))
            self.assertTrue(all(p["reference"] and p["candidate"] and p["comparison"]["worker_inputs"] for p in decisions))
            for writer in report["releases"]["first"]["writers"]:
                evidence = writer["evidence"]
                self.assertTrue(evidence["version_sha256"] and evidence["body_chars"] > 0)
                self.assertLessEqual(len(evidence["stored_source_literals"]), evidence["source_literal_candidates"])
            self.assertEqual(report["tables"]["delivery_pairs"]["development"]["both_success"]["metrics"]
                             ["question_turns"]["observed"], 5)
            self.assertTrue(all(t["business"]["verified_db_sha256"] and t["first_business"]["request_sha256"]
                                and t["first_action_literals"] is not None for t in trials))
            self.assertTrue(all(any(c["request"] for c in t["business"]["calls"]) for t in trials))
            self.assertEqual(report["tables"]["business_delivery_pairs"]["development"]["jointly_graded"]["pairs"], 5)

            # Exercise the missing form through the normal controller, including its durable allocations.
            omit_procedure = True
            rejected = output/"failed-form"
            rejected.mkdir()
            for stage in ("write", "solve"):
                study.stage_job((workspace, rejected, strict, {}, conn, case, tasks[case["task"]], stage,
                                 {"fixture": True}, models, None, "1.50"))
            failed_task = rejected/("task-" + case["task"])
            bad_writer = next(w for w in study.sealed_rows(failed_task/"write")[2] if w["arm"] == "procedure")
            self.assertEqual(bad_writer["result"]["error_code"], "WRITER_REQUIRED_STORE_MISSING")
            close, _, skipped = study.sealed_rows(failed_task/"solve")
            self.assertEqual((close["http_calls"], close["reported_usd"]), (0, 0))
            self.assertEqual([r["reason"] for r in skipped], ["required_form_set_incomplete"] * 10)
