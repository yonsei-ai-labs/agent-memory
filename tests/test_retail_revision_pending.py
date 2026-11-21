import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError
from scripts import complete_retail_transfer_revision as pending
from scripts.run_retail_related_transfer import design_for


class PendingRetailRevisionTests(unittest.TestCase):
    def test_filter_preserves_recorded_indices_and_rejects_any_existing_attempt(self):
        jobs = [
            {"index": i, "phase": "solve", "repetition": 1, "task_id": "37", "arm": arm}
            for i, arm in enumerate(("no_memory", "empty_preload"), 1)
        ]
        slots = [
            dict(jobs[0], directory="jobs/0001", run_id="old-one", status="error", result={}),
            dict(
                jobs[1], directory="jobs/0002", run_id="old-two", status="not_started", reason="unknown_cost"
            ),
        ]
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "jobs/0001").mkdir(parents=True)
            selected = pending.untouched_jobs(jobs, slots, folder)
            self.assertEqual(
                selected, [{"job": jobs[1], "original_run": "old-two", "reason": "unknown_cost"}]
            )
            (folder / "jobs/0002").mkdir()
            with self.assertRaises(ContractError):
                pending.untouched_jobs(jobs, slots, folder)
        row = {"task": "37", "split": "train", "pairs": {}, "unstarted_jobs": {"solve1": [jobs[1]]}}
        with (
            patch(
                "scripts.run_retail_related_transfer.retail.full_design", return_value=({"source": {}}, None)
            ),
            patch("scripts.run_retail_related_transfer.schedule", return_value=jobs),
        ):
            design, actual = design_for({"donors": {}}, None, row, "solve1")
            self.assertEqual(actual, [jobs[1]])
            self.assertEqual(design["allocation"]["jobs"], actual)
            row["unstarted_jobs"]["solve1"][0] = dict(jobs[1], arm="replacement")
            with self.assertRaises(ContractError):
                design_for({"donors": {}}, None, row, "solve1")
