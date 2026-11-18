import unittest

from agent_memory_eval.analysis.forms_results import contrast


class PairingTests(unittest.TestCase):
    def test_partial_pairs_keep_task_weights_and_tight_missing_bounds(self):
        rows = []
        for t, rep, data in [("A", 1, [True, False]), ("A", 2, [True, False]),
                             ("B", 1, [False, True]), ("B", 2, [None, True])]:
            for arm, val in zip(("fact", "no_memory"), data):
                rows.append(dict(task_id=t, repetition=rep, arm=arm, index=len(rows), result=dict(official_success=val)))
        s = contrast(rows, "fact", "no_memory", {"A": "g1", "B": "g2"}, resamples=25)
        self.assertEqual(s["jointly_graded_pairs"], 3)
        self.assertEqual(s["counts"], dict(gain=2, loss=1, both_success=0, both_failure=0, ungraded=1))
        self.assertEqual(s["task_equal_difference_pp"], 0)
        self.assertEqual(s["possible_difference_pp"], [0, 25])
        self.assertEqual(s["leave_one_group_out_pp"], {"g1": -100, "g2": 100})
