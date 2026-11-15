"""Mechanical public review metrics, without any provider or benchmark execution."""

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("baseline_review", ROOT / "scripts/review_tau2_baseline_screening.py")
REVIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REVIEW)


class BaselineReviewTests(unittest.TestCase):
    def test_write_annotations_are_read_from_the_native_decorator(self):
        source = '''
class Tools:
    @is_tool(ToolType.WRITE)
    def update(self): pass
    @is_tool(ToolType.READ)
    def read(self): pass
    def unrelated_write_name(self): pass
'''
        self.assertEqual(REVIEW.write_tool_names(source), ["update"])

    def test_call_repeats_normalize_arguments_without_claiming_policy_harm(self):
        report = dict(messages=[
            dict(role="assistant", tool_calls=[dict(id="a", name="lookup", arguments=dict(x=1, y=2))]),
            dict(role="assistant", tool_calls=[dict(id="b", name="lookup", arguments=dict(y=2, x=1)),
                dict(id="c", name="update", arguments=dict(x=1)), dict(id="d", name="transfer_to_human_agents", arguments={})]),
            dict(role="tool", error=True, content="error"),
        ])
        result = REVIEW.public_metrics(report, ["update"])
        self.assertEqual(result["business_tool_calls"], 4)
        self.assertEqual(result["identical_call_repeats"], 1)
        self.assertEqual(result["write_calls"], 1)
        self.assertEqual(result["transfer_calls"], 1)
        self.assertEqual(result["tool_error_message_indices"], [2])

    def test_public_projection_excludes_provider_raw_and_reasoning_fields(self):
        message = dict(role="assistant", content="public", raw_data=dict(reasoning="private"), reasoning_content="private",
            tool_calls=[dict(id="a", name="update", arguments={}, requestor="assistant", private="private")])
        selected = REVIEW.public_message(message)
        self.assertEqual(selected, dict(role="assistant", content="public", tool_calls=[dict(id="a", name="update", arguments={})]))

    def test_partial_trajectory_uses_the_same_descriptive_metrics(self):
        self.assertEqual(REVIEW.public_metrics(dict(partial_trajectory=[]), ["update"])["business_tool_calls"], 0)


if __name__ == "__main__":
    unittest.main()
