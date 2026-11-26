import unittest
from collections import Counter

from agent_memory_eval.contracts.core import ContractError
from scripts.continue_fresh_matched import AMENDMENT
from scripts.run_fresh_matched import PREFLIGHT, PROFILE, ROOT, STAGES, read
from scripts.run_full_retail import release_groups, stage_order


class ContinuedReleaseTests(unittest.TestCase):
    def test_check_release_preserves_every_original_stage_assignment_once(self):
        registration = read(ROOT / PREFLIGHT)
        cases = registration["cases"]
        amendment = read(ROOT / AMENDMENT)
        stages = ["initial-" + s for s in STAGES] + list(STAGES)
        config = dict(read(ROOT / PROFILE), stages=stages)
        assigned = Counter((r["task"], s) for r in cases for s in r["scheduled_stages"])
        for groups in (release_groups(config, cases),
                       release_groups(dict(config, first_remaining_tasks=amendment["first_remaining_tasks"]), cases)):
            actual = Counter((r["task"], s) for _, s, rows in groups for r in rows)
            self.assertEqual(actual, assigned)
            for _, stage, rows in groups:
                self.assertEqual(stage_order(config, rows, stage), rows)
        self.assertEqual([g[0] for g in groups], stages[:3] + ["check-" + s for s in STAGES] + list(STAGES))
        for _, _, group in groups[3:6]:
            self.assertEqual({r["task"] for r in group}, set(amendment["first_remaining_tasks"]))
        with self.assertRaises(ContractError):
            release_groups(dict(config, first_remaining_tasks=list(amendment["original_sources"])), cases)
