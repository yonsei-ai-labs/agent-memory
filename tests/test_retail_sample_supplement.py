import tempfile
import unittest
from pathlib import Path
from collections import Counter
from decimal import Decimal

from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.episode_admission import EpisodeAdmission
from scripts.run_retail_sample_supplement import ALLOCATION, RECEIPT, retail


class RetailSupplementTests(unittest.TestCase):
    def test_admission_waits_outside_the_next_episode_without_repeating_a_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            moment = [100.0]
            seen = []
            responses = iter(
                [{}, {"error_code": "HTTP_503"}, {"error_code": "HTTP_503"}, {}]
            )
            waits = []

            def sleep(t):
                waits.append(t)
                moment[0] += t

            def execute(row, _):
                seen.append(row["run_id"])
                return next(responses)

            root = Path(tmp)
            gate = EpisodeAdmission(
                root,
                {"codes": ["HTTP_503"], "seconds": [30, 60]},
                now=lambda: moment[0],
                sleep=sleep,
            )
            for name in ("a", "b", "c", "d"):
                gate.run(execute, {"run_id": name}, None)
            self.assertEqual(seen, ["a", "b", "c", "d"])
            self.assertEqual(sum(waits), 90)
            self.assertEqual(retail.read(root / "admission.json")["failures"], 0)

    def test_supplement_matches_unstarted_runs_and_prices_all_received_errors(self):
        receipt = retail.read(ROOT / RECEIPT)
        record = retail.read(ROOT / ALLOCATION)
        counts = Counter()
        all_arms = []
        for r in record["rows"]:
            for stage, jobs in r["stages"].items():
                counts[stage] += len(jobs)
                if stage == "solve1":
                    self.assertEqual(
                        [(j["index"], j["arm"]) for j in jobs],
                        [
                            (x["index"], x["arm"])
                            for x in receipt["unstarted"][r["task"]]
                        ],
                    )
                    all_arms.extend((r["task"], j["arm"]) for j in jobs)
        self.assertEqual(counts, {"solve1": 12, "solve2": 396, "solve3": 396})
        self.assertTrue(
            set(all_arms).isdisjoint((v["task"], v["arm"]) for v in receipt["receipts"])
        )
        self.assertEqual(len(set(x["request"] for x in receipt["receipts"])), 24)
        self.assertEqual(
            sum(Decimal(x["maximum_usd"]) for x in receipt["receipts"])
            + Decimal(receipt["reported_usd"]),
            Decimal(receipt["covered_usd"]),
        )
