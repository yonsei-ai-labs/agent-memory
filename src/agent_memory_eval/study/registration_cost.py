"""Registration-wide observed charges and outstanding reservations across all cohort attempts."""

from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.models.expense_ledger import read_run_reservations
from agent_memory_eval.recording.assets import asset_bytes
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.study.orchestrator import shard_accounting
from agent_memory_eval.study.plan import plan_digest
from agent_memory_eval.study.records import read, sealed_rows


class Charge(NamedTuple):
    reported: Decimal
    held: Decimal
    requests: int
    unknown: int | None


def _totals(receipts):
    reported = sum((r.reported for r in receipts), Decimal(0))
    held = sum((r.held for r in receipts), Decimal(0))
    unknown = None if any(r.unknown is None for r in receipts) else sum(r.unknown for r in receipts)
    return dict(reported_usd=format(reported, "f"), held_usd=format(held, "f"),
                requests=sum(r.requests for r in receipts), unknown_price_requests=unknown)


def _tau_receipts(folder, plan, record, closure, receipts, *, source_supplement=False):
    expected, allowed = set(), {}
    for case in record["cases"]:
        for base in case["scheduled_stages"]:
            name = f"task-{case['task']}/{base}"
            kind = "solve" if base.startswith("solve-repeat-") else base
            require(kind in receipts, "Unexpected cohort cost stage")
            expected.add(name)
            allowed[name] = case["task"], kind
            if kind == "solve":
                for n in range(1, len(plan["retry"]["wait_seconds"]) + 1):
                    allowed[f"{name}-retry-{n}"] = case["task"], kind
    present = {str(p.relative_to(folder)) for p in folder.glob("task-*/*") if p.is_dir()}
    require(present <= allowed.keys(), "Unregistered cohort cost shard")
    closed_entries = closure["stages"] if closure is not None else {}
    if closure is not None:
        if source_supplement:
            require(all(allowed.get(n, (None, None))[1] == "source" for n in closed_entries),
                    "A first-pass supplement contains another stage")
        else:
            require(expected <= closed_entries.keys(), "Completed cohort cost ledger has missing allocations")
        require(closed_entries.keys() <= allowed.keys() and present == {
            key for key, value in closed_entries.items() if value.get("status") != "not_started"
        }, "Completed cohort cost ledger has missing or unexpected shards")
        for entry in closed_entries.values():
            if entry.get("status") == "not_started":
                require(entry["http"] == 0 and all(Decimal(entry[k]) == 0 for k in ("spent", "reported", "held")),
                        "Unstarted cohort stage has recorded spending")

    unsealed = []
    registration = digest(canonical(record))
    for name in sorted(present):
        shard = safe_path(folder, name)
        if not (shard / "completion.json").is_file():
            require(closure is None, "Completed registration contains an unsealed cost shard")
            unsealed.append(name)
            continue
        task, kind = allowed[name]
        entry = closed_entries.get(name)
        _, control, rows = sealed_rows(shard, entry["seal"] if entry is not None else None)
        profile = control["profile"]
        require(profile["case"] == task and profile["registration_sha256"] == registration
                and profile["name"] == plan["id"] and profile["stage"] == control["design"]["stage"] == kind,
                "Cohort cost shard belongs to another registration or stage")
        cost = shard_accounting(shard, str(profile["guards"]["max_usd"]))
        if entry is not None:
            require(entry["seal"] == cost["seal"] and entry["http"] == cost["http"] and
                    all(Decimal(entry[k]) == Decimal(cost[k]) for k in ("reported", "held", "spent")),
                    "Controller spending differs from the sealed stage ledger")
        unknown = 0
        for row in rows:
            result = row.get("result") or {}
            count = sum(cell["unknown_count"] for cell in result.get("billing_by_role", {}).values())
            if result.get("unknown_cost") and (not count or result.get("status") == "interrupted"):
                unknown = None  # A partial journal can leave the number of submissions unknown.
                break
            unknown += count
        receipts[kind].append(Charge(Decimal(cost["reported"]), Decimal(cost["held"]), cost["http"], unknown))
    return unsealed


def _bfcl_receipts(folder, closure, receipts):
    ledger = folder / "expenses.jsonl"
    if closure is not None:
        expected = closure["files"].get(ledger.name)
        require(expected == (digest(ledger.read_bytes()) if ledger.is_file() else None),
                "BFCL expenses differ from the registered completion seal")
    for run in read_run_reservations(ledger).values():
        for entry in run:
            require(entry["stage"] in receipts, "Unregistered BFCL cohort expense stage")
            pending = entry["cost"] is None
            reported, held = (Decimal(0), Decimal(entry["reserved"])) if pending else (Decimal(entry["cost"]), Decimal(0))
            receipts[entry["stage"]].append(Charge(reported, held, 1, int(pending)))


def registration_cost(plan, workspace):
    """Read financial evidence for the complete plan, independently of any selected tasks or terminal outcomes."""
    folder = Path(workspace) / "runs" / plan["id"]
    registration = folder / "registration.json"
    view = dict(registration_id=plan["id"], scope="entire_registration_all_tasks_and_attempts",
                status="not_registered", stages=None, total=None, final_actual_usd=None)
    if not registration.is_file():
        return view
    record = read(registration)["preflight"]
    require(record["adapter"] == plan["benchmark"]["adapter"] and record["plan_sha256"] == plan_digest(plan),
            "Cost registration differs from the analyzed plan")
    receipts = {stage: [] for stage in ("source", "write", "solve")}
    closure_path = folder / "completion.json"
    closure = read(closure_path) if closure_path.is_file() else None
    bfcl = plan["benchmark"]["adapter"] == "bfcl-relevance"
    if closure is not None:
        recorded_hash = closure["files"]["registration.json"] if bfcl else closure["registration_sha256"]
        require(recorded_hash == digest(registration.read_bytes()), "Cost completion belongs to another registration")
    if bfcl:
        _bfcl_receipts(folder, closure, receipts)
        view.update(coverage="request_reservations_to_date", request_basis="pre_submission_reservations")
    else:
        unsealed = _tau_receipts(folder, plan, record, closure, receipts)
        view.update(coverage="sealed_stage_ledgers_only", request_basis="sealed_stage_http_calls", unsealed_shards=unsealed)
    total = _totals([receipt for stage in receipts.values() for receipt in stage])
    if closure is not None:
        reported, held = Decimal(total["reported_usd"]), Decimal(total["held_usd"])
        if bfcl:
            summary = closure["summary"]
            require((reported, held, total["requests"]) == (
                Decimal(summary["reported_usd"]), Decimal(summary["reserved_usd"]), summary["requests"]),
                "BFCL expense totals differ from the completed registration")
        else:
            require(reported + held == Decimal(closure["total_covered_usd"]),
                    "Stage spending differs from the completed registration total")
    continuation = folder / "write-continuation.json"
    if continuation.is_file() and not bfcl:
        from agent_memory_eval.study.writer_stage import active_writer_sources
        _, meta = active_writer_sources(Path(workspace), folder, plan)
        for ref in meta["source_recovery_checkpoints"]:
            check = read_json(asset_bytes(Path(workspace), ref))
            _tau_receipts(safe_path(Path(workspace), ref["path"]).parent, plan, record, check, receipts,
                          source_supplement=True)
        require(sum(r.reported + r.held for r in receipts["source"]) == Decimal(meta["prior_source_covered_usd"]),
                "Original and recovered source spending differs from the writer release")
        total = _totals([receipt for stage in receipts.values() for receipt in stage])
    if closure is not None and Decimal(total["held_usd"]) == 0 and total["unknown_price_requests"] == 0:
        view["final_actual_usd"] = total["reported_usd"]
    view.update(status="sealed" if closure is not None else "open",
                stages={stage: _totals(events) for stage, events in receipts.items()}, total=total)
    return view
