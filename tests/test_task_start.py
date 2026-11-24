from copy import deepcopy
import unittest
from unittest.mock import Mock

from jsonschema import Draft202012Validator

from agent_memory_eval.adapters.bfcl_task_start import open_simple_python
from agent_memory_eval.analysis.efficiency import request_effort_by_start
from agent_memory_eval.contracts.validation import ROOT, read_json
from agent_memory_eval.runner.task_start import TaskKey, TaskStart, TaskStartMismatch, open_task_start


class TaskStartTests(unittest.TestCase):
    def test_non_dialogue_observation_survives_independent_sessions(self):
        key = TaskKey("grid", "r1", "10")
        controls = dict(seed=9, reset={"grid": [1, 2]}, grader="private reference")
        start = open_task_start(key, controls, lambda _: {"board": [[0, 1]], "legal_moves": ["left"]})
        record = start.record()
        schema = read_json((ROOT / "schemas/task-start-v1.schema.json").read_bytes())
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid(record))
        self.assertFalse(validator.is_valid(dict(record, grade="private")))
        with self.assertRaises(TaskStartMismatch):
            TaskStart.from_record(dict(record, grade="private"))
        self.assertNotIn("private reference", str(record))
        self.assertEqual(TaskStart.from_record(record), start)
        record["public_input"]["board"].clear()
        for _ in range(2):
            self.assertEqual(open_task_start(key, controls, lambda public: public, reference=start), start)
        with self.assertRaises(TaskStartMismatch):
            open_task_start(key, controls, lambda _: record["public_input"], reference=start)

    def test_mismatched_task_or_reset_is_rejected_before_exposing_the_reference(self):
        key = TaskKey("one", "r1", "1")
        controls = dict(seed=0)
        start = open_task_start(key, controls, lambda _: {"instruction": "Find x"})
        prepare = Mock()
        for task, control in [(TaskKey("other", "r1", "1"), controls), (TaskKey("one", "r2", "1"), controls),
                              (TaskKey("one", "r1", "2"), controls), (key, dict(seed=1))]:
            with self.subTest(task=task, control=control), self.assertRaises(TaskStartMismatch):
                open_task_start(task, control, prepare, reference=start)
        prepare.assert_not_called()

    def test_bfcl_static_task_is_reopened_from_native_public_fields(self):
        row = dict(id="simple_python_0", question=[[dict(role="user", content="Select the function")]],
                   function=[dict(name="example", description="An example.", parameters=dict(type="dict"))])
        controls = dict(worker="scripted")
        start = open_simple_python(row, controls)
        self.assertEqual(start.record()["public_input"], dict(messages=row["question"][0], functions=row["function"]))
        self.assertEqual(open_simple_python(deepcopy(row), controls, reference=start), start)
        for changed in [dict(row, ground_truth="private"),
                        dict(row, function=[dict(row["function"][0], name="different")]),
                        dict(row, function=[dict(row["function"][0], answer_key="private")])]:
            with self.assertRaises(TaskStartMismatch):
                open_simple_python(changed, controls, reference=start)

    def test_shared_setup_cost_is_charged_once_and_unknown_solver_usage_stays_missing(self):
        def sent(seq, request, role):
            return dict(type="model.request_started", event_seq=seq, payload=dict(request_id=request, actor_role=role))

        def finished(request, submitted, tokens, usd):
            return dict(type="model.request_finished", payload=dict(request_id=request,
                delivery_status="submitted" if submitted else "not_submitted", usage=dict(total_tokens=tokens),
                cost_status="observed" if usd is not None else "unknown", cost_usd=usd))

        events = [sent(1, "opening", "environment"), finished("opening", True, 30, 0.06),
                  sent(5, "worker", "worker"), finished("worker", True, None, None),
                  sent(8, "blocked", "worker"), finished("blocked", False, None, None),
                  sent(10, "grade", "grader"), finished("grade", True, 500, 1)]
        parts = request_effort_by_start(events, 4, {"worker", "environment"})
        self.assertEqual(parts["setup"], dict(http_calls=1, tokens=30, usd="0.06"))
        self.assertEqual(parts["solve"], dict(http_calls=1, tokens=None, usd=None))
