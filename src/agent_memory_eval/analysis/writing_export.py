"""Read-only writing audit, descriptive metrics and condition-hidden content export."""

from collections import Counter
from decimal import Decimal
import json
from pathlib import Path

from agent_memory_eval.analysis.writing_exploration import audit_writing
from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.journal import BlobStore, read_experiment


def collect(root):
    """Build the views in memory; safe inside the historical read-only bootstrap."""
    root = Path(root)
    audit = audit_writing(root)
    scan, state, _ = read_experiment(root)
    blobs = BlobStore(root)
    runtime = state["writing_exploration"]
    control = blobs.get(runtime["control_ref"])
    rows = []
    for result in runtime["results"]:
        run = result["run_id"]
        events = [e for e in scan["events"].values() if e["run_id"] == run]
        finishes = [e["payload"] for e in events if e["type"] == "model.request_finished"]
        raw = [blobs.get(e["response_ref"]) for e in finishes if e["status"] == "response"]
        items = snapshot_items(blobs, state, result["snapshot_id"]) if result["status"] != "skipped" else []
        stores = [call for value in raw for call in value["choices"][0]["message"].get("tool_calls") or []
                  if call["function"]["name"] == "memory_store"]
        cost = sum((Decimal(str(e["cost_usd"])) for e in finishes if e["cost_status"] == "observed"), Decimal(0))
        usages = [e["usage"] for e in finishes if isinstance(e.get("usage"), dict)]
        reason = [details.get("reasoning_tokens") for u in usages
                  for details in [u.get("completion_tokens_details")] if isinstance(details, dict)]
        reason = [v for v in reason if type(v) is int]
        rows.append(dict(run_id=run, feedback_level=result["feedback_level"], repetition=result["repetition"],
            status=result["status"], error_code=result.get("error_code"), skip_reason=result.get("reason"),
            snapshot_id=result.get("snapshot_id"), memory_stores=result.get("memory_stores", 0),
            proposed_store_calls=len(stores), final_items=len(items), items=items,
            payload_bytes=sum(len(canonical({k:item[k] for k in ("title", "body")})) for item in items),
            requests=len(finishes), reported_cost_usd=str(cost),
            unknown_cost_requests=sum(e["cost_status"] == "unknown" for e in finishes),
            prompt_tokens=sum(u.get("prompt_tokens", 0) for u in usages),
            completion_tokens=sum(u.get("completion_tokens", 0) for u in usages),
            reasoning_tokens=sum(reason) if reason else None, reasoning_usage_records=len(reason),
            finish_reasons=dict(Counter(str(v["choices"][0].get("finish_reason")) for v in raw)),
            replay_eligible=result.get("replay_eligible", False)))
    assert sum(r["requests"] for r in rows) == audit["request_audit"]["requests"]
    assert sum((Decimal(r["reported_cost_usd"]) for r in rows), Decimal(0)) == Decimal(audit["request_audit"]["observed_cost_usd_decimal"])
    groups = {}
    for level in control["profile"]["feedback_levels"]:
        group = [r for r in rows if r["feedback_level"] == level]
        groups[level] = dict(allocations=len(group), states=dict(Counter(r["status"] for r in group)),
            nonempty_banks=sum(r["final_items"] > 0 for r in group), final_items=sum(r["final_items"] for r in group),
            memory_stores=sum(r["memory_stores"] for r in group), proposed_store_calls=sum(r["proposed_store_calls"] for r in group),
            payload_bytes=sum(r["payload_bytes"] for r in group), requests=sum(r["requests"] for r in group),
            reported_cost_usd=str(sum((Decimal(r["reported_cost_usd"]) for r in group), Decimal(0))),
            unknown_cost_requests=sum(r["unknown_cost_requests"] for r in group))
    ordered = sorted(rows, key=lambda row:digest(("memory-writing-review-order/v1:" + row["run_id"]).encode()))
    blind, mapping = [], {}
    for index, row in enumerate(ordered, 1):
        case = f"W{index:02d}"
        blind.append(dict(case_id=case, items=[{k:item[k] for k in ("title", "body")} for item in row["items"]]))
        mapping[case] = {k:row[k] for k in ("run_id", "feedback_level", "repetition", "status", "snapshot_id")}
    first = next(e for e in scan["events"].values() if e["type"] == "model.request_started" and e["payload"]["actor_role"] == "writer")
    context = json.loads(blobs.get(first["payload"]["request_ref"])["messages"][1]["content"])
    exports = dict(blind=dict(format="agent-memory-writing-blind/v1", cases=blind),
        mapping=dict(format="agent-memory-writing-review-map/v1", cases=mapping),
        metrics=dict(format="agent-memory-writing-descriptive/v1", audit=audit,
            run_log_sha256=digest((root / "log.jsonl").read_bytes()), control_ref=runtime["control_ref"],
            implementation=control["implementation"], profile=control["profile"], model=control["model"],
            by_feedback=groups, runs=rows, scope="descriptive_writing_observation_no_memory_effect_measurement"),
        public_context=context)
    return exports


def export(root, output):
    root, output = Path(root), Path(output)
    if output.exists() or output.resolve().is_relative_to(root.resolve()):
        raise ValueError("Use a new export directory outside the immutable run")
    exports = collect(root)
    output.mkdir(parents=True)
    for name, value in exports.items():
        (output / (name + ".json")).write_bytes(canonical(value) + b"\n")
    return dict(status="exported", cases=len(exports["metrics"]["runs"]), output=str(output))
