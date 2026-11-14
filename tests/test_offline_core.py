"""Real local writes/dispatch/requests against synthetic state; no provider calls."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.adapters.fake import FakeBenchmark, SyntheticTask
from agent_memory_eval.analysis.offline import summarize
from agent_memory_eval.contracts.core import (ContractError, MemoryFault, MemoryPolicy, RunContext,
                                             canonical, digest, new_id, read_json)
from agent_memory_eval.contracts.validation import inspect_journal
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.models.fake import FakeWorker, tool_call
from agent_memory_eval.recording.journal import Journal, read_experiment
from agent_memory_eval.recording.provenance import OfflineGateway, SourceRegistry, json_string_ranges
from agent_memory_eval.runner.offline import OfflineRunner, tool_schemas
from agent_memory_eval.runner.router import ToolRouter


class OfflineCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root, "synthetic-test")
        self.addCleanup(lambda: self.journal.close())
        self.memory = MemoryService(self.journal)
        self.policy = MemoryPolicy(True, "during_and_post", True, page_size=2)
        self.ctx, self.bank = self.start()

    def start(self, policy=None, parent=None, ctx=None):
        policy = policy or self.policy
        ctx = ctx or RunContext("synthetic-test", new_id("r"), new_id("s"), "test", "collection", "solve")
        bank = self.memory.create_bank(ctx.run_id, policy, parent)
        task = SyntheticTask("t", "배송지", "동쪽").public_message()
        self.journal.append([ctx.event("run.started", dict(bank_id=bank, start_snapshot_id=parent,
            fresh_session=True, source_run_ids=[], task_ref=self.journal.blobs.put(task)))])
        return ctx, bank

    def op(self, name, args, *, ctx=None, bank=None, operation=None, call_id=None):
        return self.memory.execute(ctx or self.ctx, bank or self.bank, operation or new_id("op"),
                                   name, args, call_id=call_id or new_id("call"))

    def store(self, body="현재 조건을 확인한다.", **kwargs):
        return self.op("memory_store", dict(title="배송지 절차", body=body), **kwargs)

    def reopen(self):
        self.journal.close()
        self.journal = Journal(self.root, "synthetic-test")
        self.memory = MemoryService(self.journal)

    def requests(self, ctx=None):
        ctx = ctx or self.ctx
        registry = SourceRegistry(self.journal, ctx)
        start = self.journal.scan["starts"][ctx.run_id]
        task = registry.add("task_initial", start["event_id"])
        template = registry.template(dict(model="synthetic-offline", system=dict(role="system", content="synthetic_worker"),
                                           tools=tool_schemas(["inspect", "apply", "finish"] + self.policy.tools("solve"))))
        return registry, template, [(template, "/system"), (task, "")]

    def batch(self, calls):
        class ScriptedWorker:
            offline = True
            def respond(self, request):
                return dict(choices=[dict(message=dict(role="assistant", tool_calls=deepcopy(calls)))])
        registry, template, history = self.requests()
        return OfflineGateway(self.journal).call(self.ctx, registry, "worker", template, history,
                                                ScriptedWorker(), max_requests=10)[2]

    def test_commit_contains_stored_head_body_and_result(self):
        result = self.store('한글\n"따옴표"와 이모지 🧠')
        row = self.journal.scan["records"][-1]
        self.assertEqual([e["type"] for e in row["events"]], ["memory.stored", "memory.returned"])
        state = self.journal.blobs.get(row["state_ref"])
        item = result.value["id"]
        self.assertEqual(state["banks"][self.bank]["heads"][item], result.value["version"])
        self.assertEqual(self.journal.blobs.get(row["operation"]["result_ref"]), result.wire)
        self.assertNotIn("head_revision", result.wire["content"])

    def test_infrastructure_retry_replays_but_fresh_call_is_new_action(self):
        first = self.store(operation="op_retry", call_id="call_retry")
        count = len(self.journal.scan["records"])
        self.reopen()
        retry = self.store(operation="op_retry", call_id="call_retry")
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.value, first.value)
        self.assertEqual(len(self.journal.scan["records"]), count)
        second = self.store()
        self.assertNotEqual(second.value["id"], first.value["id"])
        with self.assertRaisesRegex(ContractError, "different request"):
            self.store("別の内容", operation="op_retry", call_id="call_retry")

    def test_compare_and_swap_preserves_old_version_and_logical_quota(self):
        policy = MemoryPolicy(True, "during_task", True, 2, max_items=1)
        ctx, bank = self.start(policy)
        first = self.store(ctx=ctx, bank=bank)
        original = deepcopy(self.journal.state["versions"])
        updated = self.op("memory_store", dict(title="更新", body="new", id=first.value["id"],
            expected_version=first.value["version"]), ctx=ctx, bank=bank)
        self.assertEqual(updated.value["status"], "ok")
        self.assertTrue(original.keys() <= self.journal.state["versions"].keys())
        stale = self.op("memory_store", dict(title="bad", body="bad", id=first.value["id"],
            expected_version=first.value["version"]), ctx=ctx, bank=bank)
        self.assertEqual(stale.value["error"]["code"], "VERSION_CONFLICT")
        failed = self.store(ctx=ctx, bank=bank)
        self.assertEqual(failed.value["error"]["code"], "QUOTA_EXCEEDED")
        self.assertEqual(len(self.journal.state["banks"][bank]["heads"]), 1)

    def test_payload_and_return_quota_failures_leave_no_partial_state_or_charge(self):
        for settings in (dict(max_payload_bytes=1), dict(max_return_bytes=0)):
            with self.subTest(settings=settings):
                ctx, bank = self.start(MemoryPolicy(True, "during_task", True, 2, **settings))
                original = deepcopy(self.journal.state)
                result = self.store(ctx=ctx, bank=bank)
                self.assertEqual(result.value["error"]["code"], "QUOTA_EXCEEDED")
                self.assertEqual(self.journal.state["banks"][bank], original["banks"][bank])
                self.assertEqual(self.journal.state["return_bytes"], original["return_bytes"])

    def test_snapshot_fork_and_foreign_ids_do_not_share_writable_heads(self):
        first = self.store()
        snapshot = self.memory.freeze(self.bank)
        ctx, clone = self.start(parent=snapshot)
        own = self.store("clone only", ctx=ctx, bank=clone)
        self.assertNotIn(own.value["id"], self.journal.state["banks"][self.bank]["heads"])
        denied = self.op("memory_get", dict(id=first.value["id"]), ctx=ctx, bank=self.bank)
        self.assertEqual(denied.value["error"]["code"], "ACCESS_DENIED")
        guessed = self.op("memory_get", dict(id="../../grader.json"), ctx=ctx, bank=clone)
        self.assertEqual(guessed.value["error"]["code"], "NOT_FOUND")
        self.assertNotIn(own.value["id"], self.journal.state["snapshots"][snapshot]["heads"])

    def test_frozen_read_view_hides_own_writes_but_live_view_reads_them(self):
        first = self.store()
        snapshot = self.memory.freeze(self.bank)
        frozen = MemoryPolicy(True, "during_task", False, 2)
        ctx, bank = self.start(frozen, snapshot)
        later = self.store(ctx=ctx, bank=bank)
        listing = self.op("memory_list", {}, ctx=ctx, bank=bank)
        self.assertEqual([item["id"] for item in listing.value["items"]], [first.value["id"]])
        self.assertEqual(self.op("memory_get", dict(id=later.value["id"]), ctx=ctx, bank=bank).value["status"], "error")
        self.assertEqual(self.op("memory_get", dict(id=first.value["id"])).value["status"], "ok")

    def test_compose_preserves_versions_and_rejects_conflicting_branches(self):
        first = self.store()
        parent = self.memory.freeze(self.bank)
        branches = []
        for body in ("one", "two"):
            ctx, bank = self.start(parent=parent)
            self.op("memory_store", dict(title="new", body=body, id=first.value["id"],
                expected_version=first.value["version"]), ctx=ctx, bank=bank)
            branches.append(self.memory.freeze(bank))
        before = self.journal.path.read_bytes()
        with self.assertRaisesRegex(MemoryFault, "VERSION_CONFLICT"):
            self.memory.compose(branches)
        self.assertEqual(self.journal.path.read_bytes(), before)
        ctx, bank = self.start(parent=parent)
        unchanged = self.memory.freeze(bank)
        combined = self.memory.compose([parent, unchanged])
        self.assertEqual(self.journal.state["snapshots"][combined]["heads"], {first.value["id"]: first.value["version"]})

    def test_opaque_cursor_is_persistent_and_detects_live_revision_change(self):
        values = [self.store() for _ in range(3)]
        listing = self.op("memory_list", {})
        cursor = listing.value["next_cursor"]
        self.assertTrue(cursor.startswith("cursor_"))
        self.assertEqual(set(listing.value["items"][0]), {"id", "title", "version"})
        self.reopen()
        page = self.op("memory_list", dict(cursor=cursor))
        self.assertEqual(page.value["items"][0]["id"], values[2].value["id"])
        self.store()
        stale = self.op("memory_list", dict(cursor=cursor))
        self.assertEqual(stale.value["error"]["code"], "STALE_CURSOR")
        snapshot = self.memory.freeze(self.bank)
        ctx, bank = self.start(MemoryPolicy(True, "during_task", False, 2), snapshot)
        first_page = self.op("memory_list", {}, ctx=ctx, bank=bank)
        self.store(ctx=ctx, bank=bank)
        next_page = self.op("memory_list", dict(cursor=first_page.value["next_cursor"]), ctx=ctx, bank=bank)
        self.assertEqual(next_page.value["status"], "ok")
        self.assertEqual(next_page.value["items"][0]["id"], values[2].value["id"])

    def test_seal_and_phase_policy_are_enforced(self):
        post_only = MemoryPolicy(True, "post_task", False, 2)
        ctx, bank = self.start(post_only)
        self.assertEqual(self.store(ctx=ctx, bank=bank).value["error"]["code"], "POLICY_DENIED")
        self.assertEqual(self.store(ctx=ctx.at("post_task_write"), bank=bank).value["status"], "ok")
        self.memory.seal(bank)
        self.assertEqual(self.store(ctx=ctx.at("post_task_write"), bank=bank).value["error"]["code"], "ACCESS_DENIED")

    def test_crash_before_commit_leaves_only_orphans(self):
        original = deepcopy(self.journal.state)
        self.journal.fault = lambda point: (_ for _ in ()).throw(OSError("injected")) if point == "before_log" else None
        with self.assertRaises(OSError):
            self.store(operation="op_crash", call_id="call_crash")
        with self.assertRaisesRegex(ContractError, "requires recovery"):
            self.store()
        self.reopen()
        self.assertEqual(self.journal.state, original)
        self.assertEqual(self.store(operation="op_crash", call_id="call_crash").value["status"], "ok")

    def test_blob_sync_failure_does_not_publish_a_memory_change(self):
        original = self.journal.path.read_bytes()
        original_state = deepcopy(self.journal.state)
        with patch("agent_memory_eval.recording.journal.os.fsync", side_effect=OSError("injected sync")):
            with self.assertRaises(OSError):
                self.store()
        self.assertEqual(self.journal.path.read_bytes(), original)
        self.assertEqual(self.journal.state, original_state)
        self.reopen()
        self.assertEqual(self.journal.state["banks"][self.bank]["heads"], {})

    def test_crash_after_commit_recovers_result_without_second_charge(self):
        self.journal.fault = lambda point: (_ for _ in ()).throw(OSError("injected")) if point == "after_log" else None
        with self.assertRaises(OSError):
            self.store(operation="op_crash", call_id="call_crash")
        self.reopen()
        before = self.journal.path.read_bytes()
        usage = deepcopy(self.journal.state["return_bytes"])
        result = self.store(operation="op_crash", call_id="call_crash")
        self.assertTrue(result.replayed)
        self.assertEqual(self.journal.path.read_bytes(), before)
        self.assertEqual(self.journal.state["return_bytes"], usage)

    def test_tail_is_preserved_and_complete_corruption_is_fatal(self):
        self.journal.close()
        original = self.journal.path.read_bytes()
        tail = original + b'{"unfinished"'
        self.journal.path.write_bytes(tail)
        with self.assertRaisesRegex(ContractError, "tail preserved"):
            Journal(self.root, "synthetic-test")
        self.assertEqual(self.journal.path.read_bytes(), tail)
        self.assertGreater(inspect_journal(tail)["tail_bytes"], 0)
        self.journal.path.write_bytes(original + b"{}\n")
        with self.assertRaisesRegex(ValueError, "Corrupt complete"):
            Journal(self.root, "synthetic-test")

    def test_exclusive_writer_and_symlink_boundaries(self):
        with self.assertRaises(BlockingIOError):
            Journal(self.root, "synthetic-test")
        outside = self.root / "outside.json"
        outside.write_bytes(b"{}")
        (self.root / "link.json").symlink_to(outside)
        with self.assertRaisesRegex(ContractError, "Symlinks"):
            self.journal.blobs.get(dict(path="link.json", sha256=digest(b"{}"), media_type="application/json"))

    def test_views_can_fail_and_be_rebuilt_without_reexecuting(self):
        self.store(operation="op_view", call_id="call_view")
        before = self.journal.path.read_bytes()
        with patch.object(self.journal.blobs, "write_view", side_effect=OSError("injected export")):
            with self.assertRaises(OSError):
                self.journal.export_views()
        self.reopen()
        self.assertTrue(self.store(operation="op_view", call_id="call_view").replayed)
        self.journal.export_views()
        first = (self.root / "views/events.jsonl").read_bytes()
        self.journal.export_views()
        self.assertEqual((self.root / "views/events.jsonl").read_bytes(), first)
        self.assertEqual(self.journal.path.read_bytes(), before)

    def test_mixed_dispatch_preserves_state_seen_between_business_calls(self):
        benchmark = FakeBenchmark(SyntheticTask("t", "배송지", "동쪽"))
        router = ToolRouter(self.journal, self.memory, benchmark, self.bank, self.policy)
        calls = [tool_call("inspect", {}), tool_call("memory_store", dict(title="t", body="b")),
                 tool_call("apply", dict(value="동쪽"))]
        request_id = self.batch(calls)
        results = router.dispatch(self.ctx, request_id, calls)
        self.assertEqual([r[0]["tool_call_id"] for r in results], [c["id"] for c in calls])
        effects = [e["payload"].get("action", e["type"]) for e in self.journal.events.values()
                   if e["type"] == "memory.stored" or "action" in e["payload"]]
        self.assertEqual(effects, ["inspect", "memory.stored", "apply"])
        self.assertEqual(benchmark.steps, 2)
        self.assertEqual(benchmark.user_turns, 1)
        with self.assertRaisesRegex(ContractError, "automatically replayed"):
            router.dispatch(self.ctx, request_id, calls)
        altered = deepcopy(calls)
        altered[-1]["function"]["arguments"] = canonical(dict(value="wrong")).decode()
        with self.assertRaisesRegex(ContractError, "recorded response"):
            router.dispatch(self.ctx, request_id, altered)

    def test_stop_batch_preflight_has_no_business_or_memory_side_effect(self):
        benchmark = FakeBenchmark(SyntheticTask("t", "配送", "east"))
        router = ToolRouter(self.journal, self.memory, benchmark, self.bank, self.policy)
        calls = [tool_call("memory_store", dict(title="t", body="b")), tool_call("finish", {})]
        results = router.dispatch(self.ctx, self.batch(calls), calls)
        self.assertEqual(len(results), 2)
        self.assertEqual(benchmark.steps, 0)
        self.assertFalse(benchmark.ended)
        self.assertEqual(self.journal.state["banks"][self.bank]["heads"], {})
        self.assertTrue(all(r[1] == "policy_result" for r in results))

    def test_request_manifest_blocks_wrapper_changes_before_transport(self):
        registry, template, history = self.requests()
        worker = FakeWorker()
        gateway = OfflineGateway(self.journal)
        transforms = [lambda r: dict(r, previous_response_id="old-session"),
                      lambda r: dict(r, tools=r["tools"] + [{"hint": "private"}])]
        for transform in transforms:
            with self.assertRaisesRegex(ContractError, "approved manifest"):
                gateway.call(self.ctx, registry, "worker", template, history, worker, max_requests=5, transform=transform)
        self.assertEqual(worker.requests, [])
        self.assertFalse(any(e["type"] == "model.request_started" for e in self.journal.events.values()))

    def test_registry_rejects_other_run_and_other_session(self):
        registry, template, history = self.requests()
        other_ctx, other_bank = self.start()
        with self.assertRaisesRegex(ContractError, "this run"):
            registry.add("task_initial", self.journal.scan["starts"][other_ctx.run_id]["event_id"])
        with self.assertRaisesRegex(ContractError, "cross sessions"):
            registry.prepare(other_ctx, "worker", template, history)

    def test_self_authored_equal_strings_have_distinct_ranges_without_fake_return(self):
        registry, template, history = self.requests()
        class StoreWorker:
            offline = True
            def respond(self, request):
                return dict(choices=[dict(message=dict(role="assistant", tool_calls=[
                    tool_call("memory_store", dict(title="같은 말 🧠", body="같은 말 🧠"))]))])
        gateway = OfflineGateway(self.journal)
        message, source, request_id = gateway.call(self.ctx, registry, "worker", template, history, StoreWorker(), max_requests=5)
        history.append((source, ""))
        gateway.call(self.ctx, registry, "worker", template, history, FakeWorker(), max_requests=5, cancel=True)
        spans = [e["payload"] for e in self.journal.events.values() if e["type"] == "memory.input_included"]
        self.assertEqual(len(spans), 2)
        self.assertNotEqual(spans[0]["char_start"], spans[1]["char_start"])
        self.assertTrue(all(p["source"]["kind"] == "self_authored" and p["item_id"] is None for p in spans))
        self.assertFalse(any(e["type"] == "memory.returned" for e in self.journal.events.values()))

    def test_returned_is_distinct_from_input_and_cancel_is_not_delivery(self):
        result = self.store()
        returned = self.op("memory_get", dict(id=result.value["id"]))
        self.assertFalse(any(e["type"] == "memory.input_included" for e in self.journal.events.values()))
        registry, template, history = self.requests()
        history.append((registry.add("memory_return", returned.returned_event_id), ""))
        worker = FakeWorker()
        OfflineGateway(self.journal).call(self.ctx, registry, "worker", template, history, worker, max_requests=3, cancel=True)
        finish = next(e for e in reversed(list(self.journal.events.values())) if e["type"] == "model.request_finished")
        self.assertEqual(finish["payload"]["delivery_status"], "not_submitted")
        self.assertEqual(worker.requests, [])
        self.assertEqual(sum(e["type"] == "memory.input_included" for e in self.journal.events.values()), 2)

    def test_timeout_and_usage_absence_remain_unknown_values(self):
        class TimeoutWorker:
            offline = True
            def respond(self, request):
                raise TimeoutError("injected")
        registry, template, history = self.requests()
        with self.assertRaises(TimeoutError):
            OfflineGateway(self.journal).call(self.ctx, registry, "worker", template, history, TimeoutWorker(), max_requests=3)
        finish = list(self.journal.events.values())[-1]["payload"]
        self.assertEqual(finish["delivery_status"], "unknown")
        self.assertIsNone(finish["cost_usd"])
        self.assertIsNone(finish["usage"])

    def test_public_prefix_has_no_future_observation_and_survives_later_events(self):
        task_ref = self.journal.blobs.put(dict(role="user", content="first public"))
        self.journal.append([self.ctx.event("business.transition", dict(public_message_ref=task_ref))])
        first = self.journal.public_prefix(self.ctx)
        ref = self.journal.blobs.put(dict(role="user", content="later public"))
        self.journal.append([self.ctx.event("business.transition", dict(public_message_ref=ref))])
        second = self.journal.public_prefix(self.ctx)
        from agent_memory_eval.contracts.validation import read_public_prefix
        old = read_public_prefix(self.root, first, self.journal.events, len(self.journal.events) + 1)
        self.assertEqual(len(old["messages"]), 1)
        self.assertEqual(second["entry_count"], 2)


class OfflineFlowTests(unittest.TestCase):
    def test_two_sources_own_mixed_and_no_memory_recompute_without_leakage(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with Journal(root, "synthetic-flow") as journal:
                runner = OfflineRunner(journal)
                runs = runner.two_pass()
                before = (root / "log.jsonl").read_bytes()
                summary = summarize(root)
                self.assertEqual(summary["run_count"], 8)
                self.assertEqual(summary["scheduled_runs"], 8)
                self.assertFalse(journal.scan["records"][0]["events"])
                initial_state = journal.blobs.get(journal.scan["records"][0]["state_ref"])
                self.assertEqual(len(initial_state["schedule"]), 8)
                self.assertEqual(len(summary["synthetic_pairs"]), 4)
                self.assertTrue(all(v is None for v in summary["official_outcomes"].values()))
                self.assertTrue(all(summary["synthetic_outcomes"].values()))
                self.assertGreater(summary["retrieved_spans"], 0)
                self.assertGreater(summary["self_authored_spans"], 0)
                self.assertEqual(summary["incomplete_runs"], [])
                secrets = [env.private_marker for env in runner.environments.values()]
                sessions = {e["session_id"] for e in journal.events.values() if e["type"] == "run.started"}
                self.assertEqual(len(sessions), 8)
                self.assertEqual(len({id(env.state) for env in runner.environments.values()}), 8)
                for run, assignment in journal.state["assignments"].items():
                    for request in runner.workers[run].requests:
                        text = canonical(request).decode()
                        self.assertTrue(all(secret not in text for secret in secrets))
                        self.assertNotIn("source_run_ids", text)
                        self.assertNotIn("head_revision", text)
                        if assignment["mode"] == "no_memory":
                            self.assertTrue(all(not t["function"]["name"].startswith("memory_") for t in request["tools"]))
                    self.assertEqual(runner.environments[run].steps, 3)
                    self.assertEqual(runner.environments[run].user_turns, 1)
                mixed = journal.state["snapshots"][runs["mixed_snapshot"]]
                self.assertEqual(len(mixed["heads"]), 2)
                for run in runs["probes"]:
                    bank = run["bank_id"]
                    if bank is not None:
                        parent = journal.state["banks"][bank]["parent_snapshot"]
                        self.assertEqual(journal.state["snapshots"][parent]["heads"], journal.state["banks"][bank]["start_heads"])
                journal.export_views()
                self.assertEqual((root / "log.jsonl").read_bytes(), before)
            self.assertEqual(summarize(root), summary)

    def test_all_write_timings_are_behaviors_and_post_write_preserves_result(self):
        for timing, expected in (("none", 0), ("during_task", 1), ("post_task", 1), ("during_and_post", 2)):
            with self.subTest(timing=timing), TemporaryDirectory() as folder:
                with Journal(folder, "synthetic-timing") as journal:
                    runner = OfflineRunner(journal)
                    runner.run(SyntheticTask("t", "配送", "east"), MemoryPolicy(True, timing, False, 3),
                               stage="collection", mode=timing, max_requests=12)
                    stores = [e for e in journal.events.values() if e["type"] == "memory.stored"]
                    self.assertEqual(len(stores), expected)
                    summary = summarize(folder)
                    self.assertEqual(list(summary["synthetic_outcomes"].values()), [True])
                    grade_seq = next(e["event_seq"] for e in journal.events.values() if e["type"] == "outcome.scored")
                    self.assertTrue(all(e["event_seq"] > grade_seq for e in stores if e["phase"] == "post_task_write"))

    def test_request_limit_keeps_grade_and_grader_failure_stays_missing(self):
        for fail_grade in (False, True):
            with self.subTest(grader_error=fail_grade), TemporaryDirectory() as folder:
                with Journal(folder, "synthetic-limit") as journal:
                    runner = OfflineRunner(journal)
                    result = runner.run(SyntheticTask("t", "配送", "east"),
                        MemoryPolicy(False, "none", False, 2, read_mode="none"), stage="collection", mode="limit",
                        max_requests=1, grader_error=fail_grade)
                    summary = summarize(folder)
                    self.assertEqual(summary["terminations"][result["run_id"]], "request_limit")
                    self.assertIsNone(summary["official_outcomes"][result["run_id"]])
                    if fail_grade:
                        self.assertNotIn(result["run_id"], summary["synthetic_outcomes"])
                    else:
                        self.assertFalse(summary["synthetic_outcomes"][result["run_id"]])
                    self.assertEqual(summary["incomplete_runs"], [])


if __name__ == "__main__":
    unittest.main()
