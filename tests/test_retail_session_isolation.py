import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.journal import BlobStore
from scripts.audit_retail_session_isolation import load_tasks, replay_task


@unittest.skipUnless(
    (ROOT / "runs/tau2-retail-sample-expansion-01-r3/completion.json").is_file(),
    "Requires the original retail runs",
)
class RetailSessionIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, groups = load_tasks(ROOT)
        ws, cells, task, refs, settings = next(g for g in groups if g[2]["id"] == "90")
        control = next(
            c for c in cells if c["stage"] == "solve3" and c["arm"] == "no_memory"
        )
        source = next(c for c in cells if c["stage"] == "source")
        past = read_json((ws / source["report"]).read_bytes())["messages"]
        cls.past_worker = next(
            m["content"]
            for m in reversed(past)
            if m["role"] == "assistant" and m.get("content")
        )
        cls.past_customer = next(
            m["content"]
            for m in reversed(past)
            if m["role"] == "user" and m.get("content")
        )
        cls.past_change = next(w for w in source["behavior"]["writes"] if w["success"])
        cls.group = ws, [control], task, refs, settings
        cls.first_requests = {}
        for line in (ws / control["journal"]).read_bytes().splitlines():
            for event in read_json(line)["events"]:
                if event["type"] == "model.request_started":
                    p = event["payload"]
                    cls.first_requests.setdefault(
                        p["actor_role"], p["request_ref"]["sha256"]
                    )

    def test_previous_conversation_detected_for_either_role(self):
        read = BlobStore.get
        for role, content in [
            ("worker", self.past_worker),
            ("user_simulator", self.past_customer),
        ]:

            def insert_previous_turn(
                store, ref, *, target=self.first_requests[role], previous=content
            ):
                value = read(store, ref)
                if ref["sha256"] == target:
                    value["messages"].insert(
                        1, {"role": "assistant", "content": previous}
                    )
                return value

            with (
                self.subTest(role=role),
                patch.object(BlobStore, "get", insert_previous_turn),
                self.assertRaisesRegex(
                    ContractError, role + " request .*differs from clean construction"
                ),
            ):
                replay_task(self.group)

    def test_prior_order_change_detected_before_first_call(self):
        from tau2.runner import build

        clean = build.build_text_orchestrator

        def carry_over_order(*args, **kwargs):
            orch = clean(*args, **kwargs)
            orch.environment.make_tool_call(
                self.past_change["name"], **self.past_change["args"]
            )
            return orch

        with (
            patch.object(build, "build_text_orchestrator", carry_over_order),
            self.assertRaisesRegex(ContractError, "original DB"),
        ):
            replay_task(self.group)
