from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.adapters.bfcl_memory import RecSumMemory
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, RunContext
from agent_memory_eval.contracts.validation import validate_public_return
from agent_memory_eval.memory.router import ToolRouter
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner.bfcl_memory_check import _open, _trial


class LocalRecSum:
    def __init__(self):
        self.memory = ""

    def _load_scenario(self, config):
        self.saved = config["model_result_dir"] / "checkpoint"
        if self.saved.exists():
            self.memory = self.saved.read_text()

    def _flush_memory_to_local_file(self):
        self.saved.parent.mkdir(exist_ok=True)
        self.saved.write_text(self.memory)

    def memory_append(self, text):
        self.memory += text
        return {"status": "Memory appended."}

    def memory_retrieve(self):
        return {"memory_content": self.memory} if self.memory else {"error": "Memory is empty."}


class NativeMemoryTests(unittest.TestCase):
    def test_version_bound_delivery_and_no_read_permission_use_the_same_first_question(self):
        with TemporaryDirectory() as folder:
            tools = {name: {"type": "function", "function": {"name": name}} for name in RecSumMemory.tools}
            report = _trial(Path(folder) / "case", LocalRecSum, lambda a, b: {"valid": a in b},
                {"role": "user", "content": "My name is Fixture."}, {"role": "user", "content": "What is my name?"},
                tools, ["Fixture"])
        self.assertEqual(report["version_bound_input_spans"], {"read_enabled": 1, "access_control": 0})
        self.assertEqual(report["request_audit"]["status"], "passed")
        self.assertTrue(report["equal_first_reader_wire"])
        self.assertEqual(report["official_success"], {"read_enabled": True, "access_control": False})

    def test_return_quota_cannot_mutate_native_or_framework_state(self):
        with TemporaryDirectory() as folder, Journal(Path(folder) / "j", "quota", contract_version="1.3") as j:
            m, native = MemoryService(j), RecSumMemory(LocalRecSum())
            policy = MemoryPolicy(True, "during_task", True, 1, max_return_bytes=1)
            ctx = RunContext("quota", "r", "s", "native", "collection", "solve")
            bank = m.create_bank("r", policy)
            _open(j, ctx, {"role": "user", "content": "A"}, [], bank)
            result = m.execute(ctx, bank, "op", "memory_append", {"text": "secret"}, call_id="call", native=native)
            self.assertEqual(result.value["error"]["code"], "QUOTA_EXCEEDED")
            self.assertEqual((native.backend.memory, j.state["banks"][bank]["heads"]), ("", {}))
            replay = m.execute(ctx, bank, "op", "memory_append", {"text": "secret"}, call_id="call", native=native)
            self.assertTrue(replay.replayed)

    def test_retry_finishes_an_interrupted_native_write_without_creating_a_new_version(self):
        with TemporaryDirectory() as folder, Journal(Path(folder) / "j", "retry", contract_version="1.3") as j:
            m, native = MemoryService(j), RecSumMemory(LocalRecSum())
            ctx = RunContext("retry", "r", "s", "native", "collection", "solve")
            bank = m.create_bank("r", MemoryPolicy(True, "during_task", True, 1))
            _open(j, ctx, {"role": "user", "content": "A"}, [], bank)
            def write():
                return m.execute(ctx, bank, "op", "memory_append", {"text": "a fact"}, call_id="c", native=native)
            with patch.object(native, "commit", side_effect=RuntimeError("interrupted")), self.assertRaises(RuntimeError):
                write()
            self.assertEqual(native.backend.memory, "")
            self.assertTrue(write().replayed)
            self.assertEqual(native.backend.memory, "a fact")
            self.assertEqual(len(j.state["versions"]), 1)
            native.backend.memory = "a different source"
            with self.assertRaisesRegex(ContractError, "unrelated"):
                write()

    def test_repeated_append_versions_and_frozen_read_view(self):
        with TemporaryDirectory() as folder, Journal(Path(folder) / "j", "versions", contract_version="1.3") as j:
            m, native = MemoryService(j), RecSumMemory(LocalRecSum())
            p = MemoryPolicy(True, "during_task", False, 1)
            ctx = RunContext("versions", "r", "s", "native", "collection", "solve")
            bank = m.create_bank("r", p)
            _open(j, ctx, {"role": "user", "content": "A"}, [], bank)
            for i, text in enumerate(("A", " B")):
                m.execute(ctx, bank, str(i), "memory_append", {"text": text}, call_id=str(i), native=native)
            self.assertEqual(native.backend.memory, "A B")
            self.assertEqual(len(j.state["versions"]), 2)
            self.assertEqual(len(j.state["banks"][bank]["heads"]), 1)
            read = m.execute(ctx, bank, "read", "memory_retrieve", {}, call_id="read", native=native)
            self.assertIn("error", read.value)
            fake = deepcopy(j.events[read.returned_event_id]["payload"])
            fake["status"] = "ok"
            with self.assertRaises(ValueError):
                validate_public_return(read.wire, fake)
            error = deepcopy(j.events[read.returned_event_id]["payload"])
            with self.assertRaises(ValueError):
                validate_public_return(dict(read.wire, content='{"error":"empty","id":"internal"}'), error)
            with self.assertRaises(ValueError):
                validate_public_return(read.wire, dict(error, operation="create"))
        with TemporaryDirectory() as folder, Journal(Path(folder) / "old", "old") as old:
            with self.assertRaises(ContractError):
                ToolRouter(old, None, None, None, p, native_memory=native)
