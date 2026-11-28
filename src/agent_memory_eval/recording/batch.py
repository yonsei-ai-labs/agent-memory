"""Read immutable allocation rows bound to a batch completion record."""

from decimal import Decimal

from agent_memory_eval.contracts.core import ContractError, digest, read_json


def sealed_rows(folder, expected_close=None):
    def load(name, expected):
        raw = (folder / name).read_bytes()
        if digest(raw) != expected:
            raise ContractError(f"Broken ledger seal in {folder}")
        return read_json(raw)

    close = (read_json((folder / "completion.json").read_bytes()) if expected_close is None
             else load("completion.json", expected_close))
    return close, load("control.json", close["control_sha256"]), load("allocations.json", close["allocation_sha256"])


def request_measurements(events, *, reservations=None):
    """Join request starts to finishes and, when available, the run's sequential expense ledger."""
    starts, finishes = [], {}
    for event in events:
        pay = event["payload"]
        if event["type"] == "model.request_started":
            starts.append(event)
        elif event["type"] == "model.request_finished":
            finishes[pay["request_id"]] = pay
    expenses = iter(reservations) if reservations is not None else None
    for start in starts:
        pay = start["payload"]
        finish = finishes.get(pay["request_id"])
        if finish and finish["delivery_status"] == "not_submitted":
            yield start, finish
            continue
        expense = next(expenses, None) if expenses is not None else None
        if expense and (expense["run"] != start["run_id"] or expense["wire_sha256"] != pay["request_ref"]["sha256"]):
            raise ContractError("Expense reservation differs from the run or recorded request wire")
        if finish:
            if expenses is not None:
                reported = finish["cost_usd"] if finish["cost_status"] == "observed" else None
                if not expense or (expense["cost"] is None) != (reported is None) or (
                    reported is not None and Decimal(expense["cost"]) != Decimal(str(reported))
                ):
                    raise ContractError("Journal request and provider expense receipt disagree")
        else:
            # A durable reservation precedes HTTP. The expense receipt can survive loss of the journal finish.
            cost = expense["cost"] if expense else None
            unsubmitted = expenses is not None and expense is None
            finish = dict(delivery_status="not_submitted" if unsubmitted else "pending",
                          cost_status="not_applicable" if unsubmitted else ("unknown" if cost is None else "observed"),
                          cost_usd=cost, usage=None)
        if expense and expense["cost"] is None:
            finish = dict(finish, held_usd=expense["reserved"])
        yield start, finish
    if expenses is not None and next(expenses, None) is not None:
        raise ContractError("Expense reservation has no matching journal request")


def usage_from_events(events, *, reservations=None):
    """Sum reported charges and tokens. An expense ledger also identifies pending or unsubmitted starts."""
    stats = {}
    for start, pay in request_measurements(events, reservations=reservations):
        role = start["payload"]["actor_role"]
        cell = stats.setdefault(role, dict(requests=0, http_calls=0, total_tokens=0, reported_usd=Decimal(0),
                                           unknown_count=0))
        cell["requests"] += 1
        if pay["delivery_status"] == "not_submitted":
            continue
        cell["http_calls"] += 1
        cell["unknown_count"] += pay["cost_status"] == "unknown"
        tokens = (pay.get("usage") or {}).get("total_tokens")
        if type(tokens) is int and tokens >= 0:
            cell["total_tokens"] += tokens
        elif reservations is not None:
            cell["unknown_token_requests"] = cell.get("unknown_token_requests", 0) + 1
        if pay["cost_status"] == "observed":
            cell["reported_usd"] += Decimal(str(pay["cost_usd"]))
        if reservations is not None and pay["delivery_status"] == "pending":
            cell["pending_reservations"] = cell.get("pending_reservations", 0) + 1
        if "held_usd" in pay:
            cell["held_usd"] = cell.get("held_usd", Decimal(0)) + Decimal(pay["held_usd"])
    for cell in stats.values():
        cell["reported_usd"] = str(cell["reported_usd"])
        if "held_usd" in cell:
            cell["held_usd"] = str(cell["held_usd"])
    return stats
