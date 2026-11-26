import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jsonschema import Draft202012Validator

from agent_memory_eval.analysis import task_room as room
from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.contracts.validation import SCHEMA_ROOT


def graded(task, run, success, **extra):
    return dict(task=task, run=run, status="graded", success=success, **extra)


def make_domain(root, customers):
    """Tasks keyed to a registered customer, or to None for a persona outside the database."""
    users = {c: dict(name=f"Person {c}", phone_number=f"555-01{i:02d}")
             for i, c in enumerate(sorted({c for c in customers.values() if c}))}
    (root / "tasks").mkdir(parents=True)
    (root / "db.json").write_text(json.dumps({"users": {"data": users}}))
    for task, customer in customers.items():
        actions = [dict(name="log_verification", arguments=dict(user_id=customer))] if customer else []
        case = dict(user_scenario=dict(instructions="A customer asks for help."), evaluation_criteria=dict(actions=actions))
        (root / "tasks" / f"{task}.json").write_text(json.dumps(case))
    return root


class TaskRoomTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.domain = make_domain(self.root / "domain", dict(t1="c1", t2="c1", t3=None, t4="c2"))
        # List-shaped first pass and follow-ups.
        self.listed = dict(
            sources=[graded("t1", "r1", False)],
            trials=[graded("t1", "x1", True, arm="fact__on_demand", read_calls=0),
                    graded("t1", "x2", False, arm="fact__preload", read_calls=0)],
        )
        # Task-keyed first pass reusing r1, and two registered arms.
        self.keyed = dict(
            sources={"t1": graded("t1", "r1", False), "t2": graded("t2", "r2", False)},
            trials={"existing": [graded("t1", "y1", False, arm="fact__on_demand", read_calls=0),
                                 graded("t2", "y3", False, arm="fact__preload")],
                    "candidate": [graded("t1", "y2", True, arm="fact__on_demand", read_calls=0),
                                  graded("t2", "y4", False, arm="fact__preload")]},
        )
        # Reader rows that never started.
        self.readers = dict(
            sources={"t3": dict(task="t3", run="r3", status="not_started", success=None)},
            readers=[dict(task="t3", form="free", repeat=1, run=None, status="not_started", success=None)],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, **changes):
        studies = []
        for name, result in (("listed", self.listed), ("keyed", self.keyed), ("readers", self.readers)):
            path = self.root / f"{name}.json"
            path.write_text(json.dumps(result))
            studies.append(dict(id=name, result=dict(path=path.name, sha256=digest(path.read_bytes()))))
        plan = dict(
            format=room.PLAN,
            studies=studies,
            comparisons=[dict(id="arms", reference=dict(study="keyed", condition="existing"),
                              candidate=dict(study="keyed", condition="candidate"))],
            sizing=dict(alpha=0.05, power=0.8, scenarios=[[0.5, 0.75]]),
        )
        plan.update(changes)
        return plan

    def test_result_shapes_reused_runs_rooms_and_units(self):
        result = room.task_room(self.root, self.plan(), self.domain)
        summary = result["summary"]
        self.assertEqual((summary["rows"], summary["distinct_rows"], summary["reused_runs"]), (11, 10, 1))
        rooms = {t["task"]: t["room"] for t in result["tasks"]}
        self.assertEqual(rooms, dict(t1="mixed", t2="floor", t3="unobserved", t4="not_run"))
        units = {t["task"]: t["unit"] for t in result["tasks"]}
        self.assertEqual(units, dict(t1="c1", t2="c1", t3="task:t3", t4="c2"))
        t1 = next(t for t in result["tasks"] if t["task"] == "t1")
        self.assertEqual(t1["source"], dict(success=0, failure=1, ungraded=0))
        self.assertEqual(t1["solve"], dict(success=2, failure=2, ungraded=0))
        self.assertEqual(summary["mixed_units"], ["c1"])
        self.assertEqual((summary["identified_customers"], summary["unresolved_tasks"]), (2, 1))
        self.assertEqual(summary["units_only_in_tasks_not_run"], 1)
        error = next(Draft202012Validator(
            json.loads((SCHEMA_ROOT / "task-room-v1.schema.json").read_text())).iter_errors(result), None)
        self.assertIsNone(error, error and error.message)

    def test_comparison_separates_unopened_on_demand_pairs_in_mixed_tasks(self):
        (comparison,) = room.task_room(self.root, self.plan(), self.domain)["comparisons"]
        self.assertEqual(comparison["by_task"]["t1"]["candidate_only"], 1)
        self.assertEqual(comparison["by_task"]["t2"]["both_failure"], 1)
        self.assertEqual(comparison["unopened_on_demand_in_mixed_tasks"]["candidate_only"], 1)
        self.assertEqual(sum(comparison["unopened_on_demand_in_mixed_tasks"].values()), 1)
        result = room.task_room(self.root, self.plan(), self.domain)
        self.assertEqual((result["noise"]["pairs"], result["noise"]["discordant"]), (1, 1))

    def test_inconsistent_rows_and_pairings_are_rejected(self):
        with self.assertRaises(ContractError):
            room.result_rows("bad", dict(sources=[dict(task="t1", run="r", status="graded", success=None)], trials=[]))
        self.keyed["sources"]["t1"] = graded("t1", "r1", True)  # The reused first pass now disagrees.
        with self.assertRaises(ContractError):
            room.task_room(self.root, self.plan(), self.domain)
        self.keyed["sources"]["t1"] = graded("t1", "r1", False)
        self.keyed["trials"]["candidate"].pop()
        with self.assertRaises(ContractError):
            room.task_room(self.root, self.plan(), self.domain)

    def test_changed_input_is_rejected(self):
        plan = self.plan()
        plan["studies"][0]["result"]["sha256"] = "0" * 64
        with self.assertRaises(ContractError):
            room.task_room(self.root, plan, self.domain)

    def test_exact_sign_test_sizing(self):
        # Hand check: at a 0.9 share, 9 pairs reject at 0, 1, 8 or 9 wins, so the power is P(G<=1) + P(G>=8).
        self.assertAlmostEqual(room.sign_test_power(9, 0.9), 0.774841060, places=9)
        self.assertLess(room.sign_test_power(11, 0.9), 0.8)
        self.assertEqual(room.discordant_needed(0.9), 12)
        self.assertGreater(room.discordant_needed(0.6), room.discordant_needed(0.65))
        self.assertGreater(room.discordant_needed(0.65), room.discordant_needed(0.75))
        self.assertIsNone(room.discordant_needed(0.51, limit=50))
        case = room.scenario(0.5, 0.75, 0.05, 0.8)
        self.assertEqual((case["discordance"], case["candidate_share"]), (0.5, 0.75))
        self.assertEqual(case["pairs"], 2 * case["discordant_pairs"])


if __name__ == "__main__":
    unittest.main()
