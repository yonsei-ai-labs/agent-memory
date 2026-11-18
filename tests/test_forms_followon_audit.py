"""Offline checks for the third billing authorization and the three-origin analysis view."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_memory_eval.analysis import forms_followon as audit
from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.recording.journal import BlobStore


class FollowonAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.raw = Path(temporary.name)
        self.start = datetime(2026, 11, 18, tzinfo=timezone.utc)
        self.model = dict(id="model", selected_endpoint={"tag": "route", "provider_name": "Route"},
                          pricing={"prompt": "0.000001", "completion": "0.000002"})
        self.wire = dict(model="model", stream=False, seed=2, max_tokens=10, messages=[dict(role="user", content="change")],
                         provider=dict(only=["route"], allow_fallbacks=False, require_parameters=True))
        settings = {"worker": {digest(canonical({k: v for k, v in self.wire.items() if k not in {"messages", "seed"}}))}}
        self.carry = dict(unknown_calls=2, prior_504=dict(generation_id="gen-prior"))
        self.h = dict(state=dict(calls=1861, known=Decimal("0.2916642358"), reserved=Decimal("0.0100881"),
                                 next_send=self.start, settings=settings), limits=dict(max_seconds=600, max_calls=16000, max_usd=5),
                      began=self.start, seen=set(), seeds={1: {"native_and_solver": 2}}, cap=128000, carry=self.carry)

    def identity(self, i):
        return dict(index=i, phase="solve", task_id="t", repetition=1, arm="no_memory", directory=f"jobs/{i:04d}", run_id=f"run-{i}")

    def job(self, i, offset, *, runtime="PROVIDER_RESPONSE_ERROR", code="504", generation="gen-valid", price=None, retry=None):
        row = self.identity(i)
        folder = self.raw / row["directory"]
        folder.mkdir(parents=True)
        blobs = BlobStore(folder)
        request = blobs.put(self.wire)
        body = dict(error=dict(code=code), id=generation) if runtime else dict(model="model", provider="Route")
        if retry is not None:
            body["error"]["retry_after_seconds"] = retry
        if price is not None:
            body["usage"] = dict(total_tokens=3, cost=price)
        ref = blobs.put(body)
        req = "request-" + str(i)
        def event(kind, value, delta):
            return dict(type=kind, payload=value, run_id=row["run_id"],
                        timestamp_utc=(self.start + timedelta(seconds=offset + delta)).isoformat())
        begin = dict(request_id=req, actor_role="worker", request_ref=request)
        finish = dict(request_id=req, response_ref=ref, status="error" if runtime else "response", delivery_status="response_received",
                      cost_status="unknown" if price is None else "observed", cost_usd=price, usage=body.get("usage"))
        events = [event("model.request_started", begin, 0), event("model.request_finished", finish, 1)]
        if runtime:
            events.append(event("runtime.error", dict(code=runtime), 1))
        raw = canonical(dict(events=events)) + b"\n"
        (folder / "log.jsonl").write_bytes(raw)
        size = len(canonical(self.wire))
        zero = dict(requests=0, http_attempts=0, responses=0, input_bytes=0, input_bytes_by_delivery={},
                    reported_tokens=0, reported_cost_usd_decimal="0", unknown_cost_requests=0)
        bill = dict(requests=1, http_calls=1, total_tokens=3 if price is not None else 0, reported_usd=str(price or 0),
                    unknown_count=int(price is None))
        measured = dict(requests=1, http_attempts=1, responses=int(runtime is None), input_bytes=size,
                        input_bytes_by_delivery={"response_received": size}, reported_tokens=bill["total_tokens"],
                        reported_cost_usd_decimal=bill["reported_usd"], unknown_cost_requests=bill["unknown_count"])
        status = "error" if runtime else "completed"
        result = dict(run_id=row["run_id"], status=status, log_sha256=digest(raw), error_code=runtime,
            grading_status="incomplete" if runtime else "graded", official_score=None if runtime else 1.0,
            official_success=None if runtime else True, first_worker_request_sha256=request["sha256"],
            billing_by_role={"worker": bill}, usage_by_role={"worker": measured, "user_simulator": zero},
            requests_by_role={"worker": 1, "user_simulator": 0}, memory_input_spans_by_delivery={"response_received": 0},
            memory_input_spans=0, memory_input_fields={}, normal_response_memory_spans=0, unknown_cost=price is None)
        row.update(status=status, result=result)
        exception = audit.provider_failure(runtime, result, begin, events[1], self.wire, body, self.model)
        if exception:
            row["provider_error"] = exception
        return row

    def run_check(self, rows, *, stop=None, history=None):
        h = deepcopy(history or self.h)
        bills = [r["result"]["billing_by_role"]["worker"] for r in rows if "result" in r]
        unknown = sum(v["unknown_count"] for v in bills)
        known = sum((Decimal(v["reported_usd"]) for v in bills), Decimal(0))
        reserved = sum((Decimal(r["provider_error"]["reserved_usd"]) for r in rows if r.get("provider_error")), h["state"]["reserved"])
        seal = dict(ended_at=(self.start + timedelta(seconds=590)).isoformat(), other_unpriced_stop=stop=="unknown_cost",
                    stop_reason=stop, supplement=dict(http_calls=len(bills), unknown_calls=unknown, known_usd=str(known)),
                    combined_http_calls=h["state"]["calls"]+len(bills), combined_unknown_calls=2+unknown,
                    combined_reserved_usd=str(reserved), combined_known_usd=str(h["state"]["known"]+known))
        batch = dict(rows=rows, control=dict(started_at=self.start.isoformat(), model_catalog_entries={"worker": self.model}), seal=seal)
        return audit.check_followon(self.raw, batch, h)

    def test_same_generation_sources_and_safe_format(self):
        for res in (dict(id="gen-top"), dict(generation_id="gen-top"),
                    dict(error=dict(metadata=dict(generation_id="gen-top")))):
            self.assertEqual(audit.generation_id(res), "gen-top")
        for bad in ({"id": "gen-not valid"}, {"error": {"generation_id": "gen-ignored"}}, {"id": "gen-"}, {}):
            self.assertIsNone(audit.generation_id(bad))

    def test_json_504_uses_sixty_seconds_and_keeps_unpriced_reserve(self):
        error = self.job(303, 0, retry=500)
        self.assertEqual(error["provider_error"]["code"], 504)
        self.assertIsNone(error["provider_error"]["retry_after_seconds"])
        self.assertGreater(Decimal(error["provider_error"]["reserved_usd"]), 0)
        _, failures = self.run_check([error, self.job(304, 61, runtime=None, price=0.002)])
        self.assertEqual(set(failures), {303})
        with self.assertRaisesRegex(ContractError, "cooldown"):
            self.run_check([error, self.job(305, 60, runtime=None, price=0.002)])

    def test_http_504_observed_charge_and_retry_after(self):
        paid = self.job(303, 0, runtime="HTTP_504", code=504, price=0.0004, retry=100)
        self.assertEqual(paid["provider_error"]["reserved_usd"], "0")
        self.run_check([paid, self.job(304, 101, runtime=None, price=0.002)])
        with self.assertRaisesRegex(ContractError, "cooldown"):
            self.run_check([paid, self.job(305, 100, runtime=None, price=0.002)])

    def test_unidentified_504_and_nontransport_429_stop_when_unpriced(self):
        unidentified = self.job(303, 0, runtime="HTTP_504", code=504, generation=None)
        wrong_429 = self.job(304, 61, runtime="PROVIDER_RESPONSE_ERROR", code=429)
        for row in (unidentified, wrong_429):
            self.assertNotIn("provider_error", row)
            self.run_check([row], stop="unknown_cost")
            with self.assertRaisesRegex(ContractError, "stop disposition"):
                self.run_check([row])
        with self.assertRaisesRegex(ContractError, "beyond a stop"):
            self.run_check([unidentified, wrong_429], stop="unknown_cost")
        self.run_check([self.job(305, 0, runtime="HTTP_429", code=429, generation=None)])

    def test_forged_receipt_reused_id_wrong_lengths_and_cumulative_budget(self):
        valid = self.job(303, 0)
        edited = deepcopy(valid)
        edited["provider_error"]["request_sha256"] = "f"*64
        with self.assertRaisesRegex(ContractError, "Declared provider exception"):
            self.run_check([edited])
        reused = self.job(304, 70, generation="gen-prior")
        with self.assertRaisesRegex(ContractError, "generation ID was reused"):
            self.run_check([reused])
        edited = deepcopy(valid)
        edited["result"]["usage_by_role"]["worker"]["input_bytes"] += 1
        with self.assertRaisesRegex(ContractError, "byte lengths"):
            self.run_check([edited])
        low = deepcopy(self.h)
        low["limits"]["max_usd"] = 0.3018
        with self.assertRaisesRegex(ContractError, "spend reservation cap"):
            self.run_check([valid], history=low)
        low = deepcopy(self.h)
        low["limits"]["max_calls"] = 1861
        with self.assertRaisesRegex(ContractError, "call or spend"):
            self.run_check([valid], history=low)

    def test_merge_preserves_prior_origins_and_rejects_a_repeat_of_302(self):
        prior = [dict(self.identity(i), status="completed", result={}, analysis_origin="original" if i<=276 else "supplement",
                      episode_origin="original" if i<=276 else "supplement") for i in range(233,303)]
        pending = [dict(self.identity(i), status="not_started", reason="unknown_cost", analysis_origin="supplement",
                        episode_origin=None) for i in range(303,585)]
        fresh = [dict(self.identity(i), status="not_started", reason="call_limit") for i in range(303,585)]
        fresh[0]=dict(self.identity(303), status="completed",result={})
        merged=audit.merge_followon(prior+pending,fresh)
        self.assertEqual(merged[:70],prior)
        self.assertEqual((merged[70]["episode_origin"],merged[71]["episode_origin"]),("followon",None))
        with self.assertRaises(ContractError):
            audit.merge_followon(prior+pending,[prior[-1],*fresh[1:]])
