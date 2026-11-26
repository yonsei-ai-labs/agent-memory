from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from agent_memory_eval.adapters.banking_source_screen import document_opportunity, partition, summarize_task
from agent_memory_eval.adapters.tau2_task_start import matched_opening
from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.runner.task_start import TaskStartMismatch
from scripts import run_banking_memory as bank


class SourceScreenTests(TestCase):
    def test_archived_customer_tool_opening_is_missing_only_under_its_exact_registration(self):
        root = bank.ROOT / "runs/tau-banking-source-screen-01"
        if not (root / "registration.json").is_file():
            self.skipTest("Requires the recorded customer-tool opening")
        reg = bank.matched.read(root / "registration.json")
        case = next(c for c in reg["preflight"]["cases"] if c["task"] == "task_081__source-1")
        folder = root / ("task-" + case["task"]) / "source"
        if not (folder / "completion.json").is_file():
            self.skipTest("Source not sealed")
        _, ctl, _ = bank.matched.sealed_rows(folder)
        args = [folder, case, ctl["profile"], ctl["design"], reg["implementation"], ctl["model_catalog_entries"]]
        self.assertEqual(bank.verified_customer_tool_stop(*args)["reason"], "customer_tool_before_first_worker_input")
        for index in (2, 3, 4, 5):
            changed = args.copy()
            changed[index] = {}
            with self.subTest(field=index), self.assertRaises(ContractError):
                bank.verified_customer_tool_stop(*changed)

    def test_confirmed_customer_alias_is_quarantined_even_without_a_verification_target(self):
        past = [dict(task="a", unit="A", identified=True, room="floor"),
                dict(task="b", unit="B12345", identified=True, room="not_run"),
                dict(task="c", unit="task:c", identified=False, room="not_run")]
        def task(text):
            return dict(user_scenario=dict(instructions=text), evaluation_criteria=dict(reward_basis=["DB"], actions=[]))
        tasks = dict(a=task("Prior user"), b=task("Jamie Example"), c=task("You're Jamie Example."))
        db = {"B12345": dict(name="Jamie Example")}
        result = partition(past, tasks, db)
        self.assertEqual([result[k]["group"] for k in "abc"], ["previous", "confirmation", "possible_overlap"])
        tasks["a"] = tasks["c"]
        with self.assertRaises(ContractError):
            partition(past, tasks, db)

    def test_document_must_be_in_a_linked_public_tool_reply_and_absent_from_first_input(self):
        text = "1. Policy\n   ID: doc_policy_1\n   Score: 1.0\n"
        call = dict(role="assistant", tool_calls=[dict(id="g", function=dict(name="grep", arguments='{"pattern":"fees"}'))])
        reply = dict(role="tool", tool_call_id="g", content=text)
        def observed(messages, first="doc_policy_11"):
            return document_opportunity({"doc_policy_1"}, dict(messages=[dict(content=first)]), messages)["observed"]
        self.assertTrue(observed([call, reply]))
        self.assertFalse(observed([call, reply], first="doc_policy_1"))
        self.assertFalse(observed([dict(role="user", content=text)]))
        self.assertIsNone(observed([call]))
        self.assertIsNone(observed([call, dict(reply, content="Unrecognized renderer")]))

    def test_primary_candidate_requires_all_draws_and_positive_public_evidence(self):
        rows = [dict(draw=i, success=bool(i % 2), opportunity=dict(observed=(i == 2))) for i in (1, 2, 3)]
        self.assertTrue(summarize_task(rows, 3)["candidate"])
        rows[1]["opportunity"]["observed"] = None
        self.assertFalse(summarize_task(rows, 3)["candidate"])
        rows[0].update(success=None, opportunity=None)
        self.assertEqual(summarize_task(rows, 3)["pattern"], "incomplete")
        with self.assertRaises(ContractError):
            summarize_task([rows[0], rows[0], rows[2]], 3)

    def test_native_data_reset_reaches_same_public_start_and_wrong_db_stops_before_transport(self):
        if not (bank.ROOT / "data/tau2-bench").is_dir():
            self.skipTest("Requires pinned native checkout")
        _, cfg, conn, _, _, _ = bank.prepare(bank.ROOT)
        modules = bank.matched.modules_for(bank.ROOT)
        from tau2.data_model.message import AssistantMessage
        from tau2.domains.banking_knowledge.environment import get_tasks

        task = next(t for t in get_tasks() if t.id == "task_078")
        case, raw = bank.native_case(cfg, conn, modules, task, "DB")
        self.assertNotEqual(case["initial_db"], case["initialized_databases"]["agent"])
        conf = bank.solve_connection(conn, bank.matched.design_for(cfg, case, "source"), bank.matched.jobs_for(case, "source")[0],
                                     cfg["episode_cap_usd"])
        starts = []
        for mode in ("fresh", "replay", "wrong"):
            orch, native = bank.build_baseline_orchestrator(conf, raw)
            control = bank.native_control(orch, native, conf, cfg["limits"], cfg["http_timeout_seconds"], None)
            self.assertEqual(orch.environment.get_db_hash(), case["initial_db"])
            if mode == "wrong":
                control = deepcopy(control)
                control["initialized_databases"]["agent"] = "wrong"
            with (patch.object(modules[2], "completion", side_effect=AssertionError("Unexpected network")),
                  patch.object(modules[1], "generate", return_value=AssistantMessage(role="assistant", content="Hello.")) as user):
                opening = matched_opening(orch, modules, control, bank.role_settings(control)["user_simulator"],
                                          lambda *a: None, starts.append, reference=starts[0] if starts else None)
                if mode == "wrong":
                    with self.assertRaises(TaskStartMismatch), opening:
                        orch.initialize()
                        orch.step()
                else:
                    with opening:
                        orch.initialize()
                        orch.step()
                self.assertEqual(user.call_count, int(mode == "fresh"))
        self.assertEqual(starts[0].record(), starts[1].record())
