"""Reconcile a sealed retail shard's observed fees and identifiable unpriced requests without changing its ledger."""

import re
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import digest
from agent_memory_eval.models.openrouter import request_reservation_usd
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path
from scripts import run_full_retail as retail
from scripts.analyze_full_retail import require


def bounded_receipt(folder, slot, models, policy):
    """Bind one unpriced request to its actual wire, endpoint and terminal provider event."""
    res = slot["result"]
    unknowns = {role: b["unknown_count"] for role, b in res["billing_by_role"].items() if b["unknown_count"]}
    error = res.get("error_code") or ""
    http = re.fullmatch(r"HTTP_([45][0-9]{2})", error)
    if http:
        code = int(http[1])
    elif error == "TRANSPORT_UNKNOWN" and policy.get("reserve_uncertain_transport"):
        code = error
    else:
        return None
    if sum(unknowns.values()) != 1:
        return None
    job = safe_path(folder, slot["directory"])
    require(digest((job / "log.jsonl").read_bytes()) == res["log_sha256"], "Changed uncertain episode")
    scan, _, _ = read_experiment(job)
    require(set(scan["starts"]) == {slot["run_id"]} and not scan.get("pending_requests"), "Unclosed episode")
    blobs = BlobStore(job)
    starts, ends = {}, {}
    for event in scan["events"].values():
        p = event["payload"]
        if event["type"] in {"model.request_started", "model.request_finished"}:
            require(event["run_id"] == slot["run_id"], "Unrelated model request")
            rows = starts if event["type"] == "model.request_started" else ends
            require(p["request_id"] not in rows, "Duplicate request event")
            rows[p["request_id"]] = p
    require(starts.keys() == ends.keys(), "Unclosed request ledger")
    amounts = Decimal(0)
    unresolved = []
    for rid, e in ends.items():
        if e["cost_status"] == "unknown":
            unresolved.append((rid, e))
        elif e["cost_status"] == "observed":
            raw = blobs.get(e["response_ref"])
            amount = Decimal(str(raw["usage"]["cost"]))
            require(raw.get("id") and amount == Decimal(str(e["cost_usd"])), "Provider fee differs")
            amounts += amount
        else:
            require(
                e["delivery_status"] == "not_submitted" and e.get("cost_usd") in (None, 0),
                "Unrecognized cost",
            )
    require(
        len(unresolved) == 1
        and amounts == sum(Decimal(b["reported_usd"]) for b in res["billing_by_role"].values()),
        "Unpriced or reported requests differ from billing",
    )
    rid, e = unresolved[0]
    s = starts[rid]
    role = s["actor_role"]
    require(unknowns == {role: 1} and e["status"] == "error", "Unknown charge differs from its role ledger")
    if code == "TRANSPORT_UNKNOWN":
        require(
            e["delivery_status"] in {"unknown", "response_received"} and e.get("response_ref") is None,
            "Ambiguous uncertain transport",
        )
    else:
        require(
            e["delivery_status"] == "response_received"
            and e.get("response_ref")
            and blobs.get(e["response_ref"]).get("error", {}).get("code") == code,
            "Provider receipt differs",
        )
    model = models["worker" if role == "writer" else role]
    wire = blobs.get(s["request_ref"])
    require(
        wire["model"] == model["id"]
        and wire["provider"]
        == {"only": [model["selected_endpoint"]["tag"]], "allow_fallbacks": False, "require_parameters": True}
        and wire["stream"] is False,
        "Different model or route",
    )
    maximum = request_reservation_usd(wire, model)
    receipt = {
        "request_id": rid,
        "code": code,
        "role": role,
        "usd": str(maximum),
        "request_ref": s["request_ref"],
        "response_ref": e["response_ref"],
    }
    prior = res.get("unpriced_reservation")
    require(prior is None or prior == receipt, "Existing request maximum differs")
    return {
        "run": slot["run_id"],
        "journal_sha256": res["log_sha256"],
        "original_request_hold": prior is not None,
        **receipt,
    }


def cost_basis(root: Path, end, reg, policy):
    known = held = Decimal(0)
    changed = {}
    receipts = []
    for name, seal in sorted(end["stages"].items()):
        known += Decimal(seal["reported"])
        old_hold = Decimal(seal["held"])
        if not old_hold:
            continue
        folder = root / name
        _, ctl, rows = retail.sealed_rows(folder)
        models = ctl["model_catalog_entries"]
        terms = retail.endpoint_terms(models)
        require(
            all(reg["endpoint_terms"][k] == v for k, v in terms.items())
            and retail.matches_accounting_seal(folder, ctl["profile"]["guards"]["max_usd"], seal),
            "Endpoint pricing or original grant changed",
        )
        bounds = []
        whole = False
        eligible = True
        for row in rows:
            res = row.get("result", {})
            unknown_count = sum(b["unknown_count"] for b in res.get("billing_by_role", {}).values())
            require(
                not unknown_count or res.get("unknown_cost"), "Unpriced request missing from stage accounting"
            )
            if not res.get("unknown_cost"):
                continue
            prior = res.get("unpriced_reservation")
            whole |= prior is None
            receipt = bounded_receipt(folder, row, models, policy)
            if receipt:
                receipts.append({"stage": name, **receipt})
                bounds.append(Decimal(receipt["usd"]))
            elif prior:
                # A pre-existing capped provider receipt keeps its full registered amount.
                bounds.append(Decimal(prior["usd"]))
            else:
                eligible = False
        amount = sum(bounds, Decimal(0))
        if whole and eligible:
            require(amount <= old_hold, "Verified request maxima exceed the original conservative grant")
            held += amount
            changed[name] = {"original_held": str(old_hold), "verified_held": str(amount)}
        else:
            require(whole or amount == old_hold, "Original request reservations differ")
            held += old_hold
    require(len({r["request_id"] for r in receipts}) == len(receipts), "Reused uncertain request")
    return {
        "original_covered": end["total_covered_usd"],
        "reported": str(known),
        "held": str(held),
        "covered": str(known + held),
        "reconciled_shards": changed,
        "requests": receipts,
    }
