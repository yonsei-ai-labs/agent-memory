"""Read-only batch registration, input checks and staging on synthetic local files."""

import fcntl
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from agent_memory_eval.reproduction import runner


class HistoricalBatchInputTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.original = self.base / "source"
        self.worktree = self.base / "staged"
        self.root = self.original / "runs/sealed"
        self.jobs = self.root / "jobs"
        self.item = dict(kind="run_batch", path="runs/sealed")
        self.entry = dict(inputs=[self.item], reproduction_commit="a" * 40, implementation_commit="a" * 40)
        self.config = dict(entry=self.entry, experiment="batch", frozen_ref_count=0)
        self.rows = []
        for name in ("0001", "0002"):
            directory = self.jobs / name
            directory.mkdir(parents=True)
            (directory / "writer.lock").touch()
            (directory / "log.jsonl").write_bytes(("journal " + name).encode())
            self.rows.append(dict(directory="jobs/" + name, status="completed",
                                  result=dict(log_sha256=runner.file_hash(directory / "log.jsonl"))))
        self.rows.append(dict(directory="jobs/0003", status="not_started"))
        data = b"immutable observation"
        self.blob = self.jobs / "0002/blobs/sha256" / runner.sha(data)
        self.blob.parent.mkdir(parents=True)
        self.blob.write_bytes(data)
        (self.root / "control.json").write_text("{}")
        self.seal()

    def seal(self):
        allocations = self.root / "allocations.json"
        allocations.write_text(json.dumps(self.rows))
        control = self.root / "control.json"
        summary = dict(allocation_sha256=runner.file_hash(allocations))
        if control.exists():
            summary["control_sha256"] = runner.file_hash(control)
        completion = self.root / "completion.json"
        completion.write_text(json.dumps(summary))
        self.item["completion_sha256"] = runner.file_hash(completion)

    def check(self):
        return runner.preflight(self.config, self.original)

    def test_independent_read_only_copies_and_no_source_lock_creation(self):
        with runner.input_leases(self.original, self.entry):
            manifest = runner.inventory_inputs(self.original, self.entry)
            runner.stage_inputs(self.original, self.worktree, manifest, self.entry)
        self.assertEqual(self.check()["input_files"], len(manifest))
        self.assertEqual(runner.inventory_inputs(self.worktree, self.entry), manifest)
        for row in manifest:
            source, copy = self.original / row["path"], self.worktree / row["path"]
            self.assertFalse(copy.samefile(source))
            self.assertEqual(copy.stat().st_mode & 0o222, 0)
        self.assertFalse((self.root / "writer.lock").exists())
        self.assertFalse((self.jobs / "0003").exists())

    def test_tampering_at_each_level_fails_before_staging(self):
        for path in (self.root / "completion.json", self.root / "allocations.json",
                     self.root / "control.json", self.jobs / "0002/log.jsonl", self.blob):
            with self.subTest(path=path.relative_to(self.root)):
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaisesRegex(runner.ReproductionError, "checksum mismatch"):
                    self.check()
                path.write_bytes(original)
        (self.jobs / "0001/log.jsonl").unlink()
        with self.assertRaisesRegex(runner.ReproductionError, "checksum mismatch"):
            self.check()
        self.assertFalse(self.worktree.exists())

    def test_existing_root_or_job_writer_blocks_and_releases_earlier_leases(self):
        root_lock = self.root / "writer.lock"
        root_lock.touch()
        for path in (root_lock, self.jobs / "0002/writer.lock"):
            with self.subTest(lock=path), path.open("rb") as writer:
                fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(runner.ReproductionError, "active writer"):
                    self.check()
            with (self.jobs / "0001/writer.lock").open("rb") as released:
                fcntl.flock(released, fcntl.LOCK_EX | fcntl.LOCK_NB)
        missing = self.jobs / "0001/writer.lock"
        missing.unlink()
        with self.assertRaisesRegex(runner.ReproductionError, "Missing regular"):
            self.check()
        self.assertFalse(missing.exists())

    def test_missing_extra_or_symlinked_job_is_rejected(self):
        first, extra = self.jobs / "0001", self.jobs / "0003"
        first.rename(extra)
        with self.assertRaisesRegex(runner.ReproductionError, "differ from"):
            self.check()
        extra.rename(first)
        extra.mkdir()
        (extra / "writer.lock").touch()
        with self.assertRaisesRegex(runner.ReproductionError, "differ from"):
            self.check()
        shutil.rmtree(extra)
        outside = self.base / "moved-original"
        first.rename(outside)
        first.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(runner.ReproductionError, "Symlink"):
            self.check()

    def test_allocations_cannot_share_or_escape_job_directories(self):
        for name in ("jobs/0001", "jobs/../elsewhere", "outside", "jobs/aa/bb"):
            with self.subTest(name=name):
                self.rows[-1]["directory"] = name
                self.seal()
                with self.assertRaises(runner.ReproductionError):
                    self.check()

    def test_batch_with_no_materialized_jobs_stages_the_empty_jobs_directory(self):
        self.rows = [dict(directory=row["directory"], status="not_started") for row in self.rows]
        for path in self.jobs.iterdir():
            shutil.rmtree(path)
        (self.root / "control.json").unlink()
        self.seal()
        self.assertEqual(self.check()["input_files"], 2)
        manifest = runner.inventory_inputs(self.original, self.entry)
        runner.stage_inputs(self.original, self.worktree, manifest, self.entry)
        self.assertEqual(runner.inventory_inputs(self.worktree, self.entry), manifest)
        self.assertEqual(list((self.worktree / self.item["path"] / "jobs").iterdir()), [])

    def test_single_run_and_file_still_use_their_v1_hashes(self):
        name = self.item["path"] + "/jobs/0001"
        checksum = self.rows[0]["result"]["log_sha256"]
        control = self.root / "control.json"
        self.entry["inputs"] = [dict(kind="run", path=name, log_sha256=checksum),
                                dict(kind="file", path=self.item["path"] + "/control.json", sha256=runner.file_hash(control))]
        self.assertEqual(self.check()["input_files"], 3)
        (self.jobs / "0001/log.jsonl").write_bytes(b"edited")
        with self.assertRaisesRegex(runner.ReproductionError, "checksum mismatch"):
            self.check()

    def test_batch_registration_requires_v2_and_a_pinned_completion(self):
        files = {"artifacts/protocol.json": json.dumps({"implementation_files": [], "planned_output": self.item["path"]}).encode(),
                 "scripts/review.py": b"pass", "pyproject.toml": b"", "uv.lock": b""}
        def ref(name):
            return dict(path=name, sha256=runner.sha(files[name]))
        entry = dict(self.entry, protocol_lock_ref=ref("artifacts/protocol.json"), review_script_ref=ref("scripts/review.py"),
                     environment=dict(project_ref=ref("pyproject.toml"), lock_ref=ref("uv.lock"), python_version="3.12", extras=[]),
                     expected_outputs=[])
        registry = self.base / "registry.json"
        def load(fmt):
            registry.write_text(json.dumps(dict(format=fmt, intent="read_only_historical_review", experiments=dict(batch=entry))))
            return runner.load_entry(self.original, registry, "batch")
        def git_reads(repo, command, *args):
            if command == "rev-parse":
                return (str(repo) if args[0] == "--show-toplevel" else self.entry["reproduction_commit"]).encode()
            self.assertEqual(command, "cat-file")
            return files[args[-1].split(":", 1)[1]]
        with patch.object(runner, "git", side_effect=git_reads):
            self.assertEqual(load(runner.BATCH_REGISTRY_FORMAT)["entry"], entry)
            with self.assertRaises(runner.ReproductionError):
                load(runner.REGISTRY_FORMAT)
            for checksum in ("not-a-hash", None):
                entry["inputs"] = [dict(self.item, completion_sha256=checksum)]
                with self.subTest(checksum=checksum), self.assertRaises(runner.ReproductionError):
                    load(runner.BATCH_REGISTRY_FORMAT)
            entry["inputs"] = [dict(kind="run", path=self.item["path"], log_sha256="0" * 64)]
            self.assertEqual(load(runner.REGISTRY_FORMAT)["entry"], entry)
