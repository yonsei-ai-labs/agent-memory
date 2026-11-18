"""Real Git worktrees and isolated subprocesses; no model or network calls."""

import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import venv

from agent_memory_eval.reproduction.runner import (
    ReproductionError,
    load_entry,
    preflight,
    reproduce,
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class HistoricalReproductionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "repository"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Offline Test")
        self.git("config", "user.email", "offline@example.invalid")
        files = {
            ".gitignore": "/runs/\n/data/\n*.egg-info/\n.venv/\n__pycache__/\n",
            "src/agent_memory_eval/__init__.py": '"""Historical fixture package."""\n',
            "src/agent_memory_eval/version.py": 'VALUE = "historical"\n',
            "protocols/profile.json": "{}\n",
            "pyproject.toml": '[project]\nname="reproduction-fixture"\nversion="0.0.0"\n',
            "uv.lock": "version = 1\n",
            "artifacts/result.json": '{"success":true}\n',
        }
        self.body = '''import json
from pathlib import Path
from agent_memory_eval.version import VALUE
root = Path(__file__).resolve().parents[1]
assert VALUE == "historical"
assert (root / "runs/fixed/log.jsonl").read_text() == "original\\n"
assert (root / "runs/fixed/blob.json").read_text() == "preserved\\n"
assert json.loads((root / "artifacts/result.json").read_text()) == {"success": True}
print(json.dumps({"review": "passed"}))
'''
        files["scripts/review.py"] = self.body
        for name, text in files.items():
            self.write(name, text)
        lock = dict(implementation_files=[self.ref("src/agent_memory_eval/version.py")],
                    profile_ref=self.ref("protocols/profile.json"), planned_output="runs/fixed")
        self.write("artifacts/protocol.json", json.dumps(lock))
        self.git("add", ".")
        self.git("commit", "-qm", "Historical implementation and analysis")
        self.revision = self.git("rev-parse", "HEAD").strip()
        self.write("runs/fixed/log.jsonl", "original\n")
        self.write("runs/fixed/blob.json", "preserved\n")
        self.write("runs/fixed/writer.lock", "")
        self.registry = self.base / "registry.json"
        self.output = self.base / "reproduction"
        self.save_registry()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE, text=True)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def ref(self, name):
        return dict(path=name, sha256=digest((self.repo / name).read_bytes()))

    def save_registry(self, extra_script=""):
        if extra_script:
            self.write("scripts/review.py", self.body + extra_script)
            self.git("add", "scripts/review.py")
            self.git("commit", "-qm", "Additional historical review behavior")
            self.revision = self.git("rev-parse", "HEAD").strip()
        self.entry = dict(reproduction_commit=self.revision, implementation_commit=self.revision,
            protocol_lock_ref=self.ref("artifacts/protocol.json"), review_script_ref=self.ref("scripts/review.py"),
            environment=dict(lock_ref=self.ref("uv.lock"), project_ref=self.ref("pyproject.toml"),
                             python_version="3.12", extras=[]),
            expected_outputs=[self.ref("artifacts/result.json")],
            inputs=[dict(kind="run", path="runs/fixed", log_sha256=digest(b"original\n"))])
        self.flush_registry()

    def flush_registry(self):
        self.registry.write_text(json.dumps(dict(format="agent-memory-historical-reproduction-registry/v1",
            intent="read_only_historical_review", experiments=dict(fixture=self.entry))))

    def config(self):
        return load_entry(self.repo, self.registry, "fixture")

    def fixture_environment(self, worktree, output, environment, source=None):
        # A standard-library-only editable fixture stands in for uv's installation.
        # The three real historical reviews separately exercise locked uv sync.
        venv.EnvBuilder(with_pip=False).create(worktree / ".venv")
        packages = next((worktree / ".venv/lib").glob("python*/site-packages"))
        (packages / "historical-fixture.pth").write_text(str((source or worktree) / "src") + "\n")
        (output / "environment.log").write_text("standard-library fixture environment\n")
        return dict(kind="stdlib_fixture", extras=environment["extras"])

    def run_review(self, **kwargs):
        return reproduce(self.config(), self.repo, self.output,
                         environment_factory=kwargs.pop("environment_factory", self.fixture_environment), **kwargs)

    def assert_cleanup(self, result):
        self.assertEqual(result["cleanup"]["status"], "removed")
        self.assertFalse((self.output / "checkout").exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual((self.repo / "runs/fixed/blob.json").read_text(), "preserved\n")

    def test_historical_imports_ignore_modified_main_and_credentials(self):
        self.write("src/agent_memory_eval/version.py", 'VALUE = "changed-main"\n')
        self.git("add", "src/agent_memory_eval/version.py")
        self.git("commit", "-qm", "Evolving main")
        head = self.git("rev-parse", "HEAD")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "offline-test-key", "OPENROUTER_API_KEY": "offline-test-key"}):
            result = self.run_review()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["reproduction_commit"], self.revision)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertIn("changed-main", (self.repo / "src/agent_memory_eval/version.py").read_text())
        self.assertTrue(result["source_inputs_preserved"] and result["staged_inputs_preserved"])
        self.assertFalse(result["child"]["api_role_keys_present"])
        self.assertTrue(result["child"]["assertions_enabled"])
        self.assertIn("agent_memory_eval.version", result["child"]["imported_modules"])
        self.assert_cleanup(result)

    def test_preflight_has_no_worktree_or_output_side_effect(self):
        before = self.git("worktree", "list", "--porcelain")
        result = preflight(self.config(), self.repo)
        self.assertEqual(result["input_files"], 3)
        self.assertEqual(result["benchmark_or_model_calls"], 0)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain"), before)

    def test_bad_commit_and_script_hash_are_rejected(self):
        for key, replacement in (("reproduction_commit", "0" * 40),
                                 ("review_script_ref", dict(path="scripts/review.py", sha256="0" * 64))):
            original = self.entry[key]
            self.entry[key] = replacement
            self.flush_registry()
            with self.assertRaises(ReproductionError):
                self.config()
            self.entry[key] = original
        self.assertFalse(self.output.exists())

    def test_tampered_or_missing_raw_log_is_rejected(self):
        log = self.repo / "runs/fixed/log.jsonl"
        for value in ("tampered\n", None):
            if value is None:
                log.unlink()
            else:
                log.write_text(value)
            with self.assertRaises(ReproductionError):
                self.run_review()
        self.assertFalse(self.output.exists())

    def test_symlink_input_and_active_writer_are_rejected(self):
        blob = self.repo / "runs/fixed/blob.json"
        blob.unlink()
        blob.symlink_to(self.repo / "artifacts/result.json")
        with self.assertRaises(ReproductionError):
            self.run_review()
        blob.unlink()
        blob.write_text("preserved\n")
        with (self.repo / "runs/fixed/writer.lock").open("rb") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ReproductionError):
                self.run_review()
        self.assertFalse(self.output.exists())

    def test_missing_or_tampered_external_asset_is_rejected(self):
        self.entry["inputs"].append(dict(kind="file", path="data/asset.txt", sha256=digest(b"asset")))
        self.flush_registry()
        with self.assertRaises(OSError):
            self.run_review()
        self.write("data/asset.txt", "wrong asset")
        with self.assertRaises(ReproductionError):
            self.run_review()
        self.assertFalse(self.output.exists())

    def test_overlapping_input_paths_and_existing_output_are_rejected(self):
        self.entry["inputs"].append(dict(kind="file", path="runs/fixed/log.jsonl", sha256=digest(b"original\n")))
        self.flush_registry()
        with self.assertRaises(ReproductionError):
            self.config()
        self.entry["inputs"].pop()
        self.flush_registry()
        self.output.mkdir()
        (self.output / "keep.txt").write_text("user file")
        with self.assertRaises(ReproductionError):
            self.run_review()
        self.assertEqual((self.output / "keep.txt").read_text(), "user file")

    def test_input_write_attempt_is_preserved_as_failure(self):
        self.save_registry('''
try:
    (root / "runs/fixed/blob.json").write_text("bad write")
except RuntimeError:
    pass
''')
        result = self.run_review()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["child"]["protected_write_attempts"], 1)
        self.assertTrue(result["source_inputs_preserved"] and result["staged_inputs_preserved"])
        self.assert_cleanup(result)

    def test_external_scratch_cleanup_uses_its_own_directory_descriptor(self):
        self.save_registry('''
from tempfile import TemporaryDirectory
with TemporaryDirectory() as temporary:
    scratch = Path(temporary) / "samples"
    scratch.mkdir()
    (scratch / "blob.json").write_bytes(b"scratch")
assert not Path(temporary).exists()
''')
        result = self.run_review()
        self.assertEqual(result["status"], "passed", result.get("error"))
        self.assertEqual(result["child"]["protected_write_attempts"], 0)
        self.assert_cleanup(result)

    def test_directory_relative_unlink_and_rename_preserve_worktree_inputs(self):
        self.save_registry('''
import os
from contextlib import chdir
from tempfile import TemporaryDirectory
with TemporaryDirectory() as temporary, chdir(temporary):
    Path("scratch").write_bytes(b"scratch")
    protected = os.open(root / "runs/fixed", os.O_RDONLY)
    try:
        for operation in (
            lambda: os.unlink("blob.json", dir_fd=protected),
            lambda: os.rename("blob.json", "moved", src_dir_fd=protected),
            lambda: os.rename("scratch", "moved", dst_dir_fd=protected),
        ):
            try:
                operation()
            except RuntimeError:
                pass
    finally:
        os.close(protected)
''')
        result = self.run_review()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["child"]["protected_write_attempts"], 3)
        self.assertTrue(result["source_inputs_preserved"] and result["staged_inputs_preserved"])
        self.assert_cleanup(result)

    def test_network_attempt_cannot_be_hidden_by_catching_the_exception(self):
        self.save_registry('''
import socket
try:
    socket.create_connection(("127.0.0.1", 9))
except RuntimeError:
    pass
''')
        result = self.run_review()
        self.assertEqual(result["status"], "failed")
        self.assertGreater(result["child"]["network_attempts"], 0)
        self.assert_cleanup(result)

    def test_nested_process_attempt_is_blocked(self):
        self.save_registry('''
import subprocess, sys
try:
    subprocess.run([sys.executable, "-c", "print('unexpected')"])
except RuntimeError:
    pass
''')
        result = self.run_review()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["child"]["process_attempts"], 1)
        self.assert_cleanup(result)

    def native_fixture(self):
        root = self.repo / "data/benchmark"
        root.mkdir(parents=True)
        def native_git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE, text=True)
        native_git("init", "-q")
        native_git("config", "user.name", "Offline Test")
        native_git("config", "user.email", "offline@example.invalid")
        (root / "asset.txt").write_text("native source\n")
        (root / "internal-link").symlink_to("asset.txt")
        native_git("add", ".")
        native_git("commit", "-qm", "Pinned benchmark")
        self.entry["inputs"].append(dict(kind="git_checkout", path="data/benchmark",
                                        commit=native_git("rev-parse", "HEAD").strip()))
        self.flush_registry()
        return root

    def test_registered_local_git_clone_permits_only_identity_and_status_reads(self):
        self.save_registry('''
import subprocess
native = root / "data/benchmark"
assert (native / ".git/shallow").is_file()
assert (native / "asset.txt").read_text() == "native source\\n"
assert (native / "internal-link").is_symlink()
assert (native / "internal-link").read_text() == "native source\\n"
subprocess.run(["git", "-C", str(native), "rev-parse", "HEAD"], check=True, capture_output=True)
assert not subprocess.run(["git", "-C", str(native), "status", "--porcelain"], check=True, capture_output=True).stdout
''')
        self.native_fixture()
        result = self.run_review()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(len(result["child"]["read_only_git_calls"]), 2)
        self.assertEqual(result["child"]["process_attempts"], 0)
        self.assertTrue(result["staged_inputs_preserved"])
        self.assert_cleanup(result)

    def test_registered_git_input_does_not_enable_mutating_subcommands(self):
        self.save_registry('''
import subprocess
try:
    subprocess.run(["git", "-C", str(root / "data/benchmark"), "reset", "--hard"])
except RuntimeError:
    pass
''')
        self.native_fixture()
        result = self.run_review()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["child"]["process_attempts"], 1)
        self.assert_cleanup(result)

    def test_benchmark_symlink_cannot_escape_its_registered_checkout(self):
        root = self.native_fixture()
        (root / "internal-link").unlink()
        (root / "internal-link").symlink_to("../../artifacts/result.json")
        subprocess.run(["git", "-C", str(root), "add", "internal-link"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "Escaping link"], check=True, capture_output=True)
        self.entry["inputs"][-1]["commit"] = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        self.flush_registry()
        with self.assertRaisesRegex(ReproductionError, "symlink escapes"):
            self.run_review()
        self.assertFalse(self.output.exists())

    def test_historical_neighbor_script_import_uses_fixed_worktree(self):
        self.write("scripts/neighbor.py", 'VALUE = "old-neighbor"\n')
        self.git("add", "scripts/neighbor.py")
        self.save_registry('\nfrom neighbor import VALUE\nassert VALUE == "old-neighbor"\n')
        self.write("scripts/neighbor.py", 'VALUE = "changed-main"\n')
        result = self.run_review()
        self.assertEqual(result["status"], "passed", result)
        self.assertIn("changed-main", (self.repo / "scripts/neighbor.py").read_text())
        self.assert_cleanup(result)

    def test_wrong_editable_import_path_is_rejected(self):
        wrong = self.base / "other"
        (wrong / "src/agent_memory_eval").mkdir(parents=True)
        (wrong / "src/agent_memory_eval/__init__.py").write_text('"""Wrong checkout."""\n')
        result = self.run_review(environment_factory=lambda w, o, e: self.fixture_environment(w, o, e, wrong))
        self.assertEqual(result["status"], "failed")
        self.assertIn("imports escaped", result["child"]["error"])
        self.assert_cleanup(result)

    def test_environment_failure_leaves_receipt_and_cleans_worktree(self):
        def unavailable(worktree, output, environment):
            raise ReproductionError("Offline dependency cache is incomplete")
        result = self.run_review(environment_factory=unavailable)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Offline dependency", result["error"])
        self.assertTrue((self.output / "receipt.json").is_file())
        self.assert_cleanup(result)

    def test_timeout_preserves_originals_and_cleans_worktree(self):
        self.save_registry("\nimport time\ntime.sleep(30)\n")
        result = self.run_review(timeout_seconds=0.05)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "TimeoutExpired")
        self.assertTrue(result["source_inputs_preserved"])
        self.assert_cleanup(result)

    def test_explicit_keep_retains_independent_input_copies(self):
        result = self.run_review(keep_worktree=True)
        self.addCleanup(lambda: self.git("worktree", "remove", "--force", str(self.output / "checkout")))
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["cleanup"]["status"], "retained")
        source = self.repo / "runs/fixed/blob.json"
        copy = self.output / "checkout/runs/fixed/blob.json"
        self.assertEqual(source.read_bytes(), copy.read_bytes())
        self.assertFalse(source.samefile(copy))


if __name__ == "__main__":
    unittest.main()
