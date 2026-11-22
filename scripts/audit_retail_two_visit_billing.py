"""Join each actual request to its wire reservation, original provider response and final charge."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import digest, read_json
from agent_memory_eval.models.openrouter import request_reservation_usd
from agent_memory_eval.recording.journal import BlobStore
from scripts.analyze_full_retail import require
from scripts.audit_retail_order_choice import publish
from scripts.run_retail_two_visit import read


def audit(folder):
    registration = read(folder / "registration.json")
    jobs = registration["jobs"]
    if registration.get("format") == "retail-derived-two-visit/recovery-v1":
        jobs = {**jobs, **read(folder / "upstreams.json")["attempts"]}
    grants = read(folder / "authorization.json")
    ledger = (folder / "expense.jsonl").read_bytes()
    require(ledger.endswith(b"\n"), "Truncated expense ledger")
    reservations = {}
    calls = defaultdict(list)
    exposure = Decimal(0)
    maximum_exposure = Decimal(0)
    for raw in ledger.splitlines():
        row = read_json(raw)
        seq = row["seq"]
        if "reserved" in row:
            require(
                seq == len(reservations) + 1 and Decimal(row["reserved"]) >= 0, "Invalid reservation sequence"
            )
            reservations[seq] = row
            calls[row["stage"], row["run"]].append(row)
            exposure += Decimal(row["reserved"])
        else:
            r = reservations[seq]
            require(
                "cost" not in r and 0 <= Decimal(row["cost"]) <= Decimal(r["reserved"]), "Incorrect settlement"
            )
            r.update(row)
            exposure += Decimal(row["cost"]) - Decimal(r["reserved"])
        maximum_exposure = max(maximum_exposure, exposure)
        require(exposure <= Decimal(str(grants["max_usd"])), "Ledger exceeded the authorized cap")
    matched = set()
    generation_ids = set()
    counts = Counter()
    by_stage = defaultdict(lambda: Counter())
    for initial in ("source", "writer", "decision"):
        for j in jobs[initial]:
            stage = initial
            path = folder / j["run"]
            log = path / "log.jsonl"
            if not log.exists():
                continue
            blobs = BlobStore(path)
            prior = {}
            for line in log.read_bytes().splitlines():
                for e in read_json(line)["events"]:
                    p = e["payload"]
                    if p.get("origin") == "revisit.continued":
                        stage = "continuation"
                    if e["type"] == "model.request_started":
                        require(p["request_id"] not in prior, "Duplicated internal request")
                        prior[p["request_id"]] = (stage, p)
                    if e["type"] != "model.request_finished":
                        continue
                    step, req = prior.pop(p["request_id"])
                    if p["delivery_status"] == "not_submitted":
                        counts["local_rejection_without_submission"] += 1
                        continue
                    slot = (step, j["run"])
                    position = counts[slot]
                    r = calls[slot][position]
                    require(
                        r["wire_sha256"] == req["request_ref"]["sha256"] and r["seq"] not in matched,
                        "Journal and expense wire differ",
                    )
                    wire = blobs.get(req["request_ref"])
                    endpoint = registration["endpoints"][
                        "user_simulator" if req["actor_role"] == "user_simulator" else "worker"
                    ]
                    require(
                        r["model"] == wire["model"] == endpoint["model"]
                        and Decimal(r["reserved"]) == request_reservation_usd(wire, endpoint),
                        "Reserved maximum differs from the wire or registered prices",
                    )
                    counts[slot] += 1
                    matched.add(r["seq"])
                    group = by_stage[step]
                    group["submissions"] += 1
                    if p["cost_status"] == "observed":
                        raw = blobs.get(p["response_ref"])
                        require(
                            "cost" in r
                            and Decimal(r["cost"])
                            == Decimal(str(p["cost_usd"]))
                            == Decimal(str(raw["usage"]["cost"]))
                            and r["generation"] == raw.get("id"),
                            "Confirmed cost or provider ID changed",
                        )
                        if raw.get("id"):
                            require(raw["id"] not in generation_ids, "Reused provider generation")
                            generation_ids.add(raw["id"])
                        group["confirmed_usd"] += Decimal(r["cost"])
                        group["priced_calls"] += 1
                    else:
                        require("cost" not in r, "Settled charge marked unknown")
                        group["held_usd"] += Decimal(r["reserved"])
                        group["unpriced_calls"] += 1
            require(not prior, "Unsettled model request without terminal evidence")
    require(matched == set(reservations), "Unmatched expense entries")
    total = sum((g["confirmed_usd"] for g in by_stage.values()), Decimal(0))
    held = sum((g["held_usd"] for g in by_stage.values()), Decimal(0))
    summary = read(folder / "summary.json")
    require(
        total == Decimal(summary["confirmed_usd"])
        and held == Decimal(summary["held_usd"])
        and total + held == exposure
        and len(matched) == summary["reserved_calls"] <= grants["max_requests"],
        "Billing total differs from closure or exceeds authorization",
    )
    if registration.get("format") == "retail-derived-two-visit/recovery-v1":
        original_cost, original_hold = (Decimal(grants[k]) for k in ("original_confirmed_usd", "original_held_usd"))
        require(
            original_cost + total == Decimal(summary["combined_confirmed_usd"])
            and original_hold + held == Decimal(summary["combined_held_usd"])
            and original_cost + original_hold + maximum_exposure <= Decimal(str(grants["combined_max_usd"])),
            "Combined cost or retained reservations exceed the authorized total",
        )
    return {
        "registered_commit": registration["freeze"]["implementation_commit"],
        "ledger_sha256": digest(ledger),
        "verified_submissions": len(matched),
        "verified_distinct_provider_ids": len(generation_ids),
        "confirmed_usd": str(total),
        "held_usd": str(held),
        "peak_confirmed_plus_held_usd": str(maximum_exposure),
        "authorized_usd": grants["max_usd"],
        "per_stage": {
            s: {k: str(v) if isinstance(v, Decimal) else v for k, v in g.items()} for s, g in by_stage.items()
        },
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    v = audit(a.run)
    if a.output:
        publish(a.output, v)
    print(v)
