"""Integrity failure checks for the archive verifier, independent of large data."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_legacy_data.py"
SPEC = importlib.util.spec_from_file_location("verify_legacy_data", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class LegacyArchiveIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.archive = self.root / "archive/demo"
        self.name = "datasets/example-v1/body.txt"
        self.path = self.archive / self.name
        self.path.parent.mkdir(parents=True)
        self.data = '기억 "원문"\r\n다음 줄\n'.encode()
        self.path.write_bytes(self.data)
        self.manifest = {
            "format": "agent-memory-legacy-data-archive/v1",
            "snapshot_id": "test-only",
            "archive_root": "archive/demo",
            "selection": {
                "directory_roots": ["datasets"], "file_globs": [],
                "individual_files": [], "excluded_directory_names": ["__pycache__"],
                "excluded_file_suffixes": [".pyc", ".pyo"],
            },
            "summary": {
                "files": 1, "bytes": len(self.data), "files_by_role": {"dataset": 1},
                "files_by_source_git_status": {"??": 1}, "dataset_families": 1,
            },
            "files": [{
                "source_path": self.name, "bytes": len(self.data),
                "sha256": hashlib.sha256(self.data).hexdigest(),
                "role": "dataset", "source_git_status": "??",
            }],
        }
        (self.root / "artifacts").mkdir()
        self.write_manifest()

    def write_manifest(self):
        (self.root / verifier.MANIFEST).write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_original_bytes_pass_without_source_checkout(self):
        self.assertEqual(verifier.verify(self.root)["files"], 1)

    def test_line_ending_change_is_detected(self):
        self.path.write_bytes(self.data.replace(b"\r\n", b"\n"))
        with self.assertRaisesRegex(ValueError, "hash or size mismatch"):
            verifier.verify(self.root)

    def test_missing_file_is_detected(self):
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "Expected regular file"):
            verifier.verify(self.root)

    def test_unlisted_file_is_detected(self):
        (self.path.parent / "unexpected.txt").write_bytes(b"extra")
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            verifier.verify(self.root)

    def test_path_escape_is_rejected(self):
        self.manifest["files"][0]["source_path"] = "../outside.txt"
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "Invalid archive path"):
            verifier.verify(self.root)

    def test_symlink_is_rejected(self):
        self.path.unlink()
        target = self.root / "external.txt"
        target.write_bytes(self.data)
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            verifier.verify(self.root)

    def test_changed_source_is_detected(self):
        source = self.root / "source"
        path = source / self.name
        path.parent.mkdir(parents=True)
        path.write_bytes(self.data)
        self.assertEqual(verifier.verify(self.root, source)["current_source_files_verified"], 1)
        path.write_bytes(b"changed source")
        with self.assertRaisesRegex(ValueError, "Current source differs"):
            verifier.verify(self.root, source)

    def test_new_source_file_is_detected(self):
        source = self.root / "source"
        path = source / self.name
        path.parent.mkdir(parents=True)
        path.write_bytes(self.data)
        (path.parent / "added.txt").write_bytes(b"new source item")
        with self.assertRaisesRegex(ValueError, "Current source inventory differs"):
            verifier.verify(self.root, source)


if __name__ == "__main__":
    unittest.main()
