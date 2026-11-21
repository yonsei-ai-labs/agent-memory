import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError
from scripts import complete_retail_transfer_revision as pending
from scripts.run_retail_related_transfer import design_for


class PendingRetailRevisionTests(unittest.TestCase):
    def test_sealed_currency_allows_trailing_zeros_and_preserves_every_other_check(self):
        from scripts import run_full_retail as retail

        actual = {"spent": "0.55277", "held": "0.5000", "reported": "0.05277", "http": 7, "seal": "abc"}
        expected = dict(actual, spent="0.552770", held="0.5")
        with patch.object(retail, "accounting", return_value=actual):
            self.assertTrue(retail.matches_accounting_seal(Path("."), 0.55277, expected))
            self.assertFalse(retail.matches_accounting_seal(Path("."), 0.55277, dict(expected, held="0.51")))
            self.assertFalse(retail.matches_accounting_seal(Path("."), 0.55277, dict(expected, seal="other")))

    def test_received_gateway_error_requires_the_matching_wire_and_fixed_ceiling(self):
        from agent_memory_eval.contracts.core import digest
        from scripts import audit_retail_revision_reservations as accounting

        model = {
            "id": "worker",
            "selected_endpoint": {"tag": "route"},
            "pricing": {"prompt": 0.0001, "completion": 0.0002},
        }
        wire = {
            "model": "worker",
            "provider": {"only": ["route"], "allow_fallbacks": False, "require_parameters": True},
            "stream": False,
            "messages": [{"role": "user", "content": "example"}],
            "max_completion_tokens": 100,
        }
        result = {
            "error_code": "HTTP_520",
            "log_sha256": digest(b"closed"),
            "billing_by_role": {"worker": {"reported_usd": "0", "unknown_count": 1}},
        }
        slot = {"directory": "job", "run_id": "r", "result": result}
        start = {"request_id": "q", "actor_role": "worker", "request_ref": "wire"}
        end = {
            "request_id": "q",
            "delivery_status": "response_received",
            "status": "error",
            "cost_status": "unknown",
            "response_ref": "reply",
        }
        scan = {
            "starts": {"r": {}},
            "events": {
                k: {"type": "model.request_" + k, "run_id": "r", "payload": v}
                for k, v in (("started", start), ("finished", end))
            },
        }
        blobs = {"wire": wire, "reply": {"error": {"code": 520}}}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(accounting, "read_experiment", return_value=(scan, None, None)),
            patch.object(accounting, "BlobStore", return_value=blobs),
        ):
            folder = Path(tmp)
            (folder / "job").mkdir()
            (folder / "job/log.jsonl").write_bytes(b"closed")
            proof = accounting.bounded_receipt(folder, slot, {"worker": model}, {})
            self.assertEqual(
                (proof["code"], proof["role"], proof["usd"]),
                (520, "worker", str(accounting.request_reservation_usd(wire, model))),
            )
            blobs["reply"]["error"]["code"] = 502
            with self.assertRaises(ContractError):
                accounting.bounded_receipt(folder, slot, {"worker": model}, {})

    def test_cutover_accepts_only_undispatched_shards(self):
        import json

        from scripts import retail_transfer_cutover as cutover

        cfg = {"name": "sample", "stages": ["solve1"], "concurrency_by_stage": {"solve1": 1}}
        pre = {"stage_order": {"solve1": ["a", "b"]}, "targets": {"b": {"jobs": {"solve1": [{"index": 1}]}}}}
        reg = {"preflight": {"authorized_usd": "2.00"}, "implementation": {"implementation_commit": "commit"}}
        ctl = {
            "profile": {"name": "sample", "task": "a", "stage": "solve1", "guards": {"max_usd": 2}},
            "implementation": reg["implementation"],
        }
        bill = {"reported": "0.1", "held": "0", "spent": "0.1", "http": 1, "seal": "one"}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                cutover.study.retail, "sealed_rows", return_value=({}, ctl, [{"status": "completed"}])
            ),
            patch.object(cutover.study.retail, "accounting", return_value=bill),
        ):
            root = Path(tmp)
            (root / "registration.json").write_text(json.dumps(reg))
            (root / "solve1-wave-1.json").write_text(
                json.dumps({"tasks": ["a"], "prior_covered": "0", "per_shard_grant": "2.000000"})
            )
            stages, proof = cutover.inventory(root, cfg, pre, reg)
            self.assertEqual(stages["task-b/solve1"]["reason"], "deferred_to_supplement")
            self.assertEqual(proof["deferred"]["task-b/solve1"]["positions"], 1)
            self.assertEqual(proof["total_covered_usd"], "0.1")
            (root / "task-b/solve1").mkdir(parents=True)
            with self.assertRaises(ContractError):
                cutover.inventory(root, cfg, pre, reg)

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
