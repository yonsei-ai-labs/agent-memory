"""Single-response controls, narrow argument screens and fake-HTTP lifecycle tests."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from jsonschema import Draft202012Validator

from agent_memory_eval.analysis.decision_replay import primary_train_ids, screen_reply
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner import decision_replay as replay
from test_openrouter import MODEL, Reply, response


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "protocols/drafts/tau2-decision-replay-v1.json"
RAW = ROOT / "runs/tau2-baseline-luna-deepseek1017-train-01/log.jsonl"


def ready():
    """Fake-HTTP test settings only; no live cost authorization."""
    profile = read_json(PROFILE.read_bytes())
    profile.update(status="ready", repetitions=1, schedule_seed=7)
    profile["guards"] = dict(max_model_requests=24, max_reported_cost_usd=1, max_seconds=600)
    return profile


def tool(name, arguments):
    return dict(id="call-test", type="function", function=dict(name=name, arguments=json.dumps(arguments)))


class ReplayControlsTests(unittest.TestCase):
    def test_schema_and_draft_block_metadata_and_paid_calls(self):
        Draft202012Validator.check_schema(read_json((ROOT / "schemas/decision-replay.schema.json").read_bytes()))
        plan = replay.compile_replay(read_json(PROFILE.read_bytes()))
        self.assertFalse(plan["ready"])
        with patch.object(replay, "model_catalog") as catalog:
            with self.assertRaisesRegex(ContractError, "not ready"):
                replay.prepare_replay(read_json(PROFILE.read_bytes()))
            catalog.assert_not_called()

    def test_original_is_exact_and_other_arms_change_only_declared_fields(self):
        original = dict(model="model", provider=dict(only=["openai"], allow_fallbacks=False, require_parameters=True),
            messages=[dict(role="system", content="policy"), dict(role="user", content="question")],
            reasoning=dict(effort="low"), seed=1, max_completion_tokens=2048, stream=False, tools=[dict(function=dict(name="book_reservation"))])
        before = canonical(original)
        self.assertEqual(canonical(replay.make_request(original, "original", "memo")), before)
        medium = replay.make_request(original, "reasoning_medium", "memo")
        self.assertEqual(medium.pop("reasoning"), dict(effort="medium"))
        expected = deepcopy(original); expected.pop("reasoning")
        self.assertEqual(medium, expected)
        memo = replay.make_request(original, "memo_researcher", "memo")
        self.assertEqual(memo["messages"][0]["content"], "policy" + replay.MEMO_PREFIX + "memo")
        memo["messages"][0]["content"] = "policy"
        self.assertEqual(memo, original)
        self.assertEqual(canonical(original), before)

    def test_blocks_cover_all_arms_and_do_not_depend_on_mapping_order(self):
        p = ready()
        left = replay.allocation_rows(p, ["b", "a"])
        right = replay.allocation_rows(p, ["a", "b"])
        self.assertEqual(left, right)
        for start in range(0, len(left), 3):
            block = left[start:start+3]
            self.assertEqual(len({r["case_id"] for r in block}), 1)
            self.assertEqual({r["condition"] for r in block}, set(p["conditions"]))

    def test_unbounded_settings_and_mislabeled_worker_memos_are_rejected(self):
        for field, value in [("max_reported_cost_usd", float("inf")), ("max_model_requests", None), ("max_seconds", 0)]:
            p = ready();p["guards"][field] = value
            with self.subTest(field=field), self.assertRaises(ContractError):
                replay.compile_replay(p, require_ready=True)
        p = ready();memo = read_json((ROOT/p["memo_ref"]["path"]).read_bytes())
        memo["author_kind"] = "worker_generated"
        text = canonical(memo).decode();p["memo_ref"]["sha256"] = digest(text.encode())
        with self.assertRaisesRegex(ContractError, "distinct arm"):
            replay.compile_replay(p, memo_text=text)

    def test_primary_scope_retains_11_and_14_and_excludes_whole_7_and_39(self):
        lock = read_json((ROOT / "artifacts/pilots/20261115-tau2-baseline-screening-protocol.json").read_bytes())
        selected = primary_train_ids(lock["train_task_ids"])
        self.assertEqual(len(selected), 28)
        self.assertTrue({"11", "14"} <= set(selected))
        self.assertFalse({"7", "39"} & set(selected))

    def test_public_review_reply_omits_provider_reasoning(self):
        raw=dict(role="assistant",content="Please confirm",tool_calls=[],reasoning="private",reasoning_details=[dict(data="opaque")])
        self.assertEqual(replay.public_reply(raw),dict(role="assistant",content="Please confirm",tool_calls=[]))


class ArgumentScreenTests(unittest.TestCase):
    def test_no_call_or_extra_read_is_not_a_success(self):
        for message in [dict(role="assistant", content="How many bags do you need?"),
                dict(role="assistant", tool_calls=[tool("get_reservation_details", dict(reservation_id="x"))])]:
            result = screen_reply("14", message, {})
            self.assertEqual(result["status"], "no_target_write")
            self.assertEqual(result["target_write_calls"], 0)

    def test_unrequested_bags_certificate_limit_and_malformed_args(self):
        self.assertEqual(screen_reply("14", dict(tool_calls=[tool("book_reservation", dict(total_baggages=6))]), {})["status"], "violation_detected")
        self.assertEqual(screen_reply("14", dict(tool_calls=[tool("book_reservation", dict(total_baggages=0))]), {})["status"], "argument_check_passed")
        db = dict(users=dict(u=dict(payment_methods={str(i):dict(source="certificate") for i in range(3)})))
        args = dict(user_id="u", payment_methods=[dict(payment_id=str(i)) for i in range(3)])
        self.assertEqual(screen_reply("23", dict(tool_calls=[tool("book_reservation", args)]), db)["status"], "violation_detected")
        self.assertEqual(screen_reply("23", dict(tool_calls=[tool("book_reservation", {})]), db)["status"], "unparseable_or_unknown_arguments")

    def test_time_order_flags_return_before_outbound_arrival(self):
        db = dict(flights=dict(out=dict(scheduled_departure_time_est="04:00:00", scheduled_arrival_time_est="06:00:00"),
            back=dict(scheduled_departure_time_est="04:00:00", scheduled_arrival_time_est="08:00:00")))
        args = dict(flights=[dict(flight_number="out", date="2024-05-20"), dict(flight_number="back", date="2024-05-20")])
        self.assertEqual(screen_reply("21", dict(tool_calls=[tool("update_reservation_flights", args)]), db)["status"], "violation_detected")

    def test_text_snapshots_remain_manual_even_if_a_new_reply_uses_a_tool(self):
        for task in ["17", "20", "39", "42"]:
            self.assertEqual(screen_reply(task, dict(tool_calls=[tool("cancel_reservation", {})]), {})["status"], "manual_review_required")


@unittest.skipUnless(RAW.is_file(), "Original raw run is required for integration checks")
class ReplayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, OPENROUTER_API_KEY="fake-replay-key", OPENAI_API_KEY="fake-unused-key")
        self.environment.start();self.addCleanup(self.environment.stop)

    def prepared(self, profile):
        model = deepcopy(MODEL)
        model["selected_endpoint"] = dict(provider_name="OpenAI", tag="openai")
        return dict(plan=replay.preflight_replay(profile, require_ready=True), model=model)

    def test_real_snapshots_preserve_all_eight_cases_and_only_three_are_argument_screens(self):
        plan = self.prepared(ready())["plan"]
        db = read_json(plan["db_text"].encode());statuses=[]
        for case in plan["cases"].values():
            old = case["original_public_reply"]
            message = dict(role="assistant", content=old.get("content"), tool_calls=[tool(c["name"], c["arguments"]) for c in old.get("tool_calls") or []])
            statuses.append(screen_reply(case["task_id"], message, db)["status"])
        self.assertEqual(statuses.count("violation_detected"), 3)
        self.assertEqual(statuses.count("manual_review_required"), 5)
        notes = canonical(plan["memos"]["notes"]).decode()
        for forbidden in ["708", "1628", "5244", "1786", "06:00", "MSJ4OA", "PUNERT"]:
            self.assertNotIn(forbidden, notes)

    def test_fake_http_roundtrip_and_portable_audit_do_not_execute_tools_or_rewrite_source(self):
        profile = ready();prepared = self.prepared(profile);seen=[];before=digest(RAW.read_bytes())
        def opener(request, timeout):
            seen.append(read_json(request.data));reply=response();reply["id"] = f"test-{len(seen)}"
            return Reply(reply)
        with TemporaryDirectory() as folder:
            with Journal(folder, "test-replay") as journal:
                summary = replay.run_replay(journal, profile, prepared, opener=opener)
                self.assertTrue(all(e["payload"]["official_score"] is None for e in journal.events.values() if e["type"]=="outcome.scored"))
            self.assertEqual(summary["counts"],dict(response=24))
            self.assertFalse(summary["business_tool_execution"])
            self.assertEqual(len(seen),24)
            load = replay.load_ref
            def inline_only(ref, text=None):
                self.assertIsNotNone(text, "Audit tried to reread an external source")
                return load(ref, text)
            with patch.object(replay, "load_ref", side_effect=inline_only), patch.object(replay, "model_catalog", side_effect=AssertionError("HTTP")):
                self.assertEqual(replay.audit_replay(folder),summary)
            path = Path(folder)/"decision-replay-summary.json"
            changed = deepcopy(summary);changed["http_calls"] += 1;path.write_bytes(canonical(changed))
            with self.assertRaisesRegex(ContractError,"summary"):
                replay.audit_replay(folder)
        self.assertEqual(digest(RAW.read_bytes()),before)

    def test_global_call_limit_marks_remaining_allocations_skipped(self):
        profile=ready();profile["guards"]["max_model_requests"]=2;seen=[]
        def opener(request, timeout):
            seen.append(request)
            return Reply(response())
        with TemporaryDirectory() as folder:
            with Journal(folder,"test-replay-limit") as journal:
                summary=replay.run_replay(journal,profile,self.prepared(profile),opener=opener)
        self.assertEqual(len(seen),2)
        self.assertEqual(summary["counts"],dict(response=2,skipped=22))
        self.assertEqual(summary["stop_reason"],"call_limit")

    def test_503_stops_without_retry_and_preserves_unknown_cost(self):
        profile=ready();seen=[]
        def opener(request, timeout):
            seen.append(request)
            raise HTTPError(request.full_url,503,"unavailable",None,io.BytesIO(b'{"error":{"message":"provider unavailable"}}'))
        with TemporaryDirectory() as folder:
            with Journal(folder,"test-replay-503") as journal:
                summary=replay.run_replay(journal,profile,self.prepared(profile),opener=opener)
        self.assertEqual(len(seen),1)
        self.assertEqual(summary["counts"],dict(error=1,skipped=23))
        self.assertTrue(summary["unknown_cost"])
        self.assertEqual(summary["stop_reason"],"unknown_cost")

    def test_spend_reservation_refusal_never_submits_http(self):
        profile=ready();profile["guards"]["max_reported_cost_usd"]=1e-9
        with TemporaryDirectory() as folder:
            with Journal(folder,"test-replay-spend") as journal:
                summary=replay.run_replay(journal,profile,self.prepared(profile),opener=lambda *a,**kw:self.fail("HTTP submitted"))
        self.assertEqual(summary["http_calls"],0)
        self.assertFalse(summary["unknown_cost"])
        self.assertEqual(summary["stop_reason"],"spend_limit")

    def test_last_response_with_missing_cost_is_not_financially_complete(self):
        profile=ready();seen=[]
        def opener(request, timeout):
            seen.append(request)
            return Reply(response(None if len(seen)==24 else 0.0001))
        with TemporaryDirectory() as folder:
            with Journal(folder,"test-replay-last-unknown") as journal:
                summary=replay.run_replay(journal,profile,self.prepared(profile),opener=opener)
        self.assertEqual(summary["counts"],dict(response=24))
        self.assertEqual(summary["status"],"partial")
        self.assertEqual(summary["unknown_cost_requests"],1)


if __name__ == "__main__":
    unittest.main()
