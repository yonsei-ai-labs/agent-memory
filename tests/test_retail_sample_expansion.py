import unittest
from collections import Counter
from pathlib import Path

from agent_memory_eval.contracts.core import digest
from agent_memory_eval.contracts.validation import ROOT
from scripts.run_full_retail import bank_parent, read, stage_order
from scripts.run_retail_sample_expansion import PROFILE, select_new


class RetailExpansionTests(unittest.TestCase):
    def test_new_tasks_require_one_identified_previously_unseen_customer(self):
        db = {"users": {"old": {}, "new": {}}, "orders": {"order": {"user_id": "new"}}}

        def task(*args):
            return {
                "evaluation_criteria": {"actions": [{"arguments": a} for a in args]}
            }

        tasks = {
            "old": task({"user_id": "old"}),
            "new": task({"order_id": "order"}, {"user_id": "new"}),
            "unknown": task({"order_id": "absent"}),
            "mixed": task({"user_id": "old"}, {"order_id": "order"}),
        }
        self.assertEqual(
            select_new(tasks, tasks, db, {digest(b"old")}),
            [{"task": "new", "customer": digest(b"new"), "split": "train"}],
        )

    def test_schedule_includes_each_frozen_bank_and_contemporaneous_controls(self):
        cfg = read(ROOT / PROFILE)
        reg = read(
            ROOT / "artifacts/analyses/20261119-retail-sample-expansion-preflight.json"
        )
        self.assertEqual(reg["protocol_sha256"], digest((ROOT / PROFILE).read_bytes()))
        old = [r for r in reg["rows"] if r["cohort"] == "repeat_test"]
        new = [r for r in reg["rows"] if r["cohort"] == "new_train"]
        self.assertEqual((len(old), len(new)), (40, 26))
        self.assertTrue(
            {r["customer"] for r in old}.isdisjoint(r["customer"] for r in new)
        )
        self.assertEqual(len({r["customer"] for r in new}), 18)
        expected = {f + "_original" for f in ("fact", "episode", "lesson", "procedure")}
        for row in old:
            self.assertEqual(set(row["stages"]), {"solve2", "solve3"})
            self.assertEqual(
                {w["arm"] for w in row["imported"]["write"]["runs"]}, expected
            )
            self.assertNotEqual(
                bank_parent(Path("."), cfg, row).parent.name, cfg["name"]
            )
        for row in new:
            self.assertEqual(set(row["stages"]), set(cfg["stages"]))
            self.assertEqual(bank_parent(Path("."), cfg, row).parent.name, cfg["name"])
        counts = Counter()
        for stage in cfg["stages"]:
            for row in stage_order(cfg, reg["rows"], stage):
                jobs = row["stages"][stage]
                counts[stage] += len(jobs)
                if stage.startswith("solve"):
                    self.assertEqual(
                        {j["arm"] for j in jobs},
                        expected | {"no_memory", "empty_preload"},
                    )
        self.assertEqual(
            counts,
            {"source": 26, "write": 104, "solve1": 156, "solve2": 396, "solve3": 396},
        )
