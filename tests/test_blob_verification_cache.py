"""Writer-local reuse must preserve transitive integrity and cold-reader checks."""

from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, canonical
from agent_memory_eval.recording.journal import BlobStore, Journal, read_experiment


class BlobVerificationCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.store = BlobStore(self.root, cache_verification=True)
        self.leaf = self.store.put(dict(message="original"))
        self.parent = self.store.put(dict(children=[self.leaf, self.leaf]))

    def corrupt(self, ref):
        path = self.root / ref["path"]
        before = path.stat()
        data = path.read_bytes().replace(b"original", b"modified")
        self.assertEqual(len(data), before.st_size)
        path.write_bytes(data)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def test_reuses_reads_but_checks_child_metadata_and_strict_get(self):
        with patch.object(self.store, "get", wraps=self.store.get) as read:
            self.store.verify(self.parent)
            self.assertEqual(read.call_count, 2)
            self.store.verify(self.parent)
            self.assertEqual(read.call_count, 2)
        self.corrupt(self.leaf)
        with self.assertRaisesRegex(ContractError, "hash mismatch"):
            self.store.verify(self.parent)
        with self.assertRaisesRegex(ContractError, "hash mismatch"):
            self.store.get(self.leaf)

    def test_replacement_and_missing_descendant_are_not_hidden_by_parent(self):
        self.store.verify(self.parent)
        leaf_path = self.root / self.leaf["path"]
        replacement = leaf_path.with_name("replacement")
        replacement.write_bytes(canonical(dict(message="modified")))
        os.replace(replacement, leaf_path)
        with self.assertRaises(ContractError):
            self.store.verify(self.parent)
        leaf_path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.store.verify(self.parent)

    def test_cached_leaf_and_parent_directory_symlinks_are_refused(self):
        self.store.verify(self.parent)
        leaf_path = self.root / self.leaf["path"]
        saved = self.root / "saved.json"
        leaf_path.rename(saved)
        leaf_path.symlink_to(saved)
        with self.assertRaises(ContractError):
            self.store.verify(self.parent)
        leaf_path.unlink()
        saved.rename(leaf_path)
        directory = self.root / "blobs/sha256"
        moved = self.root / "original-blobs"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ContractError):
            self.store.verify(self.parent)

    def test_failed_parent_is_not_cached_and_can_be_repaired(self):
        original = (self.root / self.leaf["path"]).read_bytes()
        self.corrupt(self.leaf)
        with self.assertRaises(ContractError):
            self.store.verify(self.parent)
        self.assertNotIn((self.parent["path"], self.parent["sha256"]), self.store._verified)
        (self.root / self.leaf["path"]).write_bytes(original)
        self.store.verify(self.parent)

    def test_change_during_read_does_not_enter_cache(self):
        original = self.store.get
        def changed(ref):
            result = original(ref)
            self.corrupt(ref)
            return result
        with patch.object(self.store, "get", side_effect=changed):
            with self.assertRaisesRegex(ContractError, "changed during verification"):
                self.store.verify(self.leaf)
        self.assertEqual(self.store._verified, {})

    def test_independent_reader_rehashes_and_writer_reopen_starts_cold(self):
        with Journal(self.root, "cache") as journal:
            state = deepcopy(journal.state)
            state["example"] = self.parent
            journal.append([], state=state)
            with patch.object(journal.blobs, "get", wraps=journal.blobs.get) as read:
                journal.append([])
                self.assertEqual(read.call_count, 0)
        original = BlobStore.get
        reads = []
        def counted(store, ref):
            reads.append(ref["sha256"])
            return original(store, ref)
        with patch.object(BlobStore, "get", counted):
            with Journal(self.root, "cache"):
                pass
        self.assertIn(self.leaf["sha256"], reads)
        self.corrupt(self.leaf)
        with self.assertRaises(ContractError):
            read_experiment(self.root)
        with self.assertRaises(ContractError):
            Journal(self.root, "cache")

    def test_failed_verification_does_not_commit_and_retry_checks_repaired_blob(self):
        with Journal(self.root, "cache") as journal:
            state = deepcopy(journal.state)
            state["example"] = self.parent
            journal.append([], state=state)
            before = journal.path.read_bytes()
            original = (self.root / self.leaf["path"]).read_bytes()
            self.corrupt(self.leaf)
            with self.assertRaises(ContractError):
                journal.append([])
            self.assertEqual(journal.path.read_bytes(), before)
            (self.root / self.leaf["path"]).write_bytes(original)
            journal.append([])
            self.assertEqual(len(journal.scan["records"]), 2)

    def test_write_fault_poisoning_and_new_writer_verification_are_preserved(self):
        with Journal(self.root, "cache") as journal:
            state = deepcopy(journal.state)
            state["example"] = self.parent
            journal.append([], state=state)
            def fault(point):
                if point == "after_log":
                    raise OSError("injected fsync boundary failure")
            journal.fault = fault
            with self.assertRaises(OSError):
                journal.append([])
            with self.assertRaisesRegex(ContractError, "requires recovery"):
                journal.append([])
        with Journal(self.root, "cache") as reopened:
            self.assertEqual(len(reopened.scan["records"]), 2)
            reopened.append([])
        self.assertEqual(len(read_experiment(self.root)[0]["records"]), 3)


if __name__ == "__main__":
    unittest.main()
