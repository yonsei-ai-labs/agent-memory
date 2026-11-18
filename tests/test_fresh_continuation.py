import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.experiments.forms_batch import BatchGuard, run_jobs
from agent_memory_eval.experiments.fresh_continuation import verify_continuation


class FreshContinuationTests(unittest.TestCase):
    def test_interruption_marks_the_unstarted_suffix_at_original_indices(self):
        rows = [dict(index=i, task_id="19", repetition=1, arm="no_memory", phase="solve") for i in (79, 80)]
        guard = BatchGuard(max_calls=5, max_seconds=30, max_usd=1)

        def interrupted(row, writer):
            raise RuntimeError("local interruption")

        with self.assertRaises(RuntimeError):
            run_jobs(rows, interrupted, guard, lambda ledger: None)
        self.assertEqual([r["status"] for r in rows], ["interrupted", "not_started"])
        self.assertEqual(rows[1]["reason"], "controller_error")

    def test_resealing_cannot_replace_an_inherited_result_or_an_original_assignment(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run = workspace / "runs/follow"
            run.mkdir(parents=True)
            amendment = dict(destination="follow", first_untouched=2, received_unpriced_parent_jobs=[1])
            ctl = dict(source_outcomes={}, model_catalog_entries={}, implementation={"commit": "old"})
            cfg = {"comparison_guards": {"max_usd": 1}}
            old = [dict(index=1, run_id="original", status="error", result={"grade": None}),
                   dict(index=2, run_id="untouched", status="not_started", reason="unknown_cost")]
            control = dict(ctl, design={}, profile={"guards": cfg["comparison_guards"]},
                           continuation=dict(amendment=amendment, carry={}, parent_implementation=ctl["implementation"]))
            (run / "control.json").write_text(json.dumps(control))

            def reseal(rows):
                (run / "allocations.json").write_text(json.dumps(rows))
                seal = {k: digest((run / n).read_bytes()) for k, n in (
                    ("control_sha256", "control.json"), ("allocation_sha256", "allocations.json"))}
                (run / "completion.json").write_text(json.dumps(seal))

            with patch("agent_memory_eval.experiments.fresh_continuation.parent_evidence",
                       return_value=(amendment, cfg, None, ctl, {}, old, {})):
                child = dict(index=2, run_id="untouched", status="completed", result={"grade": True})
                reseal([old[0], child])
                self.assertEqual(verify_continuation(workspace, workspace, run), 2)
                for rows in ([dict(old[0], result={"grade": True}), child], [old[0], dict(child, run_id="new")]):
                    reseal(rows)
                    with self.assertRaises(ContractError):
                        verify_continuation(workspace, workspace, run)
