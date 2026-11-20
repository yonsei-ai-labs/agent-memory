import unittest
from collections import Counter
from decimal import Decimal

from agent_memory_eval.contracts.validation import ROOT
from scripts.run_retail_sample_recovery import ALLOCATION, PROFILE, RECEIPT, retail


class RetailSampleRecoveryTests(unittest.TestCase):
    def test_frozen_continuation_has_only_untouched_original_indices(self):
        observed = retail.read(ROOT / RECEIPT)
        plan = retail.read(ROOT / ALLOCATION)
        untouched = {
            (x["task"], x["stage"], x["index"], x["arm"]) for x in observed["untouched"]
        }
        scheduled = [
            (r["task"], s, j["index"], j["arm"])
            for r in plan["rows"]
            for s, jobs in r["stages"].items()
            for j in jobs
        ]
        self.assertEqual(set(scheduled), untouched)
        self.assertEqual(len(scheduled), len(untouched))
        self.assertEqual(
            Counter(x[1] for x in scheduled), {"solve2": 75, "solve3": 396}
        )
        self.assertEqual([x[2] for x in scheduled if x[:2] == ("101", "solve2")], [6])
        self.assertTrue(
            {(x["task"], "solve2", x["arm"]) for x in observed["uncertain"]}.isdisjoint(
                (t, stage, arm) for t, stage, _, arm in scheduled
            )
        )

    def test_each_uncertain_request_is_charged_once_at_its_full_maximum(self):
        observed = retail.read(ROOT / RECEIPT)
        plan = retail.read(ROOT / PROFILE)
        charges = observed["uncertain"]
        self.assertEqual(len({x["request"] for x in charges}), 10)
        self.assertEqual(
            Counter(x["status"] for x in charges), {"open": 1, "transport_unknown": 9}
        )
        maxima = sum(Decimal(x["maximum_usd"]) for x in charges)
        self.assertEqual(maxima, Decimal(plan["ten_worker_request_maxima_usd"]))
        self.assertEqual(
            Decimal(observed["covered_usd"]),
            Decimal(observed["reported_usd"])
            + Decimal(observed["initial_503_maxima_usd"])
            + maxima,
        )
        self.assertEqual(
            Decimal(plan["total_usd_limit"]) - Decimal(observed["covered_usd"]),
            Decimal("3.1651083448"),
        )
