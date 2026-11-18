"""Import and recheck every source bank and decision used in a written-memory batch."""

from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.written_comparison import case_packet, validate_rubrics
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import inspect_journal
from agent_memory_eval.experiments.replay_schedule import input_groups, local_snapshot, validate_profile
from agent_memory_eval.experiments.writing_comparison import (
    PROFILE_V2, _note, _profile, audit_comparison, comparison_requests,
)
from agent_memory_eval.memory.imports import copy_source_blobs, install_snapshot, validate_source
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.memory.writing import public_message
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.experience import ReplayContextReader, _source, capture_recorded_contexts
from agent_memory_eval.recording.journal import safe_path


def load_bundle(profile, root, preparation):
    validate_profile(profile)
    comparison_text = asset_text(root, profile["comparison_ref"])
    comparison = read_json(comparison_text.encode())
    if comparison["format"] != PROFILE_V2:
        raise ContractError("Batch execution requires the source-verified v2 preparation")
    rubrics_text = asset_text(root, profile["rubrics_ref"])
    instruction = asset_text(root, comparison["replay_instruction_ref"])
    receipt_text = asset_text(root, comparison["writing_result_ref"])
    raw = safe_path(Path(preparation), "preparation.json").read_bytes()
    manifest = read_json(raw)
    if digest(canonical(manifest)) != profile["preparation_sha256"] or raw != canonical(manifest) + b"\n":
        raise ContractError("Preparation differs from the batch profile")
    audit_comparison(comparison, root, preparation)
    return dict(comparison_text=comparison_text, rubrics_text=rubrics_text, instruction=instruction,
                receipt_text=receipt_text, manifest=manifest)


def import_inputs(journal, profile, bundle, root):
    manifest = bundle["manifest"]
    comparison = read_json(bundle["comparison_text"].encode())
    receipt = read_json(bundle["receipt_text"].encode())
    policy = _profile(comparison)
    memory = MemoryService(journal, record_kind="observed")
    source_root = safe_path(root, receipt["source_root"])
    writers = {}
    with _source(source_root, receipt["raw_log_sha256"]) as (scan, blobs, data, _):
        source_state = blobs.get(scan["records"][-1]["state_ref"])
        copy_source_blobs(source_root, journal)
        writing_ref = journal.blobs.put(data.decode())
        for row in manifest["writing_allocations"]:
            snapshot = source_state["snapshots"][row["snapshot_id"]]
            writers[row["run_id"]] = install_snapshot(memory, policy, snapshot["heads"], snapshot["order"],
                snapshot["head_revision"], source_state["versions"], owner_run="import_writer_" + row["run_id"])
    controls = {"empty": install_snapshot(memory, policy, {}, [], 0, {}, owner_run="control_empty")}
    for arm, memo in comparison["control_notes"].items():
        item = _note(memo)
        controls[arm] = install_snapshot(memory, policy, {item["id"]:item["version"]}, [item["id"]], 0,
                                         {item["version"]:journal.blobs.put(item)}, owner_run="control_" + arm)
    source = comparison["decision_source"]
    cases = comparison["cases"]
    refs = capture_recorded_contexts(journal, safe_path(root, source["path"]),
        [(c["run_id"], c["request_id"]) for c in cases], log_sha256=source["log_sha256"])
    return dict(profile=profile, catalog=dict(writers=writers, controls=controls),
        decisions={c["case_id"]:ref for c, ref in zip(cases, refs, strict=True)},
        writing_log_ref=writing_ref, manifest_ref=journal.blobs.put(manifest),
        **{key:bundle[key] for key in ("comparison_text", "receipt_text", "rubrics_text", "instruction")})


def _check_snapshot(blobs, state, producers, snapshot_id, items, revision, policy, owner):
    snap = state["snapshots"][snapshot_id]
    bank = state["banks"][snap["bank_id"]]
    producer = producers[snapshot_id]
    order = [item["id"] for item in items]
    heads = {item["id"]:item["version"] for item in items}
    if (blobs.get(producer["payload"]["manifest_ref"]) != snap or producer["record_kind"] != "observed"
            or snap != dict(snapshot_id=snapshot_id, bank_id=snap["bank_id"], head_revision=revision,
                            heads=heads, order=order, parent_snapshot_ids=[])
            or bank["owner_run"] != owner or not bank["sealed"] or bank["parent_snapshot"] is not None or bank["policy"] != asdict(policy)
            or bank["revision"] != revision or any(bank[k] != heads for k in ("heads", "start_heads"))
            or any(bank[k] != order for k in ("order", "start_order"))
            or snapshot_items(blobs, state, snapshot_id) != items):
        raise ContractError("Local input snapshot differs from its source, producer or frozen bank")


def _check_writers(root, blobs, state, control, manifest, receipt, policy, producers):
    data = blobs.get(control["writing_log_ref"]).encode()
    if digest(data) != manifest["writing_log_sha256"] or digest(data) != receipt["raw_log_sha256"]:
        raise ContractError("Writing log differs from its receipt or preparation")
    scan = inspect_journal(data)
    seen = set()
    for record in scan["records"]:
        blobs.verify(record, seen)
    source = blobs.get(scan["records"][-1]["state_ref"])
    results = source["writing_exploration"]["results"]
    rows = manifest["writing_allocations"]
    catalog = control["catalog"]
    if (source["writing_exploration"]["status"] != "completed" or len(rows) != len(results)
            or len(rows) != receipt["audit"]["allocations"] or len(catalog["writers"]) != len(rows)
            or set(catalog["writers"]) != {r["run_id"] for r in rows}):
        raise ContractError("Batch is missing an original writing allocation")
    total_cost, total_calls, unknown = Decimal(0), 0, 0
    for row, result in zip(rows, results, strict=True):
        run = row["run_id"]
        if (any(row[key] != result[key] for key in ("run_id", "feedback_level", "repetition", "snapshot_id",
                                                     "status", "replay_eligible"))
                or not row["replay_eligible"] or row["status"] not in {"completed", "empty"}):
            raise ContractError("Writing assignment differs from source or is incomplete")
        events = [event for event in scan["events"].values() if event["run_id"] == run]
        ends = [e["payload"] for e in events if e["type"] == "run.ended"]
        if (len(ends) != 1 or ends[0]["termination_reason"] != "memory_write_" + row["status"]
                or ends[0]["end_snapshot_id"] != row["snapshot_id"] or (row["status"] == "empty") != (not row["items"])):
            raise ContractError("Writing status or snapshot differs from the original run ending")
        snap = validate_source(root, dict(snapshot_id=row["snapshot_id"], source_run_id=run,
            experiment_id=scan["records"][0]["experiment_id"], log_sha256=manifest["writing_log_sha256"]),
            scan, source, data, allow_empty=True)
        if (snapshot_items(blobs, source, row["snapshot_id"]) != row["items"]
                or any(state["versions"][v] != source["versions"][v] for v in snap["heads"].values())):
            raise ContractError("Imported writer item lost its original immutable version")
        _check_snapshot(blobs, state, producers, catalog["writers"][run], row["items"], snap["head_revision"], policy,
                        "import_writer_" + run)
        finishes = [e["payload"] for e in events if e["type"] == "model.request_finished"]
        cost = sum((Decimal(str(e["cost_usd"])) for e in finishes if e["cost_status"] == "observed"), Decimal(0))
        missing = sum(e["cost_status"] == "unknown" for e in finishes)
        if (len(finishes), cost, missing) != (row["requests"], Decimal(row["reported_cost_usd"]), row["unknown_cost_requests"]):
            raise ContractError("Writing cost or request count differs from recorded source usage")
        total_calls += len(finishes)
        total_cost += cost
        unknown += missing
    return dict(requests=total_calls, reported_cost_usd_decimal=str(total_cost), unknown_cost_requests=unknown)


def verify_inputs(root, blobs, state, events, control):
    """Portable reconstruction from the embedded, hash-bound sources. No external folders or HTTP."""
    profile = control["profile"]
    pending = validate_profile(profile)
    comparison = read_json(control["comparison_text"].encode())
    rubrics = read_json(control["rubrics_text"].encode())
    receipt = read_json(control["receipt_text"].encode())
    for text, ref in ((control["comparison_text"], profile["comparison_ref"]), (control["rubrics_text"], profile["rubrics_ref"]),
                      (control["instruction"], comparison["replay_instruction_ref"]), (control["receipt_text"], comparison["writing_result_ref"])):
        if digest(text.encode()) != ref["sha256"]:
            raise ContractError("Archived protocol asset differs from its pinned file")
    manifest = blobs.get(control["manifest_ref"])
    if (control["manifest_ref"]["sha256"] != profile["preparation_sha256"] or manifest["profile"] != comparison
            or manifest["format"] != "agent-memory-written-replay-preparation/v2" or comparison["format"] != PROFILE_V2
            or manifest["decision_log_sha256"] != comparison["decision_source"]["log_sha256"]):
        raise ContractError("Archived preparation differs from the pinned comparison")
    policy = _profile(comparison)
    catalog = control["catalog"]
    local = [*catalog["writers"].values(), *catalog["controls"].values()]
    producers = {e["payload"]["snapshot_id"]:e for e in events.values() if e["type"] == "bank.snapshot_created"}
    if len(local) != len(set(local)) or set(catalog["controls"]) != {"empty", "generic", "researcher"}:
        raise ContractError("Every writing and control allocation requires its own source bank")
    writer_cost = _check_writers(root, blobs, state, control, manifest, receipt, policy, producers)
    for arm, snap in catalog["controls"].items():
        items = [] if arm == "empty" else [_note(comparison["control_notes"][arm])]
        _check_snapshot(blobs, state, producers, snap, items, 0, policy, "control_" + arm)
        if any(blobs.get(state["versions"][i["version"]]) != i for i in items):
            raise ContractError("Controller memo version includes unapproved fields")
    reader = ReplayContextReader()
    cases, models = [], []
    if set(control["decisions"]) != {c["case_id"] for c in comparison["cases"]}:
        raise ContractError("Decision capsules do not cover the reviewed cases")
    for case, declared in zip(manifest["candidate_cases"], comparison["cases"], strict=True):
        source = reader.read(blobs, control["decisions"][case["case_id"]])
        archive, wire = source["archive"], source["wire"]
        if (any(case[k] != v for k, v in declared.items()) or archive["source_run_id"] != case["run_id"]
                or archive["source_log_sha256"] != manifest["decision_log_sha256"] or archive["record_kind"] != "observed"
                or any(archive[k] != case[k] for k in ("request_ref", "response_ref", "request_event_id", "response_event_id"))
                or public_message(source["response"]["choices"][0]["message"]) != case["original_public_reply"]):
            raise ContractError("Decision capsule differs from the reviewed source")
        output_caps = [wire[k] for k in ("max_completion_tokens", "max_tokens") if k in wire]
        if wire.get("stream") is not False or len(output_caps) != 1 or type(output_caps[0]) is not int or output_caps[0] < 1:
            raise ContractError("Worker inputs require bounded output and non-streaming transport")
        cases.append(dict(case, wire=wire))
        models.append(dict(model=wire["model"], provider=wire["provider"],
            parameters=sorted(set(wire) - {"model", "messages", "provider", "stream"})))
    model = models[0]
    if any(m != model for m in models) or not model.get("model") or not model.get("provider"):
        raise ContractError("Batch requires one worker model, provider route and supported parameter set")
    packet = case_packet(cases, comparison)
    if manifest["case_review_packet"] != packet or digest(canonical(packet)) != manifest["case_review_packet_sha256"]:
        raise ContractError("Review packet differs from the archived public decisions")
    validate_rubrics(packet, rubrics)
    memo_review = profile["control_note_review"]
    if memo_review is not None and memo_review["packet_sha256"] != manifest["case_review_packet_sha256"]:
        raise ContractError("Control-note review belongs to a different public packet")
    reviewed = rubrics["status"] == "reviewed" and rubrics["reviewer"]["independence_declared"]
    if profile["status"] == "ready" and not reviewed:
        raise ContractError("The ready protocol requires reviewed case rubrics and declared independent review")
    if not reviewed:
        pending.append("independent_case_review")
    rows, requests = comparison_requests(cases, manifest["writing_allocations"], policy, control["instruction"],
        comparison["control_notes"], max_input_bytes=comparison["delivery"]["max_input_bytes"])
    if rows != manifest["rows"] or len(requests) != manifest["unique_request_payloads"]:
        raise ContractError("Archived inputs do not reproduce the prepared four-arm assignments")
    groups = input_groups(rows)
    for group in groups:
        group["members"] = [dict(member, local_input_snapshot_id=local_snapshot(catalog, member),
                                 representative=index == 0) for index, member in enumerate(group["members"])]
    largest = max(r["input_bytes"] for r in rows)
    if profile["max_input_bytes"] is not None and largest > profile["max_input_bytes"]:
        raise ContractError("The input-byte cap excludes a prepared arm")
    if profile["bank_limits"] is not None:
        for snap in local:
            items = snapshot_items(blobs, state, snap)
            sizes = dict(max_items=len(items), max_return_bytes=len(canonical(dict(items=items))),
                max_payload_bytes=sum(len(canonical({k:i[k] for k in ("title", "body")})) for i in items))
            if any(cap is not None and sizes[key] > cap for key, cap in profile["bank_limits"].items()):
                raise ContractError("The declared bank limits exclude a prepared arm")
    summary = dict(cases=len(cases), writer_allocations=len(catalog["writers"]),
        empty_writer_allocations=sum(r["status"] == "empty" for r in manifest["writing_allocations"]),
        input_snapshots=len(local), logical_rows_per_repetition=len(rows), distinct_calls_per_repetition=len(groups),
        groups_per_case=dict(Counter(g["case_id"] for g in groups)), input_bytes=manifest["input_bytes"],
        groups_by_logical_row_count=dict(sorted(Counter(str(len(g["members"])) for g in groups).items())),
        source_model_seed_by_case={c["case_id"]:c["wire"].get("seed") for c in cases},
        planned_calls=None if profile["repetitions"] is None else len(groups) * profile["repetitions"],
        unresolved_fields=pending, historical_writing_usage=writer_cost)
    return dict(groups=groups, packet=packet, rubrics=rubrics, model_config=model, policy=policy, summary=summary)
