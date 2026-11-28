import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from agent_memory_eval.contracts.validation import ROOT


class InstalledContractTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("uv"), "Building the regression wheel requires uv")
    def test_wheel_writes_a_new_journal_outside_the_checkout(self):
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            source = temp / "source"
            source.mkdir()
            for name in ("setup.py", "pyproject.toml", "README.md"):
                shutil.copyfile(ROOT / name, source / name)
            for name in ("src", "schemas", "scripts"):
                shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
            subprocess.run(["uv", "build", "--offline", "--wheel", "--out-dir", str(temp / "dist"), str(source)],
                           cwd=temp, check=True, capture_output=True)
            wheel, = (temp / "dist").glob("*.whl")
            installed = temp / "installed"
            with ZipFile(wheel) as archive:
                for path in (source / "schemas").rglob("*.schema.json"):
                    self.assertEqual(archive.read("agent_memory_eval/schemas/" + path.relative_to(source / "schemas").as_posix()),
                                     path.read_bytes())
                archive.extractall(installed)
            probe = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agent_memory_eval.contracts.validation import SCHEMAS, SCHEMA_ROOT, validators
from agent_memory_eval.contracts.core import MemoryPolicy
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.policies.writer_pairs import writer_pairs
from agent_memory_eval.study.adapters import adapter_for
from scripts import run_retail_two_visit
assert Path(run_retail_two_visit.__file__).resolve().is_relative_to(Path(sys.argv[1]))
assert adapter_for("tau2-retail-revisit").execution_mode == "followup"
assert SCHEMAS.resolve().is_relative_to(Path(sys.argv[1]))
conditions, pairs = writer_pairs({"timeline": "Keep events."}, "Record confirmed results.")
assert conditions[1]["id"] == pairs["timeline"]["candidate"]
assert (SCHEMA_ROOT / "prompt-comparison-v3.schema.json").is_file()
with Journal(Path("independent"), "probe", contract_version="1.3") as journal:
    assert MemoryService(journal).create_bank("writer", MemoryPolicy(True, "post_task", False, 1))
print(json.dumps(sorted(validators())))
"""
            keys = json.loads(subprocess.check_output([sys.executable, "-I", "-c", probe, str(installed)], cwd=temp))
            self.assertTrue({"journal-state", "journal-state-v1.2", "journal-state-v1.3"} <= set(keys))

            archive = Path(shutil.copy2(ROOT / "artifacts/analyses/20261125-bfcl-native-memory-control.json", temp))
            expected = json.loads((ROOT / "tests/fixtures/paired-outcomes-v1.json").read_bytes())
            invoke_report = """
import runpy, sys
sys.path.insert(0, sys.argv.pop(1))
runpy.run_module("agent_memory_eval.analysis.outcome_report", run_name="__main__")
"""
            output = subprocess.check_output(
                [sys.executable, "-I", "-c", invoke_report, str(installed), archive.name,
                 "--metric", "bfcl.official_answer", "--reference", "access_control", "--candidate", "read_enabled",
                 "--sha256", expected["archive_sha256"]], cwd=temp)
            self.assertEqual(json.loads(output), expected)
