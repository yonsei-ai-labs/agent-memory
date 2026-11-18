"""Small offline negative cases for the evidence merger and billing amendment."""

from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_memory_eval.analysis.forms_supplement import bills_total, check_billing, merge_rows, wire_evidence, write_episode_origins
from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.models.openrouter import request_reservation_usd
from agent_memory_eval.recording.journal import BlobStore


class SupplementEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.raw = Path(temp.name)
        self.start = datetime(2026, 11, 18, tzinfo=timezone.utc)
        self.model = dict(id="worker", pricing=dict(prompt="0.000001", completion="0.000002"),
                          selected_endpoint=dict(tag="route", provider_name="Route"))
        self.models = dict(worker=self.model)
        self.limits = dict(max_seconds=10000, max_usd=5, max_calls=20)

    def row(self, index, **extra):
        return dict(index=index, phase="solve", task_id="task", repetition=1, arm="no_memory",
                    directory=f"jobs/{index:04d}", run_id=f"run-{index}", **extra)

    def job(self, index, offset, *, code=None, provider_code=429, price=None, retry=None):
        row = self.row(index)
        folder = self.raw / row["directory"]
        folder.mkdir(parents=True)
        blobs = BlobStore(folder)
        wire = dict(model="worker", stream=False, provider=dict(only=["route"], allow_fallbacks=False, require_parameters=True),
                    messages=[dict(role="user", content=str(index))], max_tokens=10)
        request_ref = blobs.put(wire)
        if code:
            error = dict(code=provider_code)
            if retry is not None:
                error["retry_after_seconds"] = retry
            response = dict(error=error)
        else:
            response = dict(model="worker", provider="Route")
        if price is not None:
            response["usage"] = dict(cost=price, total_tokens=7)
        response_ref = blobs.put(response)
        request_id = "request-" + str(index)
        begun = self.start + timedelta(seconds=offset)
        def event(kind, payload, seconds):
            return dict(type=kind, payload=payload, run_id=row["run_id"],
                        timestamp_utc=(begun + timedelta(seconds=seconds)).isoformat())
        events = [
            event("model.request_started", dict(request_id=request_id, request_ref=request_ref, actor_role="worker"), 0),
            event("model.request_finished", dict(request_id=request_id, status="error" if code else "response",
                  delivery_status="response_received", response_ref=response_ref, cost_usd=price, usage=response.get("usage"),
                  cost_status="unknown" if price is None else "observed"), 1)]
        if code:
            events.append(event("runtime.error", dict(code=code), 1))
        raw = canonical(dict(events=events)) + b"\n"
        (folder / "log.jsonl").write_bytes(raw)
        status = "error" if code else "completed"
        row.update(status=status, result=dict(status=status, run_id=row["run_id"], log_sha256=digest(raw), error_code=code,
                   grading_status="incomplete" if code else "graded", official_score=None if code else 1.0,
                   official_success=None if code else True, unknown_cost=price is None,
                   billing_by_role=dict(worker=dict(requests=1, http_calls=1, total_tokens=0 if price is None else 7,
                       reported_usd=str(price or 0), unknown_count=int(price is None)))))
        if code == "HTTP_429":
            row["http_429"] = dict(request_id=request_id, actor="worker", request_sha256=request_ref["sha256"],
                response_sha256=response_ref["sha256"], cost_status="unknown" if price is None else "observed",
                reserved_usd=str(request_reservation_usd(wire, self.model) if price is None else Decimal(0)),
                retry_after_seconds=retry, retry_after_utc=None)
        return row

    def check(self, rows, *, other=False, limits=None):
        state = dict(calls=0, known=Decimal(0), reserved=Decimal(0), next_send=None)
        batch = dict(rows=rows, control=dict(model_catalog_entries=self.models, started_at=self.start.isoformat()),
                     seal=dict(ended_at=(self.start + timedelta(seconds=1000)).isoformat(), other_unpriced_stop=other,
                               stop_reason="unknown_cost" if other else None))
        bills, failures = check_billing(self.raw, batch, limits or self.limits, self.start, state, set(), supplemental=True)
        return bills_total(bills), failures, state

    def test_only_untouched_assignments_can_receive_a_supplement(self):
        original = [self.row(1, status="error", result=dict(official_score=None)),
                    self.row(2, status="not_started", reason="unknown_cost"), self.row(3, status="not_started", reason="unknown_cost")]
        original_before = deepcopy(original)
        supplement = [self.row(2, status="completed", result=dict(official_score=1)), self.row(3, status="not_started", reason="call_limit")]
        merged = merge_rows(original, supplement)
        self.assertEqual(original, original_before)
        self.assertEqual([r["episode_origin"] for r in merged], ["original", "supplement", None])
        self.assertEqual(merged[2]["reason"], "call_limit")
        path = self.raw / "episode-origins.csv"
        write_episode_origins(path, merged)
        with path.open() as stream:
            origins = list(csv.DictReader(stream))
        self.assertEqual([(r["job"], r["episode_origin"]) for r in origins], [("1", "original"), ("2", "supplement"), ("3", "")])
        for wrong in ([original[0], supplement[1]], list(reversed(supplement)), supplement[:1],
                      [dict(supplement[0], run_id="changed"), supplement[1]]):
            with self.subTest(wrong=wrong), self.assertRaises(ContractError):
                merge_rows(original, wrong)

    def test_unknown_429_reserved_separately_from_cost_and_cooldown(self):
        limited = self.job(1, 0, code="HTTP_429", retry=80)
        normal = self.job(2, 81, price=0.002)
        total, errs, state = self.check([limited, normal])
        self.assertEqual(total, dict(http_calls=2, known_usd="0.002", unknown_calls=1))
        self.assertEqual(errs, {1: limited["http_429"]})
        self.assertEqual(state["reserved"], Decimal(limited["http_429"]["reserved_usd"]))
        early = self.job(3, 80, price=0.002)
        with self.assertRaisesRegex(ContractError, "cooldown"):
            self.check([limited, early])

    def test_other_unknown_must_stop_even_when_provider_body_contains_429(self):
        other = self.job(1, 0, code="PROVIDER_RESPONSE_ERROR")
        total, errors, state = self.check([other], other=True)
        self.assertEqual(total["unknown_calls"], 1)
        self.assertEqual(errors, {})
        self.assertEqual(state["reserved"], 0)
        with self.assertRaisesRegex(ContractError, "beyond its sealed stop"):
            self.check([other, self.job(2, 80, price=0.001)], other=True)
        with self.assertRaisesRegex(ContractError, "non-429 stop"):
            self.check([other])

    def test_429_needs_the_provider_code_and_cannot_be_graded(self):
        wrong = self.job(1, 0, code="HTTP_429", provider_code=503)
        with self.assertRaisesRegex(ContractError, "transport classification"):
            self.check([wrong])
        graded = self.job(2, 90, code="HTTP_429")
        graded["result"]["official_score"] = 0
        with self.assertRaisesRegex(ContractError, "treated as graded"):
            self.check([graded])

    def test_tampered_cost_sends_or_receipt_and_shared_budget_fail(self):
        row = self.job(1, 0, code="HTTP_429", price=0.0005)
        totals, _, state = self.check([row])
        self.assertEqual((totals["known_usd"], state["reserved"]), ("0.0005", 0))
        for key, value in (("reported_usd", "0"), ("http_calls", 0), ("unknown_count", 1)):
            bad = deepcopy(row)
            bad["result"]["billing_by_role"]["worker"][key] = value
            with self.assertRaisesRegex(ContractError, "Per-role billing"):
                self.check([bad])
        bad = deepcopy(row)
        bad["http_429"]["response_sha256"] = "f" * 64
        with self.assertRaisesRegex(ContractError, "Ledger 429"):
            self.check([bad])
        with self.assertRaisesRegex(ContractError, "monetary budget"):
            self.check([row], limits=dict(self.limits, max_usd=0.00001))
        seen = set()
        wire_evidence(self.raw / row["directory"], row, self.models, seen)
        with self.assertRaisesRegex(ContractError, "Duplicate request"):
            wire_evidence(self.raw / row["directory"], row, self.models, seen)

    def test_supplement_cannot_change_seed_or_wire_settings(self):
        row = self.job(1, 5, price=0.01)
        folder = self.raw / row["directory"]
        with self.assertRaisesRegex(ContractError, "repetition seed"):
            wire_evidence(folder, row, self.models, set(), seeds=dict(native_and_solver=2))
        with self.assertRaisesRegex(ContractError, "tool interface"):
            wire_evidence(folder, row, self.models, set(), settings={"worker": {"wrong-hash"}}, supplemental=True)
        settings = {}
        wire_evidence(folder, row, self.models, set(), settings=settings)
        wire_evidence(folder, row, self.models, set(), settings=settings, supplemental=True)
