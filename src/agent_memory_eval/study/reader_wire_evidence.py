"""Offline evidence for a stopped τ² cohort reader with one denied, redundantly encoded call."""

import json
from decimal import Decimal
from pathlib import Path
import re

from agent_memory_eval.adapters.tau2_memory import _agreeing_pairs
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.experiments.matched_trial import source_receipt
from agent_memory_eval.models.openrouter import request_reservation_usd
from agent_memory_eval.recording.assets import asset_bytes, asset_text
from agent_memory_eval.recording.batch import usage_from_events
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.recording.provenance import public_assistant_message
from agent_memory_eval.study.adapters.tau_cohort import verify_reader_pool
from agent_memory_eval.study.records import first_pass_shard, read, sealed_rows, shard_seal, source_from
from agent_memory_eval.study.writer_stage import active_writer_sources


def _duplicate_arguments(text):
    """Accept only a JSON object whose duplicate keys all have identical JSON values."""
    try:
        read_json(text.encode())
    except ValueError:
        value = json.loads(text, object_pairs_hook=_agreeing_pairs)
        require(isinstance(value, dict), "Expected a function argument object")
        canonical(value)  # Reject non-finite numeric values anywhere in this object.
        return value
    raise ContractError("No identical duplicate in the provider's function arguments")


def _events(log, kind, origin=None):
    return [e for e in log.of_type(kind) if origin is None or e["payload"].get("origin") == origin]


def _project(message, *, native):
    """Business fields in one provider/native history message, with strict parsing of prior calls."""
    role = message["role"]
    calls = []
    for call in message.get("tool_calls") or []:
        if native:
            calls.append([call["id"], call["name"], call["arguments"]])
        else:
            fn = call["function"]
            calls.append([call["id"], fn["name"], read_json(fn["arguments"].encode())])
    tool_id = message.get("id" if native else "tool_call_id") if role == "tool" else None
    return [role, message.get("content"), tool_id, calls]


def _denied_response(log, result):
    first, last = log.requests[0], log.requests[-1]
    require(first["actor_role"] == last["actor_role"] == "worker", "Expected worker opening and terminal worker request")
    rid = last["request_id"]
    finished = _events(log, "model.request_finished")
    finish = finished[-1]
    require(finish["payload"]["request_id"] == rid and finish["payload"]["delivery_status"] == "response_received"
            and log.finishes[first["request_id"]]["delivery_status"] == "response_received"
            and log.finishes[first["request_id"]]["status"] == "response",
            "Missing confirmed worker request or a later provider request was sent")
    response = log.reply(last)["message"]
    public = public_assistant_message(response)
    call, = public["tool_calls"]
    cid, fn = call["id"], call["function"]
    parsed = _duplicate_arguments(fn["arguments"])
    require(cid and not fn["name"].startswith("memory_"), "Expected a denied business call")
    require(log.state["batches"][log.run + ":" + rid] == dict(
        status="finished", calls_sha256=digest(canonical(response["tool_calls"]))),
        "Router dispatch is not bound to the observed provider response")

    # Only the narrow observed sequence qualifies. In particular there is no follow-up send, native
    # business operation, successful tool response, or unrelated error between this reply and closure.
    tail = [e for e in log.all_events.values() if e["event_seq"] > finish["event_seq"]]
    require([(e["type"], e["payload"].get("origin")) for e in tail] == [
        ("business.transition", None), ("business.transition", "tau2_worker_output"), ("policy.denied", None),
        ("business.transition", "tau2_memory_result"), ("business.transition", "tau2_memory_dispatch"),
        ("runtime.error", None), ("business.transition", "forms.native_result"), ("outcome.scored", None),
        ("bank.snapshot_created", None), ("run.ended", None)], "Other activity or missing evidence after the worker response")
    elapsed, emitted, denied, returned, dispatch, error, outcome, score, _, end = [e["payload"] for e in tail]
    require((elapsed["actor_role"], elapsed["request_id"]) == ("worker", rid)
            and log.blobs.get(emitted["public_message_ref"]) == public
            and (denied["tool"], denied["code"]) == (fn["name"], "INVALID_ARGUMENT")
            and denied["tool_result_ref"] == returned["public_message_ref"], "Worker call and actual denial differ")
    reply = log.blobs.get(denied["tool_result_ref"])
    require(reply == dict(role="tool", tool_call_id=cid, content=canonical(
        dict(status="error", error=dict(code="INVALID_ARGUMENT"))).decode()), "Missing matching public denial receipt")
    require(dispatch["request_id"] == rid and dispatch["call_order"] == [cid] and dispatch["memory_only"] is False
            and dispatch["native_step_before"] == dispatch["native_step_after"]
            and dispatch["native_errors_before"] == dispatch["native_errors_after"],
            "The rejected call advanced the native environment or belongs to another dispatch")
    require(_events(log, "runtime.error") == [tail[5]] and error["code"] == "FIRST_PASS_MISMATCH"
            and _events(log, "outcome.scored") == [tail[7]] and _events(log, "run.ended") == [tail[9]]
            and end["termination_reason"] == outcome["termination_reason"] == "runtime_error"
            and score == dict(grading_status="incomplete", official_artifact_ref=None, official_score=None,
                              official_success=None) and end["grading_status"] == "incomplete"
            and all(outcome[k] == result[k] for k in (
                "error_code", "partial_trajectory_ref", "native_report_ref", "native_reward", "native_steps",
                "native_tool_errors", "final_db_hash", "termination_reason", "reached_state_assessment_ref")),
            "Mismatch must be the sole terminal error and have no official grade or native report")

    trajectory = log.blobs.get(outcome["partial_trajectory_ref"])
    native_call, native_reply = trajectory[-2:]
    require(canonical(_project(native_call, native=True)) == canonical(["assistant", public.get("content"), None, [[cid, fn["name"], parsed]]])
            and _project(native_reply, native=True) == ["tool", reply["content"], cid, []]
            and native_reply["error"] is True, "Native partial trajectory and rejected provider call disagree")
    wire = log.blobs.get(last["request_ref"])
    initial = log.blobs.get(first["request_ref"])
    offset = 1 + int(log.state["assignments"][log.run]["policy"]["read_mode"] == "preload")
    require({k: v for k, v in wire.items() if k != "messages"} == {k: v for k, v in initial.items() if k != "messages"}
            and wire["messages"][:offset] == initial["messages"][:offset]
            and canonical([_project(m, native=False) for m in wire["messages"][offset:]]) ==
                canonical([_project(m, native=True) for m in trajectory[:-2]]),
            "Another worker setting or public history field differs before the rejected call")
    return first["request_ref"]["sha256"]


def _inputs(shard, control, failed_row, log):
    """Recover the original customer's first worker wire, and the frozen writer snapshots for this shard."""
    run_root = shard.parent.parent
    workspace = run_root.parent.parent
    batch = read(run_root / "reader-batches/own-mixed-readers-final-01/registration.json")
    spec = read_json(asset_bytes(ROOT, batch["batch_ref"]))
    plan = read_json(asset_bytes(ROOT, spec["plans"][run_root.name]["plan"]))
    registration = read_json(asset_bytes(run_root, dict(path="registration.json", sha256=batch["source_registration_sha256"])))
    record = registration["preflight"]
    profile = control["profile"]
    tid = failed_row["task_id"]
    case, = [c for c in record["cases"] if c["task"] == profile["case"]]
    require(tid in batch["tasks"] and batch["plan_id"] == profile["name"] == run_root.name == plan["id"]
            and profile["registration_sha256"] == digest(canonical(record))
            and case["opening"]["task"] == case["donor"]["task"] and case["task_id"] == tid
            and case["native_sha256"] == control["preflight"]["native_sha256"], "Different target, source or native control")
    sources, _ = active_writer_sources(workspace, run_root, plan)
    source_dir, binding = first_pass_shard(workspace, run_root, case["opening"], sources)
    source = source_from(source_dir, tid, binding=binding)
    require(source is not None and type(source.official_success) is bool and
            control["source_outcomes"] == {tid: source.official_success}, "The original source has no matching official result")
    reference, = control["design"]["source"]["cohort"]
    require((reference["task_id"], reference["run_id"], reference["source_log_sha256"], reference["public_context_sha256"],
             reference["result_feedback"]) == (tid, source.source_run_id, source.source_log_sha256,
             source.public_context_sha256, source.result_receipt()), "Different original first-pass source or feedback")
    original, = sealed_rows(source_dir, binding["seal"])[2]
    receipt = source_receipt(safe_path(source_dir, original["directory"]), source,
                             dict(case["opening"], registration_sha256=profile["registration_sha256"]))
    opening, = _events(log, "business.transition", "task_start.first_decision")
    native, = _events(log, "business.transition", "matched_trial.native_control")
    first, = log.of_type("run.started")
    p = opening["payload"]
    require(first["payload"]["fresh_session"] and first["event_seq"] < native["event_seq"] < opening["event_seq"] <
            log.starts[log.requests[0]["request_id"]][0]["event_seq"] and
            (p["source_run_id"], p["source_log_sha256"], p["source_worker_request_id"], log.blobs.get(p["task_start_ref"])) ==
            (receipt["run_id"], receipt["log_sha256"], receipt["first_worker_request_id"], receipt["task_start"]),
            "Reader opening differs from the worker input actually submitted in the registered original first pass")

    checkpoint = read_json(asset_bytes(run_root, dict(path="through-write.json", sha256=batch["writer_checkpoint_sha256"])))
    cases = {c["task_id"]: c["task"] for c in record["cases"] if "write" in c["scheduled_stages"]}
    cache = {}

    def pool(arm):
        delivery = control["design"]["delivery_arms"][arm]
        form = case["arm_writer"][arm]
        tids = [tid] if case["contents"][arm] == "own" else case["pool_sources"]
        require((delivery["source_tasks"], delivery["writer"], delivery["pool"], delivery["read_mode"]) ==
                (tids, form, case["contents"][arm], case["arm_mode"][arm]), "Reader assignment changed")
        if "sources_sha256" not in delivery:
            return None  # The stop may mask unavailable pools later in this native shard.
        banks = []
        for source_id in tids:
            key = (source_id, form)
            if key not in cache:
                name = "task-" + cases[source_id] + "/write"
                directory = safe_path(run_root, name)
                _, _, rows = sealed_rows(directory, checkpoint["stages"][name]["seal"])
                row, = [r for r in rows if r["arm"] == form]
                require(row["status"] == "completed", "Reader pool includes a missing writer")
                sha = row["result"]["log_sha256"]
                bank = read_form_bank(safe_path(directory, row["directory"]), expected_log_sha256=sha)
                source_entry = sources[cases[source_id]]
                require(bank["items"] and bank["status"] == "completed" and row["run_id"] == bank["run_id"]
                        and (bank["task_id"], bank["source_run_id"], bank["public_context_sha256"]) ==
                        (source_id, source_entry["run_id"], source_entry["public_context_sha256"]),
                        "Writer bank has no available memory or belongs to a different source")
                cache[key] = dict(bank, journal_sha256=sha)
            banks.append(cache[key])
        require(digest(canonical(banks)) == delivery["sources_sha256"], "Changed ordered writer pool")
        return banks

    arm = failed_row["arm"]
    policy = plan["reading"][arm.split("__")[1]]
    delivery = control["design"]["delivery_arms"][arm]
    require(delivery.get("tool_descriptions") == policy.get("tool_descriptions"), "Reader wording differs from its plan")
    verify_reader_pool(log, failed_row, dict(pool=pool(arm), delivery=delivery, registration=profile["registration_sha256"],
        native_sha256=case["native_sha256"], descriptions=policy.get("tool_descriptions"),
        instruction=asset_text(ROOT, policy["notice"]) if "notice" in policy else ""))
    return {arm for arm in control["design"]["delivery_arms"] if pool(arm)}


def _charges(log, result, models):
    events = list(log.events.values())
    starts, finishes = log.of_type("model.request_started"), log.of_type("model.request_finished")
    require(len(starts) == len(finishes) == len(log.starts) == len(log.finishes)
            and log.starts.keys() == log.finishes.keys() and not result.get("pending_request_ids")
            and all(e["record_kind"] == "observed" and e["condition_id"] == result["arm"]
                    and e["session_id"] == result["session_id"] for e in events),
            "Unexpected session, duplicate request, or unconfirmed submission")
    for f in log.finishes.values():
        if f["status"] == "response":
            usage = log.blobs.get(f["response_ref"])["usage"]
            require(usage == f["usage"] and f["cost_status"] == "observed" and usage["cost"] == f["cost_usd"],
                    "Response usage or price differs from the original provider receipt")
    billing = usage_from_events(events)
    reported = sum((Decimal(v["reported_usd"]) for v in billing.values()), Decimal(0))
    http = sum(v["http_calls"] for v in billing.values())
    unknown = [p for p in log.finishes.values() if p["cost_status"] == "unknown"]
    require(billing == result["billing_by_role"] and http == result["http_calls"]
            and reported == Decimal(result["reported_cost_usd_decimal"])
            and result["unknown_cost"] is bool(unknown), "Reader billing differs from the recorded provider requests")
    reservation = result.get("unpriced_reservation")
    if not unknown:
        require(not reservation, "Unexplained held reader spending")
        return http, reported, Decimal(0)
    require(len(unknown) == 1 and reservation, "Unsettled reader spending without a priced reservation")
    f, = unknown
    req = log.starts[f["request_id"]][0]["payload"]
    role = req["actor_role"]
    held = request_reservation_usd(log.blobs.get(req["request_ref"]), models[role])
    code = (log.blobs.get(f["response_ref"])["error"]["code"] if f["response_ref"] else "TRANSPORT_UNKNOWN")
    require(reservation == dict(request_id=req["request_id"], code=code, role=role, usd=str(held),
                                request_ref=req["request_ref"], response_ref=f["response_ref"]),
            "Unpriced request does not match its original model, wire or reserve")
    return http, reported, held


def verified_recovery(shard, *, expected_seal=None):
    """Return eligible existing allocation indices. Never create, mark or reopen a run."""
    shard = Path(shard).absolute()
    seal = shard_seal(shard)
    require(expected_seal is None or expected_seal == seal, "Different expected shard seal")
    close, control, rows = sealed_rows(shard, seal)
    design, profile = control["design"], control["profile"]
    require(re.fullmatch(r"solve(?:-retry-[1-9][0-9]*)?", shard.name)
            and profile["stage"] == design["stage"] == "solve" and shard.parent.name == "task-" + profile["case"]
            and close["stop_reason"] == "first_pass_mismatch" and design["reader_admission"] == "sealed_bank",
            "Expected a sealed reader shard stopped by FIRST_PASS_MISMATCH")
    jobs = [{k: row[k] for k in ("index", "repetition", "task_id", "phase", "arm", "source_task_id")} for row in rows]
    require(digest(canonical(jobs)) == design["allocation"]["jobs_sha256"] == control["preflight"]["jobs_sha256"]
            and [r["index"] for r in rows] == sorted({r["index"] for r in rows})
            and len({r["arm"] for r in rows}) == len({r["run_id"] for r in rows}) == len(rows),
            "Duplicate or changed original solve allocations")

    failed, unstarted, directories, available = {}, {}, set(), set()
    http, reported, held = 0, Decimal(0), Decimal(0)
    for row in rows:
        arm = row["arm"]
        require(row["directory"] == f'jobs/{row["index"]:04d}' and row["phase"] == "solve"
                and row["task_id"] == row["source_task_id"], "Unexpected reader allocation identity")
        folder = safe_path(shard, row["directory"])
        result = row.get("result")
        if row["status"] == "not_started":
            require("result" not in row and not folder.exists(), "Never-submitted arm has a log, charge, or result")
            if row.get("reason") == "first_pass_mismatch":
                require(failed, "Unstarted arm precedes the actual mismatch")
                if arm in available:
                    unstarted[arm] = row["index"]
            else:
                require(not failed and row.get("reason") != "task_start_mismatch", "Different stop in the suffix")
            continue
        require(not failed and result and row["status"] == result["status"] and row["run_id"] == result["run_id"]
                and arm == result["arm"] and row["task_id"] == result["task_id"], "Executed or changed arm after the stop")
        directories.add(folder.name)
        log = RecordedDecisions(folder, row["run_id"], result["log_sha256"])
        require(set(log.scan["starts"]) == {log.run}, "An unexpected run shares the job journal")
        calls, cost, reserve = _charges(log, result, control["model_catalog_entries"])
        http, reported, held = http + calls, reported + cost, held + reserve
        require(result.get("error_code") != "TASK_START_MISMATCH", "Original task opening changed")
        if result.get("error_code") == "FIRST_PASS_MISMATCH":
            require(row["status"] == "error" and result["grading_status"] == "incomplete" and
                    all(result[k] is None for k in ("official_success", "official_score", "report_ref",
                                                      "native_reward", "native_report_ref", "reached_state_assessment_ref"))
                    and not result["unknown_cost"], "Reader is graded, unpriced or has another terminal native result")
            first_wire = _denied_response(log, result)
            require(first_wire == result["first_worker_request_sha256"], "Reader opening wire digest differs")
            available = _inputs(shard, control, row, log)
            failed[arm] = dict(index=row["index"], run_id=log.run, log_sha256=result["log_sha256"],
                               first_worker_request_sha256=first_wire)

    require(failed and {p.name for p in (shard / "jobs").iterdir()} == directories,
            "Missing mismatch or unexpected physical job directory")
    require(close["requests_with_unconfirmed_submission"] == 0 and close["http_calls"] == http
            and reported == Decimal(close["reported_usd_decimal"]) and float(reported) == close["reported_usd"]
            and held == Decimal(close["reserved_usd"]) and close["unknown_cost"] is bool(held)
            and reported + held <= Decimal(str(profile["guards"]["max_usd"])), "Sealed spending or held amount differs")
    return dict(seal=seal, failed=failed, unstarted=unstarted)
