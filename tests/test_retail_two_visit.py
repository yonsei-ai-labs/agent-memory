"""Scripted transports with native retail tasks, database, evaluator and durable journals."""

import importlib.util
import time
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.models.openrouter import ProviderError
from agent_memory_eval.recording.journal import Journal
from scripts.analyze_retail_two_visit import (
    decision,
    differences,
    grade_turns,
    public_turns,
)
from scripts.prepare_retail_order_pairs import current_cases
from scripts.retail_two_visit_episode import (
    continue_decision,
    first_decision,
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


class LedgerTests(unittest.TestCase):
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
