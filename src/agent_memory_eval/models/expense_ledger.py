"""Durable, sequential OpenRouter request reservations and provider cost receipts."""

import os
import time
from datetime import UTC, datetime
from decimal import Decimal

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.experiments.forms_supplement import cooldown_seconds
from agent_memory_eval.models.openrouter import ProviderError, request_reservation_usd


def _ledger_rows(path):
    if path.exists():
        with path.open("rb") as stream:
            for line in stream:
                require(line.endswith(b"\n"), "Unreconciled expense ledger tail")
                yield read_json(line)


def _reservation(row, next_seq):
    maximum = Decimal(row["reserved"])
    require(type(row["seq"]) is int and row["seq"] == next_seq and maximum.is_finite() and maximum >= 0,
            "Out-of-order reservation")
    return maximum


def _charge(row, maximum):
    cost = Decimal(row["cost"])
    require(maximum is not None and cost.is_finite() and 0 <= cost <= maximum,
            "Invalid receipt or pricing exceeded reservation")
    return cost


def read_run_reservations(path):
    """Read each run's ordered wire reservations, including costs receipted before a journal finish."""
    entries, by_run = [], {}
    for row in _ledger_rows(path):
        seq = row["seq"]
        if "reserved" in row:
            _reservation(row, len(entries) + 1)
            entry = dict(row, cost=None)
            entries.append(entry)
            by_run.setdefault(row["run"], []).append(entry)
        else:
            require(type(seq) is int and 1 <= seq <= len(entries), "Receipt without a reservation")
            entry = entries[seq - 1]
            require(entry["cost"] is None, "Duplicate expense receipt")
            _charge(row, Decimal(entry["reserved"]))
            entry["cost"] = row["cost"]
    return by_run


class ExpenseLedger:
    """Persist the full request reservation before HTTP. Unreconciled requests retain it after restart."""

    def __init__(self, path, cap, max_calls, deadline):
        self.path, self.cap, self.max_calls, self.deadline = path, Decimal(str(cap)), max_calls, deadline
        self.calls, self.spent, self.held, self.open = 0, Decimal(0), Decimal(0), {}
        self.reserved_runs = set()
        self.denied = self.unknown_cost = False
        self.stage = self.run_id = None
        self.retry_after = 0
        for row in _ledger_rows(path):
            self.apply(row)

    def apply(self, row):
        seq = row["seq"]
        if "reserved" in row:
            maximum = _reservation(row, self.calls + 1)
            self.calls, self.open[seq] = seq, maximum
            self.reserved_runs.add(row.get("run"))
            self.held += maximum
        else:
            cost = _charge(row, self.open.get(seq))
            self.held -= self.open.pop(seq)
            self.spent += cost
        require(
            self.spent + self.held <= self.cap and self.calls <= self.max_calls,
            "Expense ledger exceeds authorization",
        )

    def append(self, row):
        with self.path.open("ab") as stream:
            stream.write(canonical(row) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.apply(row)

    def can_reserve(self, maximum):
        return (self.calls < self.max_calls and self.spent + self.held + maximum <= self.cap
                and time.monotonic() < self.deadline)

    def before(self, wire, model):
        maximum = request_reservation_usd(wire, model)
        if not self.can_reserve(maximum):
            self.denied = True
            raise ProviderError("STUDY_LIMIT")
        if self.unknown_cost:
            raise ProviderError("UNPRICED_SLOT")
        self.append(
            {
                "seq": self.calls + 1,
                "reserved": str(maximum),
                "wire_sha256": digest(canonical(wire)),
                "model": model["id"],
                "stage": self.stage,
                "run": self.run_id,
            }
        )

    def after(self, response):
        error = response.get("error") if isinstance(response, dict) else None
        if isinstance(error, dict) and (error.get("retry_after_seconds") or error.get("retry_after_utc")):
            self.retry_after = cooldown_seconds(error, 0, datetime.now(UTC))
        usage = response.get("usage") if isinstance(response, dict) else None
        cost = usage.get("cost") if isinstance(usage, dict) else None
        if type(cost) not in (float, int) or not Decimal(str(cost)).is_finite() or cost < 0:
            self.unknown_cost = True
            return
        self.append({"seq": self.calls, "cost": str(cost), "generation": response.get("id")})
