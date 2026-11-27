from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from agent_memory_eval.contracts.core import ContractError, canonical, digest


def write(path, value):
    path.write_bytes(canonical(value) + b"\n")
    return digest(path.read_bytes())


def fake_shard(args):
    """A sealed shard that spends one cent, shaped like an execute_prepared batch."""
    common, case, stage, grant = args
    folder = Path(common["root"]) / ("task-" + case["task"]) / stage
    if folder.exists():
        raise AssertionError("A sealed shard was sent twice")
    common["calls"].append((case["task"], stage, grant))
    folder.mkdir(parents=True)
    usd = common.get("usd", "0.01")
    control = write(folder / "control.json", dict(stage=stage))
    rows = write(folder / "allocations.json", [dict(index=1, status="completed",
                                                    result=dict(billing_by_role={"worker": {"reported_usd": usd}}))])
    write(folder / "completion.json", dict(control_sha256=control, allocation_sha256=rows, reserved_usd="0", http_calls=1))


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "runs" / "study-01"
        self.common = dict(root=str(self.root), calls=[])

    def tearDown(self):
        self.tmp.cleanup()

    def run_groups(self, groups, **kw):
        from agent_memory_eval.study.orchestrator import run_stages
        options = dict(total_usd_limit="1.00", max_concurrent=2, pool_factory=lambda n: ThreadPoolExecutor(n))
        options.update(kw)
        return run_stages(self.root, dict(plan="p"), groups, (fake_shard, self.common), **options)

    def test_stages_run_in_order_and_seal_every_shard(self):
        cases = [dict(task="a"), dict(task="b")]
        done = self.run_groups([("source", "source", cases), ("write", "write", cases)])
        calls = [c[:2] for c in self.common["calls"]]
        self.assertEqual(set(calls[:2]), {("a", "source"), ("b", "source")})
        self.assertEqual(set(calls[2:]), {("a", "write"), ("b", "write")})
        self.assertEqual(set(done["stages"]), {"task-a/source", "task-b/source", "task-a/write", "task-b/write"})
        self.assertEqual(done["total_covered_usd"], "0.04")
        self.assertTrue((self.root / "source-wave-1.json").is_file())
        sealed = json.loads((self.root / "completion.json").read_text())
        self.assertEqual(sealed["registration_sha256"], digest((self.root / "registration.json").read_bytes()))

    def test_exhausted_budget_leaves_later_shards_unfunded(self):
        cases = [dict(task="a"), dict(task="b")]
        done = self.run_groups([("source", "source", cases), ("write", "write", cases)], total_usd_limit="0.02")
        self.assertEqual(done["stages"]["task-a/write"]["status"], "not_started")
        self.assertEqual(done["stages"]["task-a/write"]["reason"], "spend_limit")
        self.assertFalse((self.root / "task-a/write").exists())

    def test_admission_reason_skips_a_shard(self):
        cases = [dict(task="a"), dict(task="b")]
        def admission(root, stage, case):
            return "source_ungraded" if case["task"] == "b" and stage == "write" else None
        done = self.run_groups([("source", "source", cases), ("write", "write", cases)], admission=admission)
        self.assertEqual(done["stages"]["task-b/write"]["reason"], "source_ungraded")
        self.assertNotIn(("b", "write"), [c[:2] for c in self.common["calls"]])

    def test_groups_can_be_produced_after_earlier_stages_finish(self):
        cases = [dict(task="a")]
        def groups():
            yield ("solve", "solve", cases)
            if (self.root / "task-a/solve/completion.json").is_file():
                yield ("solve-retry-1", "solve-retry-1", cases)
        done = self.run_groups(groups())
        self.assertIn("task-a/solve-retry-1", done["stages"])

    def test_resume_skips_sealed_shards_and_rejects_a_changed_registration(self):
        from agent_memory_eval.study.orchestrator import run_stages
        cases = [dict(task="a"), dict(task="b")]
        first = self.run_groups([("source", "source", cases)])
        (self.root / "completion.json").unlink()
        again = self.run_groups([("source", "source", cases)])
        self.assertEqual(first["stages"], again["stages"])
        self.assertEqual(len(self.common["calls"]), 2)
        with self.assertRaisesRegex(ContractError, "different contents"):
            run_stages(self.root, dict(plan="other"), [("source", "source", cases)], (fake_shard, self.common),
                       total_usd_limit="1.00", max_concurrent=2, pool_factory=lambda n: ThreadPoolExecutor(n))


if __name__ == "__main__":
    unittest.main()
