"""A native empty initialization object must preserve the first public decision."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_memory_eval.adapters.tau2_task_start import (
    empty_initial_state,
    matched_opening,
)
from agent_memory_eval.contracts.core import canonical, digest
from scripts import analyze_banking_replacement as analysis
from scripts import run_banking_replacement as pilot


class BankingReplacementTests(unittest.TestCase):
    def test_only_semantically_empty_initial_states_are_supported(self):
        state = SimpleNamespace(initialization_data=None, initialization_actions=[], message_history=None)
        self.assertTrue(empty_initial_state(None))
        self.assertTrue(empty_initial_state(state))
        state.initialization_data = SimpleNamespace(agent_data=None, user_data=None)
        self.assertTrue(empty_initial_state(state))
        for field in ("agent_data", "user_data"):
            with self.subTest(field=field), patch.object(state.initialization_data, field, {"record": 1}):
                self.assertFalse(empty_initial_state(state))
        for field in ("message_history", "initialization_actions"):
            with self.subTest(field=field), patch.object(state, field, ["earlier event"]):
                self.assertFalse(empty_initial_state(state))

    def test_empty_native_initialization_replays_the_same_first_observation_and_db(self):
        workspace = pilot.ROOT
        if not (workspace / "data/tau2-bench").is_dir():
            self.skipTest("Requires the pinned native checkout")
        _, cfg, conn, case, task, _ = pilot.prepare(workspace)
        bank = pilot.cycle.bank
        modules = bank.matched.modules_for(workspace)
        from tau2.data_model.message import AssistantMessage

        conf = bank.solve_connection(
            conn,
            bank.matched.design_for(cfg, case, "source"),
            bank.matched.jobs_for(case, "source")[0],
            cfg["episode_cap_usd"],
        )
        starts = []
        for reference in (False, True):
            orch, native = bank.build_baseline_orchestrator(conf, task)
            control = bank.native_control(
                orch, native, conf, cfg["limits"], cfg["http_timeout_seconds"], None
            )

            def check(role, wire, messages):
                self.assertEqual(role, "user_simulator")
                self.assertEqual(digest(canonical(wire)), case["customer_opening_sha256"])

            with (
                patch.object(modules[2], "completion", side_effect=AssertionError("Unexpected transport")),
                patch.object(
                    modules[1],
                    "generate",
                    return_value=AssistantMessage(role="assistant", content="Please check my statement."),
                ) as customer,
                matched_opening(
                    orch,
                    modules,
                    control,
                    bank.role_settings(control)["user_simulator"],
                    check,
                    starts.append,
                    reference=starts[0] if reference else None,
                ),
            ):
                orch.initialize()
                orch.step()
            self.assertEqual(customer.call_count, 0 if reference else 1)
            self.assertEqual(orch.environment.get_db_hash(), case["initial_db"])
        self.assertEqual(starts[0].record(), starts[1].record())

    def test_order_receipt_and_db_replay_distinguish_accepted_but_wrong_delivery(self):
        workspace = pilot.ROOT
        if not (workspace / "data/tau2-bench").is_dir():
            self.skipTest("Requires the pinned native checkout")
        _, _, _, _, data, _ = pilot.prepare(workspace)
        from tau2.data_model.message import AssistantMessage, ToolCall
        from tau2.data_model.tasks import Task
        from tau2.domains.banking_knowledge.environment import get_environment

        task = Task.model_validate(data)
        actions = task.evaluation_criteria.actions
        order = analysis.expected_order(task)
        for expedited in (False, True):
            with self.subTest(expedited=expedited):
                env = get_environment(retrieval_variant="grep_only", task=task)
                history = []
                for i, action in enumerate(actions):
                    args = dict(action.arguments)
                    if action is actions[-1]:
                        args["arguments"] = canonical(dict(order, expedited_shipping=expedited)).decode()
                    call = ToolCall(id=str(i), name=action.name, arguments=args, requestor="assistant")
                    history.extend(
                        [AssistantMessage(role="assistant", tool_calls=[call]), env.get_response(call)]
                    )
                native = {
                    "messages": [m.model_dump(mode="json") for m in history],
                    "reward_info": {"db_check": {"db_match": expedited}},
                }
                wire = {
                    "messages": [
                        {"role": "system", "content": "Policy"},
                        {"role": "user", "content": "Please expedite."},
                    ]
                }
                wire_ref = {"sha256": digest(canonical(wire))}
                req = {"actor_role": "worker", "request_id": "r", "request_ref": wire_ref}
                reply = {
                    "message": {
                        "tool_calls": [
                            {
                                "id": call.id,
                                "function": {
                                    "name": call.name,
                                    "arguments": canonical(call.arguments).decode(),
                                },
                            }
                        ]
                    }
                }
                log = SimpleNamespace(
                    requests=[req],
                    finishes={"r": {"status": "response"}},
                    of_type=Mock(
                        return_value=[
                            {"payload": {"official_success": expedited, "official_artifact_ref": "report"}}
                        ]
                    ),
                    blobs=SimpleNamespace(get=Mock(side_effect=[native, wire])),
                    reply=Mock(return_value=reply),
                    visible=lambda *_: [],
                )
                actual = analysis.audited_db(log, task, "GamerZone LA", "")
                self.assertIs(actual["official_db_success"], expedited)
                self.assertEqual(actual["new_or_changed_orders"], 1)
                self.assertTrue(actual["target_card_closed"])
                self.assertEqual(actual["other_cards_closed"], 0)
                self.assertIs(actual["order_fields"][0]["expedited_shipping"], expedited)
                (trace,) = actual["evidence_at_order"]
                self.assertIs(trace["requested_fields_match"]["expedited_shipping"], expedited)
                self.assertIs(trace["literal_support"]["expedited"]["in_order_arguments"], expedited)
                self.assertTrue(trace["native_receipt"]["has_order_id"])
                self.assertFalse(trace["native_receipt"]["error"])
                self.assertEqual(
                    actual["source_and_stored_note_literal_support"]["second_merchant"],
                    {"source_public": True, "in_stored_note": False},
                )
