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


def usage_from_events(events):
    roles, stats = {}, {}
    for event in events:
        pay = event["payload"]
        if event["type"] == "model.request_started":
            roles[pay["request_id"]] = pay["actor_role"]
        elif event["type"] == "model.request_finished":
            role = roles[pay["request_id"]]
            cell = stats.setdefault(role, dict(requests=0, http_calls=0, total_tokens=0, reported_usd=Decimal(0),
                                               unknown_count=0))
            cell["requests"] += 1
            cell["http_calls"] += pay["delivery_status"] != "not_submitted"
            cell["unknown_count"] += pay["cost_status"] == "unknown"
            cell["total_tokens"] += (pay.get("usage") or {}).get("total_tokens") or 0
            if pay["cost_status"] == "observed":
                cell["reported_usd"] += Decimal(str(pay["cost_usd"]))
    for cell in stats.values():
        cell["reported_usd"] = str(cell["reported_usd"])
    return stats
