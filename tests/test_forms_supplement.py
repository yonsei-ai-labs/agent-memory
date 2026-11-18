from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.experiments.forms_supplement import (
    SupplementGuard, cooldown_seconds, rate_limit_evidence, run_pending,
)
from agent_memory_eval.models.openrouter import ProviderError, request_reservation_usd


class MemoryBlobs:
    def __init__(self, values):
        self.values = values

    def get(self, ref):
        return self.values[ref["sha256"]]


class SupplementTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime.now(timezone.utc)
        self.limits = dict(max_calls=5, max_usd=2, max_seconds=3600)
        self.carry = dict(http_calls=2, known_usd="0.7", original_429=dict(reserved_usd="0.3"))
        self.guard = SupplementGuard(self.limits, self.start.isoformat(), self.carry)
        self.model = dict(pricing=dict(prompt="0.0000003", completion="0.0000012"))
        self.wire = dict(messages=[dict(role="system", content="test")], max_tokens=512)
        self.response = dict(error=dict(code=429, retry_after_seconds=4))
        self.wire_hash, self.response_hash = [digest(canonical(v)) for v in (self.wire, self.response)]
        self.blobs = MemoryBlobs({self.wire_hash: self.wire, self.response_hash: self.response})
        self.events = [
            dict(type="model.request_started", payload=dict(request_id="rq", actor_role="worker",
                request_ref=dict(sha256=self.wire_hash))),
            dict(type="model.request_finished", payload=dict(request_id="rq", status="error", delivery_status="response_received",
                response_ref=dict(sha256=self.response_hash), cost_status="unknown"))]

    def receipt(self, code="HTTP_429"):
        return rate_limit_evidence(dict(error_code=code), self.events, self.blobs, dict(worker=self.model))

    def test_only_confirmed_transport_429_receives_reservation(self):
        self.assertIsNone(self.receipt("PROVIDER_RESPONSE_ERROR"))
        self.assertIsNone(self.receipt("HTTP_503"))
        self.assertEqual(Decimal(self.receipt()["reserved_usd"]), request_reservation_usd(self.wire, self.model))
        self.events[-1]["payload"]["cost_status"] = "observed"
        self.assertEqual(Decimal(self.receipt()["reserved_usd"]), 0)
        self.events[-1]["payload"]["delivery_status"] = "unknown"
        with self.assertRaises(ContractError):
            self.receipt()

    def test_one_episode_each_and_reservations_across_old_and_new_attempts(self):
        order, delays = [], []
        def execute(row):
            order.append(row["index"])
            self.guard.before(self.wire, self.model)
            special = row["index"] == 277
            self.guard.after(self.response if special else dict(usage=dict(cost=0.2)))
            result = dict(status="error" if special else "completed",
                          billing_by_role=dict(worker=dict(unknown_count=int(special))))
            return result, self.receipt() if special else None
        rows = [dict(index=i) for i in (277, 278)]
        self.assertIsNone(run_pending(rows, execute, self.guard, lambda rows: None, 60, delays.append))
        self.assertEqual((order, delays, [r["status"] for r in rows]), ([277, 278], [60], ["error", "completed"]))
        self.assertEqual(self.guard.calls, 4)
        self.assertAlmostEqual(self.guard.reported_usd, 0.9)
        self.assertEqual(self.guard.reserved, Decimal("0.3") + request_reservation_usd(self.wire, self.model))
        with self.assertRaisesRegex(ProviderError, "SPEND_LIMIT"):
            self.guard.before(dict(messages=[], max_tokens=2), dict(pricing=dict(prompt=0, completion=0.4)))

    def test_unpriced_transport_failure_ends_next_sends_without_replay(self):
        def execute(row):
            self.guard.unknown_cost = True
            return dict(status="error", billing_by_role=dict(worker=dict(unknown_count=1))), None
        rows = [dict(index=i) for i in range(501, 504)]
        self.assertEqual(run_pending(rows, execute, self.guard, lambda rows: None, 60), "unknown_cost")
        self.assertEqual([r["status"] for r in rows], ["error", "not_started", "not_started"])
        self.assertEqual(self.guard.reserved, Decimal("0.3"))

    def test_multiple_unknowns_and_disagreement_do_not_get_cleared(self):
        self.guard.unknown_cost = True
        self.guard.close_job(dict(billing_by_role=dict(worker=dict(unknown_count=2))), self.receipt())
        self.assertTrue(self.guard.unknown_cost)
        with self.assertRaises(ContractError):
            self.guard.close_job(dict(billing_by_role=dict(worker=dict(unknown_count=0))), self.receipt())

    def test_interruption_marks_suffix_and_honors_provider_wait_or_original_deadline(self):
        rows = [dict(index=277+i) for i in range(3)]
        def broken(_):
            raise ValueError("fixture")
        with self.assertRaises(ValueError):
            run_pending(rows, broken, self.guard, lambda rows: None, 60)
        self.assertEqual([r["status"] for r in rows], ["interrupted", "not_started", "not_started"])
        self.assertEqual(cooldown_seconds(dict(retry_after_seconds=96), 60, self.start), 96)
        self.assertEqual(cooldown_seconds(dict(retry_after_utc=(self.start+timedelta(seconds=185)).isoformat()), 60, self.start), 185)
        with self.assertRaisesRegex(ContractError, "time budget"):
            SupplementGuard(self.limits, (self.start-timedelta(days=5)).isoformat(), self.carry)
