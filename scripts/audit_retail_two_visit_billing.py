"""Join each actual request to its wire reservation, original provider response and final charge."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import digest, read_json
from agent_memory_eval.models.openrouter import request_reservation_usd
from agent_memory_eval.recording.journal import BlobStore
from agent_memory_eval.study.followup_budget import ceiling_schedule
from scripts.analyze_full_retail import require
from scripts.audit_retail_order_choice import publish
from scripts.run_retail_two_visit import read


def audit(folder, *, allow_interrupted_decisions=False, decision_prefixes=None):
    registration = read(folder / "registration.json")
    jobs = registration["jobs"]
    if registration.get("format") == "retail-derived-two-visit/recovery-v1":
        jobs = {**jobs, **read(folder / "upstreams.json")["attempts"]}
    grants = read(folder / "authorization.json")
    schedule = (ceiling_schedule(folder, grants) if registration.get("format") == "agent-memory-retail-followup/v3"
                else [(0, Decimal(str(grants["max_usd"])))])
    grant_index = 0
    path = folder / "expense.jsonl"
    ledger = path.read_bytes() if path.exists() or not allow_interrupted_decisions else b""
    require(ledger.endswith(b"\n") or (allow_interrupted_decisions and not ledger), "Truncated expense ledger")
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
            while grant_index + 1 < len(schedule) and schedule[grant_index + 1][0] < seq:
                grant_index += 1
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
        require(exposure <= schedule[grant_index][1], "Ledger exceeded the cap authorized at that reservation")
    require(schedule[-1][0] <= len(reservations), "Budget increase refers to a future reservation")
    matched = set()
    generation_ids = set()
    counts = Counter()
    by_stage = defaultdict(lambda: Counter())
    interrupted = Counter()

    def match_reservation(step, run, request, blobs):
        slot = (step, run)
        reservation = calls[slot][counts[slot]]
        require(reservation["wire_sha256"] == request["request_ref"]["sha256"]
                and reservation["seq"] not in matched, "Journal and expense wire differ")
        wire = blobs.get(request["request_ref"])
        actor = "user_simulator" if request["actor_role"] == "user_simulator" else "worker"
        endpoint = registration["endpoints"][actor]
        require(reservation["model"] == wire["model"] == endpoint["model"]
                and Decimal(reservation["reserved"]) == request_reservation_usd(wire, endpoint),
                "Reserved maximum differs from the wire or registered prices")
        counts[slot] += 1
        matched.add(reservation["seq"])
        by_stage[step]["submissions"] += 1
        return reservation

    for initial in ("source", "writer", "decision"):
        for j in jobs[initial]:
            stage = initial
            path = folder / j["run"]
            log = path / "log.jsonl"
            if not log.exists():
                continue
            blobs = BlobStore(path)
            prior = {}
            incomplete = (allow_interrupted_decisions and initial == "decision"
                          and (path / "decision.started").exists() and not (path / "decision.json").exists())
            raw_log = log.read_bytes()
            if decision_prefixes is not None:
                receipt = decision_prefixes[j["run"]]
                size = receipt["journal_bytes"]
                require(initial == "decision" and receipt["status"] == "checkpoint"
                        and len(raw_log) >= size and digest(raw_log[:size]) == receipt["journal_sha256"],
                        "Follow-up billing prefix differs from its original decision")
                raw_log = raw_log[size:]
                stage = "continuation"
                incomplete = (path / "continuation.started").exists() and not (path / "continuation.json").exists()
            if incomplete and not raw_log.endswith(b"\n"):
                raw_log = raw_log[:raw_log.rfind(b"\n") + 1]
            for line in raw_log.splitlines():
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
                    r = match_reservation(step, j["run"], req, blobs)
                    group = by_stage[step]
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
            if prior:
                require(incomplete and len(prior) == 1, "Unsettled model request without terminal evidence")
                (step, req), = prior.values()
                slot = (step, j["run"])
                if counts[slot] == len(calls[slot]):
                    interrupted["before_reservation"] += 1
                    continue
                r = match_reservation(step, j["run"], req, blobs)
                if "cost" in r:
                    by_stage[step]["confirmed_usd"] += Decimal(r["cost"])
                    by_stage[step]["priced_calls"] += 1
                    if r.get("generation"):
                        require(r["generation"] not in generation_ids, "Reused provider generation")
                        generation_ids.add(r["generation"])
                    interrupted["ledger_settled_without_journal_finish"] += 1
                else:
                    by_stage[step]["held_usd"] += Decimal(r["reserved"])
                    by_stage[step]["unpriced_calls"] += 1
                    interrupted["reserved_without_journal_finish"] += 1
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
        "authorized_usd": (str(schedule[-1][1]) if len(schedule) > 1 else grants["max_usd"]),
        **({"interrupted_requests": dict(interrupted)} if allow_interrupted_decisions else {}),
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
