"""Scripted transports with native retail tasks, database, evaluator and durable journals."""

import importlib.util
import time
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent_memory_eval.analysis.paired_policy import customer_contrast
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.models.openrouter import ProviderError, request_reservation_usd
from agent_memory_eval.recording.journal import BlobStore, Journal
from scripts.audit_retail_two_visit_billing import audit as audit_billing
from scripts.analyze_retail_conditional_retrieval import score_session, summarize as summarize_conditional
from scripts.run_retail_conditional_retrieval import DRAFT as CONDITIONAL_DRAFT
from scripts.analyze_retail_two_visit import (
    decision,
    differences,
    grade_turns,
    public_turns,
)
from scripts.prepare_retail_order_pairs import current_cases
from scripts.retail_two_visit_episode import (
    build_episode,
    continue_decision,
    first_decision,
    first_wire,
    policy_for,
    run_source,
    run_writer,
)
from scripts.run_full_retail import modules_for
from scripts.run_retail_two_visit import (
    DRAFT,
    ExpenseLedger,
    execute,
    read,
    recover_finished,
    sealed_bank,
)


def call(name, **args):
    return {
        "id": "call_" + name,
        "type": "function",
        "function": {"name": name, "arguments": canonical(args).decode()},
    }


class Replies:
    offline = True

    def __init__(self, responses, provider):
        self.responses = iter(responses)
        self.provider = provider
        self.wires = []

    def respond(self, wire):
        self.wires.append(wire)
        answer = next(self.responses)
        message = {"role": "assistant", "content": answer if isinstance(answer, str) else None}
        if isinstance(answer, list):
            message["tool_calls"] = answer
        return {
            "id": "offline-" + str(len(self.wires)),
            "model": wire["model"],
            "provider": self.provider,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if isinstance(answer, list) else "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14, "cost": 0},
        }


@unittest.skipUnless(
    importlib.util.find_spec("tau2") and (ROOT / "data/tau2-bench/src/tau2").exists(), "requires pinned tau2"
)
class NativeTwoVisitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = modules_for(ROOT)
        cls.protocol = read(ROOT / DRAFT)
        diagnostic = read(ROOT / cls.protocol["diagnostic"]["path"])
        retail = read(ROOT / cls.protocol["retail"]["path"])
        base = read(ROOT / retail["base_design"]["path"])
        cls.connection = read(ROOT / base["model"]["connection_profile_ref"]["path"])
        cls.models = read(ROOT / retail["endpoints"]["path"])["models"]
        for role in cls.models:
            cls.connection[role]["model_id"] = "synthetic-offline"
            cls.models[role].update(id="synthetic-offline", canonical_slug="synthetic-offline")
        db = read(ROOT / diagnostic["inputs"]["db"]["path"])
        first = read(ROOT / cls.protocol["cohort"]["path"])["reserved"][0]
        user = next(u for k, u in db["users"].items() if digest(k.encode()) == first["customer"])
        from tau2.domains.retail.environment import get_environment

        profile = (
            get_environment().make_tool_call("get_user_details", user_id=user["user_id"]).model_dump(mode="json")
        )
        cls.tasks, cls.prefixes, cases = current_cases(
            diagnostic["reserve"], user, {oid: db["orders"][oid] for oid in first["order_pair"]}, profile
        )
        cls.case = next(r for r in cases if r["mode"] == "previous_only")
        cls.other = next(oid for oid in first["order_pair"] if oid != cls.case["target"])
        cls.item = {
            "id": "m_order",
            "version": "v_order",
            "title": "Private test title",
            "body": "PRIVATE_TEST_BODY",
        }

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.prefix = self.prefixes[self.case["public_prefix_sha256"]]
        self.task = self.tasks[self.case["task"]]

    def runtime(self, worker, user):
        clients = {
            r: Replies(replies, self.models[r]["selected_endpoint"]["provider_name"])
            for r, replies in (("worker", worker), ("user_simulator", user))
        }
        return {
            "protocol": self.protocol,
            "models": deepcopy(self.models),
            "connection": self.connection,
            "clients": clients,
            "modules": self.modules,
            "guard": None,
            "instructions": {"preload": "Notes from the past.", "query": "Optional past notes."},
        }

    def job(self, arm):
        return {
            "run": "r_" + arm,
            "stage": "decision",
            "arm": arm,
            "task": self.case["task"],
            "target": self.case["target"],
            "prefix": self.case["public_prefix_sha256"],
        }

    def test_native_first_inputs_and_customer_isolation(self):
        wires = {}
        for arm in self.protocol["arms"]:
            with self.subTest(arm=arm):
                job = self.job(arm)
                rt = self.runtime([[call("get_order_details", order_id=self.case["target"])]], [])
                items = [self.item] if arm in {"preload", "query"} else []
                with Journal(self.root / arm, job["run"], contract_version="1.2") as journal:
                    first_decision(journal, job, self.task, self.prefix, rt, items, [], keep=False)
                    self.assertFalse(journal.scan["incomplete_runs"])
                self.assertEqual(rt["clients"]["user_simulator"].wires, [])
                wires[arm] = deepcopy(rt["clients"]["worker"].wires[0])
        self.assertEqual(wires["query"], wires["empty_query"])
        self.assertIn(self.item["body"], str(wires["preload"]["messages"].pop(1)))
        self.assertNotIn(self.item["title"], str(wires["empty_preload"]["messages"].pop(1)))
        self.assertEqual(wires["preload"], wires["empty_preload"])
        self.assertEqual(wires["no_memory"]["messages"][1:], self.prefix)

    def test_private_reads_and_pending_business_survive_close_and_reopen(self):
        job = self.job("query")
        oid = self.case["target"]
        rt = self.runtime(
            [
                [call("memory_list")],
                [call("memory_get", id=self.item["id"]), call("get_order_details", order_id=oid)],
                "Shall I cancel " + oid + " because it is no longer needed?",
                [call("cancel_pending_order", order_id=oid, reason="no longer needed")],
                "Cancelled.",
            ],
            ["Yes, please cancel that order.", "###STOP###"],
        )
        with Journal(self.root, job["run"], contract_version="1.2") as journal:
            record = first_decision(journal, job, self.task, self.prefix, rt, [self.item], [], keep=True)
            calls = rt["clients"]["worker"].wires
            self.assertEqual(len(calls), 2)
            self.assertNotIn(self.item["title"], str(calls[0]))
            self.assertIn(self.item["title"], str(calls[1]))
            self.assertNotIn(self.item["body"], str(calls[1]))
        self.assertEqual(recover_finished(self.root, "decision", job, True), record)
        measured = decision(self.root, job, record)
        self.assertEqual(measured["behavior"], "includes_target_with_other_calls")
        self.assertTrue(measured["title_in_input"])
        self.assertFalse(measured["body_in_input"])
        with Journal(self.root, job["run"], contract_version="1.2") as journal:
            with self.assertRaises(ContractError):
                continue_decision(journal, job, dict(self.task, id="wrong_task"), self.prefix, rt, record)
            actual = continue_decision(journal, job, self.task, self.prefix, rt, record)
            self.assertFalse(journal.scan["incomplete_runs"])
            trace = journal.blobs.get(actual["trajectory"])
            self.assertEqual(
                [t["name"] for m in trace for t in (m.get("tool_calls") or [])].count("cancel_pending_order"), 1
            )
            self.assertEqual(actual["db_reward"], 1)
            turns = public_turns(trace)
            proposal = next(i for i, t in enumerate(turns) if t.get("text", "").startswith("Shall I cancel"))
            consent = next(i for i, t in enumerate(turns) if t.get("text", "").startswith("Yes, please"))
            labels = {
                "status": "reviewed",
                "turns_sha256": digest(canonical(turns)),
                "clarifications": [],
                "proposals": [{"turn": proposal, "order": oid, "reason": "no longer needed"}],
                "responses": [{"turn": consent, "proposal": proposal, "disposition": "confirmed"}],
            }
            self.assertTrue(grade_turns(turns, oid, actual["db_reward"], labels)["consent_qualified"])
            labels["responses"][0]["disposition"] = "refused"
            self.assertFalse(grade_turns(turns, oid, 1, labels)["consent_qualified"])
            self.assertNotIn(self.item["body"], str(turns))
            self.assertEqual(len(rt["clients"]["worker"].wires), 5)
            self.assertIn(self.item["body"], str(rt["clients"]["worker"].wires[2]))
            self.assertNotIn(self.item["body"], str(rt["clients"]["user_simulator"].wires))
            with self.assertRaises(ContractError):
                continue_decision(journal, job, self.task, self.prefix, rt, record)

    def test_first_source_and_voluntary_writers(self):
        source_job = {"stage": "source", "arm": "no_memory", "run": "r_source"}
        src = self.tasks[self.case["source"]]
        rt = self.runtime(
            ["Here are the details."], [src["user_scenario"]["instructions"]["reason_for_call"], "###STOP###"]
        )
        folder = self.root / source_job["run"]
        with Journal(folder, source_job["run"], contract_version="1.2") as journal:
            record = run_source(journal, source_job, src, rt)
        self.assertEqual(record["status"], "completed")
        sha = digest((folder / "log.jsonl").read_bytes())
        for name, replies, expected in (
            ("fact", [[call("memory_store", title="Observed", body="Past details.")], "Done"], "completed"),
            ("lesson", ["Nothing to save."], "empty"),
        ):
            writer = {"stage": "writer", "arm": name, "run": "r_" + name}
            wrt = self.runtime(replies, [])
            path = self.root / writer["run"]
            with Journal(path, writer["run"], contract_version="1.2") as journal:
                result = run_writer(journal, writer, wrt, folder, source_job["run"], sha, "Write optional notes.")
            self.assertEqual(result["status"], expected)
            self.assertEqual(len(sealed_bank(path, result, writer["run"])), int(expected == "completed"))
            submitted = str(wrt["clients"]["worker"].wires[0])
            self.assertIn("Here are the details.", submitted)
            self.assertNotIn("official_success", submitted)
            self.assertNotIn(src["user_scenario"]["instructions"]["task_instructions"], submitted)

    def test_followup_uses_original_decision_once_and_audits_only_subsequent_charges(self):
        from agent_memory_eval.experiments.forms_batch import endpoint_terms, write_json
        from agent_memory_eval.study.adapters import retail_revisit as followup
        from agent_memory_eval.study.adapters.tau_banking import approval_for
        from agent_memory_eval.study.plan import FOLLOWUP_FORMAT

        original = self.root / "runs" / "prior"
        original.mkdir(parents=True)
        oid = self.case["target"]
        jobs, receipts, prior_scores = [], {}, {}
        for arm in ("query", "empty_query"):
            job = dict(self.job(arm), in_sample=True, mode="previous_only", customer="customer", source=self.case["source"])
            session = original / job["run"]
            rt = self.runtime([[call("get_order_details", order_id=oid)]], [])
            with Journal(session, job["run"], contract_version="1.2") as journal:
                result = first_decision(journal, job, self.task, self.prefix, rt,
                                        [self.item] if arm == "query" else [], [], keep=True)
                source_session = journal.scan["starts"][job["run"]]["session_id"]
            raw = (session / "log.jsonl").read_bytes()
            receipt = dict(result, journal_bytes=len(raw), journal_sha256=digest(raw))
            write_json(session / "decision.json", receipt)
            jobs.append(job)
            receipts[job["run"]] = receipt
            prior_scores[job["run"]] = dict(session=source_session, first_lookup=False, focused=True, included=[],
                                            decision_request=result["request"], confirmed_usd="0.001", held_usd="0")

        comparison = dict(memory="query", empty="empty_query", primary_mode="previous_only", primary_outcome="consent_without_reasking")
        scoring = read(ROOT / "protocols/studies/retail-revisit-followup-02.json")["scoring"]
        plan = dict(format=FOLLOWUP_FORMAT, id="followup", models={}, comparison=comparison, scoring=scoring,
                    execution={"attempts_per_checkpoint": 1, "pause_after_consecutive_transient_errors": 2})
        record = dict(assigned=dict(registered=2, continuable=2), calibration={})
        native = dict(protocol=self.protocol, connection=self.connection, instructions=rt["instructions"],
                      tasks={self.task["id"]: self.task}, public=self.prefixes, modules=self.modules)
        data = dict(record=record, native=native, jobs=jobs, upstreams=receipts, prior_scores=prior_scores,
                    original=original, uncertainty=None)
        terms = endpoint_terms(self.models)
        profile = dict(expected_revisions={r: m["canonical_slug"] for r, m in self.models.items()}, endpoint_terms=terms)
        approved = approval_for(plan, record, max_usd="1", user_authorization="synthetic test")

        class MeteredReplies:
            offline = True

            def __init__(self, model, guard, **kwargs):
                self.model, self.guard = model, guard
                self.streams = {}

            def respond(self, wire):
                self.guard.before(wire, self.model)
                key = self.guard.run_id
                if key not in self.streams:
                    worker = self.model["selected_endpoint"]["provider_name"] == terms["worker"]["provider_name"]
                    replies = (["Shall I cancel " + oid + " because it is no longer needed?",
                                [call("cancel_pending_order", order_id=oid, reason="no longer needed")], "Cancelled."]
                               if worker else ["Yes, please cancel that order.", "###STOP###"])
                    if worker and key == jobs[0]["run"]:
                        replies = [[call("memory_list")], [call("memory_get", id="m_order")], *replies]
                    self.streams[key] = Replies(replies, self.model["selected_endpoint"]["provider_name"])
                answer = self.streams[key].respond(wire)
                answer["id"] = "test-followup-" + str(self.guard.calls)
                self.guard.after(answer)
                return answer

        adapter = followup.RetailRevisit()
        with (patch.object(adapter, "resolve", return_value=data),
              patch.dict("os.environ", {"OPENROUTER_API_KEY": "synthetic"}),
              patch.object(followup, "implementation_identity", return_value={"implementation_commit": "fixture"}),
              patch.object(followup, "model_profile", return_value=profile),
              patch.object(followup, "live_models", return_value=self.models),
              patch.object(followup, "OpenRouterClient", MeteredReplies)):
            with self.assertRaises(ContractError):
                adapter.run(plan, self.root, ROOT, approval=dict(approved, resolved_sha256="different"))
            limited = dict(plan, id="low-budget")
            grant = approval_for(limited, record, max_usd="0.0000001", user_authorization="synthetic test")
            for _ in range(3):
                paused = adapter.run(limited, self.root, ROOT, approval=grant)
                self.assertEqual((paused["reserved_calls"], paused["pause_reason"]), (0, "study_limit"))
            pending, partial = adapter.analyze(limited, self.root, ROOT)
            self.assertEqual([r["status"] for r in pending], ["unstarted", "unstarted"])
            self.assertEqual(partial["phase"], "partial")
            for job in jobs:
                folder = self.root / "runs/low-budget" / job["run"]
                self.assertFalse((folder / "continuation.started").exists())
                self.assertEqual(digest((folder / "log.jsonl").read_bytes()), receipts[job["run"]]["journal_sha256"])
            result = adapter.run(plan, self.root, ROOT, approval=approved)
            self.assertEqual(result["reserved_calls"], 12)
            self.assertEqual(adapter.run(plan, self.root, ROOT, approval=approved), result)
            ledger = self.root / "runs/followup/expense.jsonl"
            first_reservation = read_json(ledger.read_bytes().splitlines()[0])["reserved"]
            partial_plan = dict(plan, id="partly-funded")
            grant = approval_for(partial_plan, record, max_usd=first_reservation, user_authorization="synthetic test")
            partial = adapter.run(partial_plan, self.root, ROOT, approval=grant)
            self.assertEqual(partial["pause_reason"], "study_limit")
            self.assertGreater(partial["reserved_calls"], 0)
            self.assertEqual(partial["stages"]["continuation"], {"error": 1})
            self.assertEqual(adapter.run(partial_plan, self.root, ROOT, approval=grant), partial)
            partially_funded, card = adapter.analyze(partial_plan, self.root, ROOT)
            self.assertEqual([r["status"] for r in partially_funded], ["error", "unstarted"])
            self.assertEqual(card["phase"], "partial")
            packet_file = self.root / "blind.json"
            rows, card = adapter.analyze(plan, self.root, ROOT, review_packets=packet_file)
            self.assertEqual(card["continuation_billing"]["verified_submissions"], 12)
            self.assertEqual([r["continuation_requests"] for r in rows], [7, 5])
            self.assertTrue(all(r["db_success"] for r in rows))
            self.assertTrue(all(r["consent_without_reasking"] is None for r in rows))
            self.assertEqual(card["phase"], "awaiting_transcript_review")
            self.assertTrue(rows[0]["body_seen"])
            exposure = rows[0]["memory_exposure"]
            self.assertFalse(exposure["first_decision"]["body_in_input"])
            self.assertTrue(exposure["continuation"]["body_in_input"])
            self.assertTrue(any(s["field"] == "body" and s["return_event"] for s in exposure["continuation"]["input_spans"]))
            self.assertFalse(rows[1]["body_seen"])
            group = next(r for r in card["by_mode_arm"] if r["arm"] == "query")
            self.assertEqual(group["memory_exposure"]["whole_consultation"]["body_in_input"], 1)
            labels = {}
            for key, packet in read(packet_file).items():
                self.assertEqual(set(packet), {"turns_sha256", "turns"})
                turns = packet["turns"]
                offer = next(i for i, t in enumerate(turns) if t.get("text", "").startswith("Shall I cancel"))
                yes = next(i for i, t in enumerate(turns) if t.get("text", "").startswith("Yes, please"))
                labels[key] = dict(turns_sha256=key, status="reviewed", method="blinded_direct_read",
                                   clarifications=[], proposals=[dict(turn=offer, order=oid, reason="no longer needed")],
                                   responses=[dict(turn=yes, proposal=offer, disposition="confirmed")])
            reviews = self.root / "reviews.json"
            key = next(iter(labels))
            write_json(reviews, {**labels, key: dict(labels[key], method="automatic")})
            with self.assertRaisesRegex(ContractError, "direct blinded review"):
                adapter.analyze(plan, self.root, ROOT, reviews=reviews)
            write_json(reviews, {**labels, key: dict(turns_sha256=key, status="unresolved", method="blinded_direct_read")})
            unresolved, card = adapter.analyze(plan, self.root, ROOT, reviews=reviews)
            self.assertEqual(card["phase"], "scored")
            self.assertTrue(any(r["consent_without_reasking"] is None for r in unresolved))
            self.assertEqual(card["contrasts"]["consent_without_reasking"]["pairs"]["missing"], 1)
            write_json(reviews, labels)
            _, card = adapter.analyze(plan, self.root, ROOT, reviews=reviews)
            self.assertEqual(card["contrasts"]["consent_without_reasking"]["pairs"]["both_success"], 1)
            self.assertEqual(card["contrasts"]["asked_which_order"]["pairs"]["both_failure"], 1)
            with (self.root / "runs/followup/controller.lock").open("rb") as active:
                followup.fcntl.flock(active, followup.fcntl.LOCK_EX)
                with self.assertRaisesRegex(ContractError, "controller is active"):
                    adapter.analyze(plan, self.root, ROOT)
            receipt_file = self.root / "runs/followup" / jobs[0]["run"] / "continuation.json"
            write_json(receipt_file, dict(read(receipt_file), status="error", provider_error="HTTP_503"))
            with self.assertRaisesRegex(ContractError, "native journal"):
                adapter.analyze(plan, self.root, ROOT)
            with self.assertRaisesRegex(ContractError, "native journal"):
                adapter.run(plan, self.root, ROOT, approval=approved)
        for job in jobs:
            session = original / job["run"]
            self.assertEqual(digest((session / "log.jsonl").read_bytes()), receipts[job["run"]]["journal_sha256"])
            self.assertFalse((session / "continuation.started").exists())

    def test_native_step_limit_and_provider_outage_have_distinct_recoverable_outcomes(self):
        from agent_memory_eval.experiments.forms_batch import write_json
        from agent_memory_eval.study.adapters.retail_revisit import consultation_row

        for cause in ("max_steps", "HTTP_520", "STUDY_LIMIT"):
            with self.subTest(cause=cause):
                job = dict(self.job("query"), run="r_" + cause, customer="c", source="s", mode="previous_only")
                rt = self.runtime([[call("get_order_details", order_id=job["target"])]], [])
                rt["seal_continuation"] = True
                rt["guard"] = ExpenseLedger(self.root / "unused-expense.jsonl", "1", 10, float("inf"))
                if cause == "max_steps":
                    rt["protocol"] = dict(self.protocol, native_steps=2)
                folder = self.root / job["run"]
                with Journal(folder, job["run"], contract_version="1.2") as journal:
                    first = first_decision(journal, job, self.task, self.prefix, rt, [self.item], [], keep=True)
                    sid = journal.scan["starts"][job["run"]]["session_id"]
                raw = (folder / "log.jsonl").read_bytes()
                first.update(journal_bytes=len(raw), journal_sha256=digest(raw))
                write_json(folder / "decision.json", first)
                write_json(folder / "continuation.started", dict(stage="continuation", run=job["run"]))
                failure = (ProviderError("STUDY_LIMIT", submitted=False) if cause == "STUDY_LIMIT" else
                           ProviderError("HTTP_520", {"error": {"code": 520}}, submitted=True))
                with (Journal(folder, job["run"], contract_version="1.2") as journal,
                      patch.object(rt["clients"]["worker"], "respond", side_effect=failure)):
                    outcome = continue_decision(journal, job, self.task, self.prefix, rt, first)
                self.assertEqual(recover_finished(folder, "continuation", job, True), outcome)
                raw = (folder / "log.jsonl").read_bytes()
                write_json(folder / "continuation.json", dict(outcome, journal_bytes=len(raw), journal_sha256=digest(raw)))
                prior = dict(session=sid, first_lookup=False, focused=True, decision_request=first["request"], included=[],
                             confirmed_usd="0", held_usd="0")
                row = consultation_row(folder, job, first, prior, {}, {}, native_failures=["max_steps", "too_many_errors"])
                self.assertEqual(row["reason"], cause)
                if cause == "STUDY_LIMIT":
                    self.assertEqual(row["status"], "budget_deferred")
                self.assertIs(row["consent_without_reasking"], False if cause == "max_steps" else None)
                self.assertIsNone(row["db_success"])

    def test_native_db_evaluator_ignores_absent_and_refused_consent(self):
        from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage
        from tau2.data_model.tasks import Task
        from tau2.domains.retail.environment import get_environment
        from tau2.evaluator.evaluator_env import EnvironmentEvaluator

        oid = self.case["target"]
        for answer, chosen, db in (
            ("Yes.", oid, 1),
            (None, oid, 1),
            ("Do not cancel.", oid, 1),
            ("Yes.", self.other, 0),
            ("Yes.", None, 0),
        ):
            native = get_environment()
            messages = [AssistantMessage.text("May I cancel " + oid + " because it is no longer needed?")]
            if answer is not None:
                messages.append(UserMessage.text(answer))
            if chosen:
                action = ToolCall(
                    id="c",
                    name="cancel_pending_order",
                    arguments={"order_id": chosen, "reason": "no longer needed"},
                )
                messages.extend([AssistantMessage.text(None, tool_calls=[action]), native.get_response(action)])
            score = EnvironmentEvaluator.calculate_reward(
                get_environment, Task.model_validate(self.task), messages
            )
            self.assertEqual(score.reward, db)
            turns = public_turns([m.model_dump(mode="json") for m in messages])
            annotation = {
                "status": "reviewed",
                "turns_sha256": digest(canonical(turns)),
                "clarifications": [],
                "proposals": [{"turn": 0, "order": oid, "reason": "no longer needed"}],
                "responses": [
                    {"turn": 1, "proposal": 0, "disposition": "confirmed" if answer == "Yes." else "refused"}
                ]
                if answer
                else [],
            }
            self.assertEqual(
                grade_turns(turns, oid, score.reward, annotation)["consent_qualified"],
                chosen == oid and answer == "Yes.",
            )

    def test_missing_source_does_not_block_controls_and_untouched_job_resumes(self):
        source = self.case["source"]
        jobs = {"source": [], "writer": [], "decision": [], "continuation": []}
        for form in self.protocol["forms"]:
            jobs["writer"].append({"stage": "writer", "task": source, "arm": form, "run": "r_" + form})
        src = {"stage": "source", "task": source, "run": "r_source", "arm": "no_memory"}
        jobs["source"].append(src)
        srcfolder = self.root / src["run"]
        srcfolder.mkdir()
        (srcfolder / "source.started").write_text("{}")
        for arm in ("preload", "query", "no_memory"):
            j = dict(self.job(arm), source=source, in_sample=False)
            jobs["decision"].append(j)
            jobs["continuation"].append(j)
        rt = self.runtime(["Please tell me the order number."], [])
        rt["guard"] = ExpenseLedger(self.root / "ledger", "1", 5, time.monotonic() + 60)
        study = {"jobs": jobs, "tasks": self.tasks, "public": self.prefixes}
        run = lambda: execute(study, rt, through="decision", continuation="sample", destination=self.root)
        self.assertEqual(run()["decision"], {"bank_missing": 2, "decision": 1})
        self.assertEqual(run()["decision"], {"bank_missing": 2, "decision": 1})
        self.assertEqual(len(rt["clients"]["worker"].wires), 1)

    def test_conditional_decisions_and_exposure_from_the_generating_request(self):
        oid, src = self.case["target"], self.case["source"]
        item = dict(self.item, body="Earlier order was " + oid)
        locate = call("get_order_details", order_id=oid)
        listing, get = call("memory_list"), call("memory_get", id=item["id"])
        examples = [
            ("mixed", "query", [[listing], [get, locate]], "checkpoint", True, {"title"}),
            ("body", "query", [[listing], [get], [locate]], "decision", True, {"title", "body"}),
            ("limit", "query", [[listing]] * 4, "private_read_limit", False, set()),
            ("empty", "empty_query", [[listing], "Which order?"], "decision", False, set()),
            ("error", "query", [], "error", None, set()),
        ]
        for label, interface, responses, status, focused, fields in examples:
            with self.subTest(label=label):
                rt = self.runtime(responses, [])
                rt["instructions"] = {"conditional": "If the order is unclear, you may consult notes."}
                rt["source_banks"] = {src: ([item], ["r_writer"])}
                rt["guard"] = ExpenseLedger(self.root / "ledger", "1", 4, time.monotonic() + 60)
                cfg, orch, _ = build_episode(self.protocol, self.connection, self.task, prefix=self.prefix)
                wire = first_wire(cfg, orch, self.prefix, policy_for(interface), rt["instructions"]["conditional"], [])
                real = interface == "query"
                job = dict(self.job("conditional_" + interface), native_arm=interface, instruction="conditional",
                           run="r_" + label, source=src, customer="test_customer", position=2, mode="previous_only",
                           history=oid, score_private_limit=True, in_sample=label == "mixed",
                           items_sha256=digest(canonical([item] if real else [])), writer_runs=["r_writer"] if real else [],
                           initial_sha256=digest(canonical(wire)))
                jobs = {"source": [], "writer": [], "decision": [job], "continuation": []}
                study = {"jobs": jobs, "tasks": self.tasks, "public": self.prefixes}
                client = rt["clients"]["worker"]
                with patch.object(client, "respond", wraps=client.respond) as send, patch("scripts.run_retail_two_visit.time.sleep"):
                    if label == "error":
                        send.side_effect = ProviderError("HTTP_503", submitted=True)
                    for _ in range(2):
                        counts = execute(study, rt, through="decision", continuation="sample", destination=self.root)
                        self.assertEqual(counts["decision"], {status: 1})
                    self.assertEqual(send.call_count, max(1, len(responses)))
                folder = self.root / job["run"]
                receipt = read(folder / "decision.json")
                result = score_session(folder, job, receipt)
                self.assertEqual(result["focused"], focused)
                self.assertEqual({e["field"] for e in result["included"]}, fields)
                self.assertEqual(any(e["target_identifier"] for e in result["included"]), label == "body")
                self.assertEqual(rt["clients"]["user_simulator"].wires, [])
                if label == "mixed":
                    self.assertTrue(result["mixed"])
                    self.assertFalse(result["strict_focused"])
                    self.assertTrue(result["first_lookup"])
                    resumed = self.runtime(["Shall I cancel " + oid + " because it is no longer needed?",
                                            [call("cancel_pending_order", order_id=oid, reason="no longer needed")],
                                            "Cancelled."], ["Yes, please cancel that order.", "###STOP###"])
                    resumed["instructions"] = rt["instructions"]
                    with Journal(folder, job["run"], contract_version="1.2") as journal:
                        self.assertEqual(continue_decision(journal, job, self.task, self.prefix, resumed, receipt)["db_reward"], 1)
                    self.assertEqual(score_session(folder, job, receipt), result)
                if label == "limit":
                    self.assertEqual(recover_finished(folder, "decision", job, False)["status"], status)


class LedgerTests(unittest.TestCase):
    def test_interrupted_decision_retains_its_verified_wire_reservation(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "r_interrupted"
            path.mkdir()
            (path / "decision.started").touch()
            endpoint = {"model": "synthetic", "pricing": {"prompt": "0.00001", "completion": "0.00002"}}
            wire = {"model": "synthetic", "messages": [], "max_tokens": 2}
            reservation = str(request_reservation_usd(wire, endpoint))
            ref = BlobStore(path).put(wire)
            req = {"actor_role": "worker", "request_id": "q1", "request_ref": ref}
            event = {"type": "model.request_started", "payload": req}
            (path / "log.jsonl").write_bytes(canonical({"events": [event]}) + b"\n")
            reg = {"jobs": {"source": [], "writer": [], "decision": [{"run": path.name}]},
                   "endpoints": {"worker": endpoint}, "freeze": {"implementation_commit": "mock"}}
            (root / "registration.json").write_bytes(canonical(reg))
            (root / "authorization.json").write_bytes(canonical({"max_usd": 1, "max_requests": 4}))
            (root / "summary.json").write_bytes(canonical({"confirmed_usd": "0", "held_usd": "0", "reserved_calls": 0}))
            first = audit_billing(root, allow_interrupted_decisions=True)
            self.assertEqual(first["interrupted_requests"], {"before_reservation": 1})
            line = {"seq": 1, "reserved": reservation, "wire_sha256": ref["sha256"], "model": "synthetic",
                    "stage": "decision", "run": path.name}
            (root / "expense.jsonl").write_bytes(canonical(line) + b"\n")
            (root / "summary.json").write_bytes(canonical({"confirmed_usd": "0", "held_usd": reservation, "reserved_calls": 1}))
            held = audit_billing(root, allow_interrupted_decisions=True)
            self.assertEqual(held["held_usd"], reservation)
            self.assertEqual(held["interrupted_requests"], {"reserved_without_journal_finish": 1})
            with self.assertRaises(ContractError):
                audit_billing(root)

    def test_continuation_requires_real_improvement_and_exposure(self):
        design = read(ROOT / CONDITIONAL_DRAFT)
        rows = []
        for customer in ("a", "b"):
            for scenario in design["allocation"]["modes"]:
                for arm in design["arms"]:
                    prev = scenario == "previous_only"
                    informed = prev and arm == "conditional_query"
                    rows.append(dict(customer=customer, source=customer, mode=scenario, arm=arm,
                        focused=not prev or informed, strict_focused=not prev or informed, prior_lookup=False,
                        status="decision", behavior="focused_target" if not prev or informed else "customer_reply",
                        first_lookup=informed, other_order_lookup=False, mutation=False, list_requested=informed,
                        get_requested=False, title_returned=informed, body_returned=False, mixed=False,
                        requests_started=1, prompt_tokens=1, completion_tokens=1, cached_tokens=0,
                        cache_write_tokens=0, submissions=1, confirmed_usd="0", held_usd="0",
                        included=[{"field": "title", "target_identifier": True}] if informed else []))
        positive = summarize_conditional(rows, design)
        self.assertTrue(positive["recommend_continuation"])
        self.assertEqual(positive["correct_after_target_identifier"], 2)
        for r in rows:
            if r["mode"] == "previous_only" and r["arm"].startswith("current_"):
                r["focused"] = True
        ambiguous = summarize_conditional(rows, design)
        self.assertEqual(ambiguous["primary"]["difference"], 1)
        self.assertFalse(ambiguous["recommend_continuation"])

    def test_four_way_bounds_and_equal_customer_weighting(self):
        terms = {"new": 1, "new_empty": -1, "old": -1, "old_empty": 1}
        cases = {
            ("a", "1"): dict(zip(terms, [1, 0, 0, 0])),
            ("a", "2"): dict(zip(terms, [1, None, 0, 0])),
            ("b", "3"): dict(zip(terms, [0, 0, 1, 0])),
            ("b", "4"): dict(zip(terms, [0, 0, 1, 0])),
        }
        result = customer_contrast(cases, terms, uncertainty={"seed": 12, "replicates": 100})
        self.assertEqual(result["complete_cases"], 3)
        self.assertEqual(result["difference"], 0)
        self.assertEqual(result["bounds"], [-.25, 0])
        self.assertEqual(result["interval"], [-1, 1])

    def test_missing_binary_outcomes_keep_registered_bounds_and_customer_weights(self):
        rows = []
        for u, task, a, b in (
            ("u", "t1", "focused_target", "other_order_only"),
            ("u", "t2", "other_order_only", "other_order_only"),
            ("v", "t3", "unobserved", "focused_target"),
        ):
            for arm, behavior in (("preload", a), ("empty_preload", b)):
                rows.append(
                    {
                        "customer": u,
                        "task": task,
                        "arm": arm,
                        "mode": "previous_only",
                        "position": 2,
                        "behavior": behavior,
                    }
                )
        by_user, stats = differences(rows, "preload", "empty_preload", "previous_only")
        self.assertEqual(by_user, {"u": 0.5})
        self.assertEqual(stats["bounds_over_all_registered"], [-0.25, 0.25])
        self.assertEqual(stats["complete_pairs"], 2)

    def test_unpriced_submission_remains_held_and_cannot_exceed_cap_on_restart(self):
        model = {"id": "x", "pricing": {"prompt": "0.000010", "completion": "0.000010"}}
        wire = {"messages": [{"role": "user", "content": "A"}], "max_tokens": 10}
        with TemporaryDirectory() as folder:
            path = Path(folder) / "expense.jsonl"
            guard = ExpenseLedger(path, "1", 3, time.monotonic() + 30)
            guard.before(wire, model)
            hold = guard.held
            guard.after({})
            self.assertTrue(guard.unknown_cost)
            reopened = ExpenseLedger(path, hold, 3, time.monotonic() + 30)
            with self.assertRaises(ProviderError):
                reopened.before(wire, model)
            self.assertEqual(reopened.held, hold)
            self.assertEqual(reopened.spent, Decimal(0))
