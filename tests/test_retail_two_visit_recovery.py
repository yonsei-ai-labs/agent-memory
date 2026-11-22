import time
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, digest
from scripts.run_recheck_rounds import fixed
from scripts.run_retail_two_visit import ExpenseLedger
from scripts.run_retail_two_visit_recovery import attempt, available, unopened, upstream


class RecoveryTests(unittest.TestCase):
    def test_existing_model_submission_is_ineligible_even_with_a_missing_receipt(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            row = {"run": "r_case"}
            folder = root / row["run"]
            folder.mkdir()
            fixed(
                folder / "decision.json",
                {"status": "bank_missing", "journal_bytes": 0, "journal_sha256": digest(b"")},
            )
            unopened(root, row, "decision", set())
            with self.assertRaises(ContractError):
                unopened(root, row, "decision", {("decision", "r_case")})
            (folder / "log.jsonl").write_bytes(b"entered")
            with self.assertRaises(ContractError):
                unopened(root, row, "decision", set())

    def test_supplement_reserves_original_unknown_cost_and_shares_expiration(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            auth = {"max_usd": 13, "max_requests": 20, "max_seconds": 60}
            fixed(root / "authorization.json", auth)
            fixed(root / "start.json", {"unix_time": 123})
            fixed(root / "registration.json", {"jobs": {"decision": [1, 2]}})
            ledger = ExpenseLedger(root / "expense.jsonl", 13, 20, time.monotonic() + 60)
            ledger.append({"seq": 1, "reserved": "1.20"})
            ledger.append({"seq": 1, "cost": "0.34"})
            ledger.append({"seq": 2, "reserved": "2.50"})
            fixed(
                root / "summary.json",
                {
                    "confirmed_usd": "0.34",
                    "held_usd": "2.50",
                    "reserved_calls": 2,
                    "stages": {"decision": {"checkpoint": 1, "bank_missing": 1}},
                },
            )
            v = available(root, {"authorization": {"combined_max_usd": 13}})
            self.assertEqual(
                (Decimal(v["max_usd"]), v["max_requests"], v["deadline_unix"]), (Decimal("10.16"), 18, 183)
            )

    def test_retry_only_technical_upstream_and_keep_voluntary_empty(self):
        profile = {"name": "supplement", "transient_errors": ["HTTP_503"]}
        jobs = [attempt({"run": key, "arm": key, "task": key}, 1, "supplement") for key in ("a", "b", "c")]
        seen = []
        runtime = {"guard": SimpleNamespace(denied=False, deadline=time.monotonic() + 600)}

        def offline(study, rt, *, through, continuation, destination):
            for j in study["jobs"]["writer"]:
                seen.append((j["original_run"], j["attempt"], j["run"]))
                folder = destination / j["run"]
                folder.mkdir(exist_ok=True)
                if j["original_run"] == "a" and j["attempt"] == 1:
                    result = {"status": "error", "provider_error": "HTTP_503"}
                elif j["original_run"] == "b":
                    result = {"status": "empty"}
                elif j["original_run"] == "c":
                    result = {"status": "incomplete", "reason": "writer stopped"}
                else:
                    result = {"status": "completed"}
                fixed(folder / "writer.json", {**result, "journal_bytes": 0, "journal_sha256": digest(b"")})

        with (
            TemporaryDirectory() as d,
            patch("scripts.run_retail_two_visit_recovery.execute", side_effect=offline),
            patch("scripts.run_retail_two_visit_recovery.time.sleep"),
        ):
            chosen, attempts = upstream("writer", jobs, [], {}, runtime, Path(d), profile)
            self.assertEqual([(orig, n) for orig, n, _ in seen], [("a", 1), ("b", 1), ("c", 1), ("a", 2)])
            self.assertEqual(len({rid for _, _, rid in seen}), len(attempts))
            self.assertEqual({j["original_run"]: j["attempt"] for j in chosen}, {"a": 2, "b": 1, "c": 1})


if __name__ == "__main__":
    unittest.main()
