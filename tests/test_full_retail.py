from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from agent_memory_eval.adapters.retail_grader import (
    checked_answer,
    native_grade_transport,
)
from agent_memory_eval.contracts.core import ContractError, RunContext, new_id
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.journal import Journal
from scripts.run_full_retail import prepare, preflight


@unittest.skipUnless(
    (ROOT / "data/tau2-bench/data/tau2/domains/retail/tasks.json").is_file(),
    "Requires pinned benchmark checkout",
)
class FullRetailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg, cls.base, _, cls.rows, cls.tasks = prepare(ROOT)
        cls.spec = dict(cls.cfg["judge"], timeout_seconds=150)
        cls.model = dict(selected_endpoint=dict(provider_name="OpenAI"))

    def test_full_split_and_source_writer_pairing(self):
        schedule = preflight(self.cfg, self.base, self.rows)
        self.assertEqual(sum(r["prior_task"] for r in self.rows), 6)
        self.assertEqual(sum(r["prior_customer"] for r in self.rows), 13)
        self.assertEqual(sum(r["assertions"] > 0 for r in self.rows), 11)
        self.assertEqual(len({r["customer"] for r in self.rows}), 31)
        self.assertEqual(
            [
                sum(len(row["stages"][s]) for row in schedule["rows"])
                for s in self.cfg["stages"]
            ],
            [40, 160, 240],
        )

    def test_twelve_additional_grading_paths_with_positive_and_negative_judge(self):
        from tau2.data_model.semantic_communication import CommunicateScoring
        from tau2.data_model.tasks import Task
        from tau2.data_model.message import AssistantMessage
        from tau2.data_model.simulation import SimulationRun
        from tau2.evaluator.evaluator import evaluate_simulation, EvaluationType
        from tau2.runner import simulation as runner

        processed = 0
        for row in self.rows:
            if not (row["assertions"] or row["communicate_items"]):
                continue
            processed += 1
            task = Task.model_validate(self.tasks[row["task"]])
            assertions = task.evaluation_criteria.nl_assertions or []
            sim = SimulationRun(
                id="fixture",
                task_id=task.id,
                start_time="2026-01-01",
                end_time="2026-01-01",
                duration=1,
                termination_reason="agent_stop",
                messages=[AssistantMessage(role="assistant", content="Hello.")],
            )
            for decision in (False, True):

                def fake(wire):
                    self.assertEqual(wire["model"], self.spec["model"])
                    self.assertEqual(wire["provider"], self.spec["route"])
                    for item in assertions:
                        self.assertIn(item, wire["messages"][1]["content"])
                    return dict(
                        id="gen-fixture",
                        provider="OpenAI",
                        model=self.spec["canonical_slug"],
                        usage=dict(cost=0),
                        choices=[
                            dict(
                                finish_reason="stop",
                                message=dict(
                                    role="assistant",
                                    content=json.dumps(
                                        dict(
                                            results=[
                                                dict(
                                                    expectedOutcome=a,
                                                    metExpectation=decision,
                                                    reasoning="Fixture",
                                                )
                                                for a in assertions
                                            ]
                                        )
                                    ),
                                ),
                            )
                        ],
                    )

                with (
                    tempfile.TemporaryDirectory() as tmp,
                    Journal(
                        Path(tmp) / "j", new_id("exp"), contract_version="1.2"
                    ) as journal,
                ):
                    ctx = RunContext(
                        journal.experiment_id,
                        new_id("r"),
                        new_id("s"),
                        "control",
                        "probe",
                        "solve",
                        "observed",
                    )
                    journal.append(
                        [
                            ctx.event(
                                "run.started",
                                dict(
                                    bank_id=None,
                                    start_snapshot_id=None,
                                    fresh_session=True,
                                    source_run_ids=[],
                                    task_ref=journal.blobs.put(
                                        dict(role="system", content="test")
                                    ),
                                ),
                            )
                        ]
                    )
                    calls = {}
                    with (
                        patch(
                            "socket.socket.connect",
                            side_effect=AssertionError("offline"),
                        ),
                        native_grade_transport(
                            journal,
                            ctx,
                            task,
                            (self.spec, self.model),
                            SimpleNamespace(respond=fake),
                            calls,
                        ),
                    ):
                        reward = runner.evaluate_simulation(
                            simulation=sim,
                            task=task,
                            evaluation_type=EvaluationType.ALL,
                            domain="retail",
                            solo_mode=False,
                            strict_replay=True,
                            communicate_scoring=CommunicateScoring(scorer="substring"),
                        )
                    db = evaluate_simulation(
                        simulation=sim,
                        task=task,
                        evaluation_type=EvaluationType.ENV,
                        domain="retail",
                        solo_mode=False,
                        strict_replay=True,
                        communicate_scoring=CommunicateScoring(scorer="substring"),
                    )
                    self.assertEqual(
                        reward.reward, db.reward * (decision if assertions else 1)
                    )
                    self.assertIsNone(reward.communicate_checks)
                    self.assertEqual(
                        [a.met for a in reward.nl_assertions or []],
                        [decision] * len(assertions),
                    )
                    starts = [
                        e
                        for e in journal.events.values()
                        if e["type"] == "model.request_started"
                    ]
                    self.assertEqual(len(starts), int(bool(assertions)))
                    if starts:
                        self.assertEqual(starts[0]["payload"]["actor_role"], "grader")
                    self.assertIn("before_grade_ref", calls)
        self.assertEqual(processed, 12)

    def test_incomplete_or_substituted_judgment_cannot_earn_credit(self):
        for values in (
            [],
            [dict(expectedOutcome="another", reasoning="x", metExpectation=True)],
        ):
            raw = dict(
                model=self.spec["model"],
                provider="OpenAI",
                choices=[
                    dict(
                        finish_reason="stop",
                        message=dict(
                            role="assistant", content=json.dumps(dict(results=values))
                        ),
                    )
                ],
            )
            with self.assertRaises(ContractError):
                checked_answer(raw, ["required"], self.spec, self.model)
