"""A real small Git checkout prevents freezing dirty or moving source code."""

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.reproduction.freeze import implementation_identity


class ImplementationFreezeTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / "implementation.py").write_text("pass\n")
        (self.root / "uv.lock").write_text("fixture lock\n")
        (self.root / ".gitignore").write_text("outputs/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial fixture")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args]).decode().strip()

    def test_clean_identity_survives_later_changes_and_ignores_output_data(self):
        (self.root / "outputs").mkdir()
        (self.root / "outputs/data.json").write_text("{}")
        result = implementation_identity(self.root)
        self.assertEqual(result["implementation_commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(result["implementation_tree"], self.git("rev-parse", "HEAD^{tree}"))
        (self.root / "implementation.py").write_text("changed\n")
        self.assertEqual(self.git("show", result["implementation_commit"] + ":implementation.py"), "pass")
        with self.assertRaises(ContractError):
            implementation_identity(self.root)

    def test_staged_untracked_and_documentation_changes_are_refused(self):
        for stage in (False, True):
            with self.subTest(staged=stage):
                (self.root / "notes.md").write_text("not committed")
                if stage:
                    self.git("add", "notes.md")
                with self.assertRaisesRegex(ContractError, "before freezing"):
                    implementation_identity(self.root)
                self.git("reset", "-q")
                (self.root / "notes.md").unlink()

    def test_subdirectory_and_nonrepository_are_refused(self):
        child = self.root / "child"
        child.mkdir()
        with self.assertRaises(ContractError):
            implementation_identity(child)
        with TemporaryDirectory() as elsewhere, self.assertRaises(ContractError):
            implementation_identity(elsewhere)


if __name__ == "__main__":
    unittest.main()
