"""Offline evidence audit for the approved third batch of the two-pass forms study."""

from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from agent_memory_eval.analysis import forms_supplement as previous
from agent_memory_eval.analysis.forms_results import summarize, summarize_checked
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import endpoint_terms, write_json
from agent_memory_eval.models.openrouter import request_reservation_usd
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, safe_path
from agent_memory_eval.reproduction.runner import input_leases, inventory_inputs


PROFILE = "protocols/pilots/tau2-two-pass-memory-forms-followon-v1.json"
PROFILE_SHA256 = "9c2751f6790ef922c955cea384c309f7b94aefadaa35a7b6d2211a1c81253ebd"
FOLLOWON = "runs/tau2-two-pass-forms-luna-deepseek-supplement-02"
FIRST_COMPLETION = "40004aaf9f1d127940bf9782864abe1cb6080796a73fa7bd1141a0e4652a5756"
SOURCE_SEALS = {previous.SOURCE_ROOT: previous.COMPLETION, previous.OUT_ROOT: FIRST_COMPLETION}
require = previous.require
timestamp = previous.timestamp


def generation_id(response):
    error = response.get("error")
    info = error.get("metadata") if isinstance(error, dict) else None
    for value in (response.get("id"), response.get("generation_id"),
                  info.get("generation_id") if isinstance(info, dict) else None):
        if isinstance(value, str) and re.fullmatch(r"gen-[A-Za-z0-9-]{1,124}", value):
            return value
    return None


def provider_failure(runtime_code, result, started, finished, wire, response, model):
    """Qualify a terminal received failure without treating a missing bill as zero."""
    if not isinstance(response, dict) or not isinstance(response.get("error"), dict):
        return None
    body = response["error"]
    code, gen = body.get("code"), generation_id(response)
    if runtime_code == "HTTP_429" and type(code) is int and code == 429:
        code = 429
    elif runtime_code in {"HTTP_504", "PROVIDER_RESPONSE_ERROR"} and code in (504, "504") and type(code) in (int, str) and gen:
        code = 504
    else:
        return None
    p = finished["payload"]
    if p["status"] != "error" or p["delivery_status"] != "response_received" or p["cost_status"] not in {"unknown", "observed"}:
        return None
    require(started["request_id"] == p["request_id"], "Provider exception is not tied to its wire")
    require(result["status"] == "error" and result["error_code"] == runtime_code and result["grading_status"] != "graded"
            and result["official_success"] is None and result["official_score"] is None,
            "Qualified provider exception disagrees with its terminal outcome")
    transport = runtime_code in {"HTTP_429", "HTTP_504"}
    return dict(request_id=p["request_id"], actor=started["actor_role"], request_sha256=started["request_ref"]["sha256"],
        response_sha256=p["response_ref"]["sha256"], code=code, cost_status=p["cost_status"],
        reserved_usd=str(request_reservation_usd(wire, model) if p["cost_status"] == "unknown" else Decimal(0)),
        generation_id=gen, retry_after_seconds=body.get("retry_after_seconds") if transport else None,
        retry_after_utc=body.get("retry_after_utc") if transport else None)


def inspect_episode(folder, row, models, max_input_bytes):
    """Derive input lengths, exposure by delivery and terminal evidence from archived events."""
    result = row["result"]
    raw = safe_path(folder, "log.jsonl").read_bytes()
    require(digest(raw) == result["log_sha256"] and result["run_id"] == row["run_id"], "Episode or log checksum differs")
    events = [e for line in raw.splitlines() for e in read_json(line)["events"] if e["run_id"] == row["run_id"]]
    blobs, requests, included = BlobStore(folder), {}, {}
    lengths = {role: Counter() for role in ("worker", "user_simulator")}
    delivered = Counter()
    normal, first_worker, last_finish, runtime = 0, None, None, None
    received_failures = []
    responses = Counter()
    for event in events:
        kind, p = event["type"], event["payload"]
        if kind == "model.request_started":
            key = p["request_id"]
            require(key not in requests, "Repeated request within an episode")
            wire = blobs.get(p["request_ref"])
            role = p["actor_role"]
            require(role in lengths, "Unexpected native actor")
            size = len(canonical(wire))
            requests[key] = (p, size)
            included[key] = Counter()
            if role == "worker" and first_worker is None:
                first_worker = p["request_ref"]["sha256"]
        elif kind == "memory.input_included":
            key = p["request_id"]
            require(requests[key][0]["actor_role"] == "worker", "Memory was sent to the simulator")
            included[key][p["field"]] += 1
        elif kind == "model.request_finished":
            start, size = requests[p["request_id"]]
            delivery, num = p["delivery_status"], sum(included[p["request_id"]].values())
            require(delivery == "not_submitted" or size <= max_input_bytes, "Oversized wire was sent")
            lengths[start["actor_role"]][delivery] += size
            delivered[delivery] += num
            if p["status"] == "response":
                normal += num
                responses[start["actor_role"]] += 1
            elif delivery == "response_received" and p.get("response_ref"):
                received_failures.append(p["request_id"])
            last_finish = event
        elif kind == "runtime.error":
            runtime = p["code"]
    for role, by_delivery in lengths.items():
        usage = result["usage_by_role"][role]
        billed = result["billing_by_role"].get(role, previous.zero_bill())
        require(usage["input_bytes"] == sum(by_delivery.values()) and usage["input_bytes_by_delivery"] == dict(by_delivery),
                "Recorded role input byte lengths differ from the request wires")
        require(result["requests_by_role"][role] == usage["requests"] == billed["requests"]
                and usage["http_attempts"] == billed["http_calls"] and usage["reported_tokens"] == billed["total_tokens"]
                and usage["unknown_cost_requests"] == billed["unknown_count"] and usage["responses"] == responses[role]
                and Decimal(usage["reported_cost_usd_decimal"]) == Decimal(billed["reported_usd"]),
                "Native role usage differs from the independently audited provider ledger")
    require(result["memory_input_spans_by_delivery"] == dict(delivered) and result["memory_input_spans"] == sum(delivered.values())
            and result["normal_response_memory_spans"] == normal and result["memory_input_fields"] == dict(sum(included.values(), Counter()))
            and result["first_worker_request_sha256"] == first_worker, "Published memory exposure differs from archived requests")
    classes = {"HTTP_429", "HTTP_504", "PROVIDER_RESPONSE_ERROR"}
    if result["error_code"] in classes or runtime in classes:
        require(result["error_code"] == runtime, "Terminal transport classification disagrees with episode")
    if last_finish is None or not last_finish["payload"].get("response_ref"):
        return None, None
    p = last_finish["payload"]
    start, _ = requests[p["request_id"]]
    receipt = provider_failure(runtime, result, start, last_finish, blobs.get(start["request_ref"]),
                               blobs.get(p["response_ref"]), models[start["actor_role"]])
    if receipt:
        require(received_failures == [p["request_id"]], "An eligible failure was not the only terminal received error")
    return receipt, timestamp(last_finish["timestamp_utc"])


def next_after_error(receipt, when):
    retry = receipt["retry_after_seconds"]
    require(retry is None or (type(retry) is int and retry >= 0), "Invalid provider Retry-After interval")
    earliest = when + timedelta(seconds=max(60, retry or 0))
    if receipt["retry_after_utc"]:
        earliest = max(earliest, timestamp(receipt["retry_after_utc"]))
    return earliest


def merge_followon(prior, new):
    pending = [r for r in prior if r["status"] == "not_started"]
    require([r["index"] for r in pending] == list(range(303, 585)) and all(r.get("reason") == "unknown_cost" for r in pending),
            "Only the untouched 303–584 suffix is eligible")
    validated = previous.merge_rows(prior, new)
    return [old if old["status"] != "not_started" else dict(row, analysis_origin="followon",
                episode_origin="followon" if "result" in row else None) for old, row in zip(prior, validated)]


def read_asset(workspace, ref):
    return read_json(asset_text(workspace, ref).encode())


def history(workspace, roots, archives):
    original, first = archives
    require({name: item["completion_sha256"] for name, item in zip(SOURCE_SEALS, archives)} == SOURCE_SEALS,
            "Authorized historical completions differ")
    old_profile = read_asset(workspace, dict(path=previous.PROFILE, sha256=previous.PROFILE_SHA256))
    old_pending = previous.verify_scope(workspace, old_profile, original, first)
    prior = previous.merge_rows(original["rows"], first["rows"])
    require(previous.counts(prior)["attempted_episodes"] == 70 and previous.counts(prior)["graded_episodes"] == 66
            and first["seal"]["other_unpriced_stop"] is True and first["seal"]["stop_reason"] == "unknown_cost",
            "The first two batches have unexpected attempted or graded episodes")
    control = original["control"]
    limits, began = control["profile"]["guards"], timestamp(control["started_at"])
    seeds = {r["repetition"]: r for r in control["design"]["model"]["seed_pairs"]}
    state = dict(calls=0, known=Decimal(0), reserved=Decimal(0), next_send=None, settings={})
    seen = set()
    base, original_errors = previous.check_billing(roots[0], original, limits, began, state, seen, supplemental=False, seeds_by_rep=seeds)
    extra, _ = previous.check_billing(roots[1], first, limits, began, state, seen, supplemental=True, seeds_by_rep=seeds)
    previous.verify_receipts(workspace, old_profile, original, first, old_pending, base, original_errors, extra, state)
    # The first supplement actually stopped. Its historic reserve remains the original HTTP 429 alone.
    require(state["reserved"] == Decimal("0.0018567"), "Historical reserve changed before new authorization")
    cap = control["design"]["second_pass"]["proposed_limits"]["max_input_bytes"]
    rate = cutoff = failed = None
    for raw, batch in zip(roots, archives):
        for row in batch["rows"]:
            if row["phase"] == "solve" and "result" in row:
                evidence, finished = inspect_episode(raw / row["directory"], row, batch["control"]["model_catalog_entries"], cap)
                if row["index"] == 302:
                    failed, rate, cutoff = row, evidence, finished
    require(rate and rate["code"] == 504 and rate["cost_status"] == "unknown"
            and Decimal(rate["reserved_usd"]) == Decimal("0.0082314"), "Prior 504 receipt or wire reservation differs")
    state["reserved"] += Decimal(rate["reserved_usd"])
    state["next_send"] = max(state["next_send"], next_after_error(rate, cutoff))
    all_bills = {}
    previous.add_bills(all_bills, base)
    previous.add_bills(all_bills, extra)
    pending = [previous.identity(r) for r in prior if r["status"] == "not_started"]
    require([r["index"] for r in pending] == list(range(303, 585)), "Historical pending range is not the approved suffix")
    carry = dict(**previous.bills_total(all_bills), reserved_usd=str(state["reserved"]), original_429=original_errors[276],
                 prior_504=rate, pending_sha256=digest(canonical(pending)))
    return dict(prior=prior, carry=carry, state=state, seen=seen, seeds=seeds, bills=all_bills, cap=cap, limits=limits,
                began=began, pending=pending, cutoff=cutoff, failed_row=failed)


def approval(workspace, profile, archives, history):
    original, first, latest = archives
    parent, deploy, c = original["control"], latest["control"], history["carry"]
    require(profile["format"] == "agent-memory-two-pass-forms-followon/v1" and profile["status"] == "ready"
            and profile["authorization"] and profile["source_seals"] == SOURCE_SEALS and profile["output_root"] == FOLLOWON
            and profile["provider_error_cooldown_seconds"] == 60 and profile["pending_sha256"] == c["pending_sha256"],
            "Deployment profile is outside the authorized follow-on scope")
    require(read_asset(workspace, profile["parent_profile_ref"]) == parent["profile"]
            and read_asset(workspace, profile["supplement_profile_ref"]) == first["control"]["profile"],
            "Follow-on profile changed a historic study profile")
    previous_audit = read_asset(workspace, profile["previous_audit_ref"])
    historic_totals = dict(**previous.counts(history["prior"]), **previous.bills_total(history["bills"]), reserved_usd="0.0018567")
    require(previous_audit["status"] == "passed" and previous_audit["combined"] == historic_totals
            and {v["path"]: v["completion_sha256"] for v in previous_audit["sources"].values()} == SOURCE_SEALS,
            "Previous audit differs in historical results or its original, unamended reserve")
    billing = read_asset(workspace, profile["billing_check_ref"])
    receipt = c["prior_504"]
    require(billing["supplement_completion_sha256"] == FIRST_COMPLETION and billing["job"] == 302
            and billing["run_id"] == history["failed_row"]["run_id"] and billing["model_request_id"] == receipt["request_id"]
            and billing["actor"] == receipt["actor"] and billing["wire_sha256"] == receipt["request_sha256"]
            and billing["provider_response_sha256"] == receipt["response_sha256"] and billing["observed_cost_status"] == "unknown"
            and digest(canonical(billing["observed_error"])) == receipt["response_sha256"]
            and billing["wire_conservative_reservation_usd"] == receipt["reserved_usd"]
            and billing["applied_to_sealed_study_budget"] is False and billing["model_sends_during_lookup"] == 0
            and billing["lookups"] and all(x["method"] == "GET" and x["http_status"] == 404 and
                x["generation_id"] == receipt["generation_id"] and x["failed_response_sha256"] == receipt["response_sha256"]
                for x in billing["lookups"]), "Generation-billing record does not identify the same unpriced prior 504")
    require(deploy["profile"] == profile and deploy["source_seals"] == SOURCE_SEALS and deploy["carry"] == c
            and re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", profile["implementation_commit"])
            and deploy["implementation"]["implementation_commit"] == profile["implementation_commit"]
            and deploy["implementation"]["worktree_has_uncommitted_implementation"] is False
            and endpoint_terms(deploy["model_catalog_entries"]) == endpoint_terms(parent["model_catalog_entries"])
            and deploy["original_started_at"] == parent["started_at"], "Follow-on runtime, endpoints, or carry changed")
    preflight = read_asset(workspace, profile["preflight_ref"])
    expected = dict(source_seals=SOURCE_SEALS, original_started_at=parent["started_at"], parent_guards=history["limits"],
                    untouched=len(history["pending"]), scope=[303, 584],
                    sealed_writer_banks_required=sum(r["arm"] in {"fact", "episode", "lesson", "procedure", "summary"}
                        for r in history["pending"]), **c)
    require(preflight["implementation_commit"] == profile["implementation_commit"] and preflight["parent"] == expected,
            "Follow-on preflight differs from the sealed historical evidence")
    require(timestamp(first["seal"]["ended_at"]) <= timestamp(profile["prepared_at"]) <= timestamp(deploy["started_at"])
            <= timestamp(latest["seal"]["ended_at"]), "Follow-on deployment timestamps are inconsistent")
    return previous_audit


def check_followon(raw, batch, h):
    bills, failures, state = {}, {}, h["state"]
    other_unknown = stopped = False
    begin, end = timestamp(batch["control"]["started_at"]), timestamp(batch["seal"]["ended_at"])
    limits = h["limits"]
    deadline = h["began"] + timedelta(seconds=limits["max_seconds"])
    cap = Decimal(str(limits["max_usd"]))
    error_ids = {h["carry"]["prior_504"]["generation_id"]}
    for row in batch["rows"]:
        if row["status"] == "not_started":
            require("result" not in row and row.get("reason") and row["reason"] == batch["seal"]["stop_reason"],
                    "Unstarted follow-on assignment lacks the sealed reason or has effects")
            stopped = True
            continue
        require(not stopped and not other_unknown and row["phase"] == "solve", "Follow-on execution continued beyond a stop")
        folder = safe_path(raw, row["directory"])
        models = batch["control"]["model_catalog_entries"]
        role_bills, sends, verified_429 = previous.wire_evidence(folder, row, models, h["seen"], seeds=h["seeds"][row["repetition"]],
            settings=state["settings"], supplemental=True)
        exception, when = inspect_episode(folder, row, models, h["cap"])
        require((verified_429 is not None) == (exception is not None and exception["code"] == 429),
                "Received HTTP 429 evidence differs from terminal journal")
        require(row.get("provider_error") == exception, "Declared provider exception differs from the received response")
        previous.add_bills(bills, role_bills)
        unknown = sum(b["unknown_count"] for b in role_bills.values())
        require(row["result"]["unknown_cost"] is bool(unknown), "Follow-on episode unknown-cost flag differs")
        for began, finished, reservation, cost in sends:
            require(begin <= began <= finished <= end and began < deadline, "Follow-on wire outside the original time cap")
            require(began >= state["next_send"], "Wire precedes prior finish or required provider cooldown")
            require(state["calls"] < limits["max_calls"] and state["known"] + state["reserved"] + reservation <= cap + previous.TOLERANCE,
                    "Wire exceeds the cumulative call or spend reservation cap")
            state["calls"] += 1
            state["known"] += cost
            state["next_send"] = finished
        if exception:
            gen = exception["generation_id"]
            if gen:
                require(gen not in error_ids, "A provider generation ID was reused for another exception")
                error_ids.add(gen)
            failures[row["index"]] = exception
            state["next_send"] = next_after_error(exception, when)
        if unknown:
            if unknown == 1 and exception and exception["cost_status"] == "unknown":
                state["reserved"] += Decimal(exception["reserved_usd"])
            else:
                other_unknown = True
    seal = batch["seal"]
    billed = previous.bills_total(bills)
    require(state["known"] + state["reserved"] <= cap + previous.TOLERANCE, "Follow-on closed charges and reserve exceed cap")
    require(seal["other_unpriced_stop"] is other_unknown and (seal["stop_reason"] == "unknown_cost") == other_unknown,
            "Unqualified unknown charge or stop disposition differs")
    require(seal["stop_reason"] in (None, "call_limit", "spend_limit", "time_limit", "unknown_cost")
            and (seal["stop_reason"] != "call_limit" or state["calls"] == limits["max_calls"])
            and (seal["stop_reason"] != "time_limit" or end >= deadline), "Reported follow-on cutoff is inconsistent")
    require(seal["supplement"] == billed and seal["combined_http_calls"] == state["calls"]
            and Decimal(seal["combined_known_usd"]) == state["known"] and Decimal(seal["combined_reserved_usd"]) == state["reserved"]
            and seal["combined_unknown_calls"] == h["carry"]["unknown_calls"] + billed["unknown_calls"],
            "Follow-on or combined billing differs from independently reconstructed charges and reserves")
    return bills, failures


def audit(workspace, ready, output, *, expected_completion=None, expected_profile=None):
    workspace, output = Path(workspace).resolve(), Path(output).absolute()
    profile_bytes = Path(ready).read_bytes()
    require(digest(profile_bytes) == PROFILE_SHA256, "Ready profile differs from the approved follow-on deployment")
    if expected_profile is not None:
        require(digest(profile_bytes) == expected_profile, "Follow-on ready profile checksum differs")
    profile = read_json(profile_bytes)
    roots = [safe_path(workspace, name) for name in (*SOURCE_SEALS, FOLLOWON)]
    require(not output.exists() and output.resolve() == output and all(
        not output.is_relative_to(raw) and not raw.is_relative_to(output) for raw in roots),
        "Use a new analysis directory outside all sealed inputs")
    last_sha = expected_completion if expected_completion is not None else digest(safe_path(roots[-1], "completion.json").read_bytes())
    items = [dict(kind="run_batch", path=name, completion_sha256=value)
             for name, value in (*SOURCE_SEALS.items(), (FOLLOWON, last_sha))]
    entry = dict(inputs=items)
    with input_leases(workspace, entry):
        archives = [previous.metadata(raw) for raw in roots]
        require(archives[-1]["completion_sha256"] == last_sha, "Follow-on completion changed")
        h = history(workspace, roots[:2], archives[:2])
        historic = approval(workspace, profile, archives, h)
        latest = archives[-1]
        merged = merge_followon(h["prior"], latest["rows"])
        bills, errors = check_followon(roots[-1], latest, h)
        total = {}
        previous.add_bills(total, h["bills"])
        previous.add_bills(total, bills)
        state = h["state"]
        final = latest["seal"]
        inv = inventory_inputs(workspace, entry)
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=output.parent) as temp:
            staged = Path(temp) / "audit"
            view = staged / "combined-view"
            refs = {label: dict(path=item["path"], completion_sha256=item["completion_sha256"])
                    for label, item in zip(("original", "supplement", "followon"), items)}
            control = archives[0]["control"]
            previous.stage_view(view, control, merged, zip(roots, items), inv, refs, final["stop_reason"],
                                final["ended_at"], previous.bills_total(total), state["reserved"])
            result = summarize(view)
            require(result["usage_by_role"] == previous.result_usage(total),
                    "Three-root native review differs from independently measured wire usage")
            new_reserve = state["reserved"] - Decimal(h["carry"]["reserved_usd"])
            specific = {k: final[k] for k in ("stop_reason", "ended_at", "allocation_sha256", "control_sha256")}
            specific.update(previous.completion_usage(previous.bills_total(bills), new_reserve))
            followon = summarize_checked(control["design"], specific, latest["rows"], previous.result_usage(bills), control["source_outcomes"])
            write_json(staged / "combined-results.json", result)
            write_json(staged / "followon-results.json", followon)
            previous.write_episode_origins(staged / "episode-origins.csv", merged)
            receipt = dict(format="two-pass-forms-followon-audit/v1", status="passed", sources=refs,
                ready_profile_sha256=digest(profile_bytes), approval=profile["authorization"], guards=h["limits"],
                original_started_at=control["started_at"], pending_sha256=h["carry"]["pending_sha256"],
                previous_as_sealed=historic["combined"], carry=h["carry"], original=historic["original"], supplement=historic["supplement"],
                followon=dict(**previous.counts(latest["rows"]), **previous.bills_total(bills), reserved_usd=str(new_reserve),
                              provider_errors=errors),
                combined=dict(**previous.counts(merged), **previous.bills_total(total), reserved_usd=str(state["reserved"])),
                role_settings_sha256={key: sorted(value) for key, value in state["settings"].items()},
                input_file_count=len(inv), input_manifest_sha256=digest(canonical(inv)),
                combined_view_completion_sha256=digest((view / "completion.json").read_bytes()))
            write_json(staged / "audit.json", receipt)
            require(inventory_inputs(workspace, entry) == inv, "Sealed input changed during follow-on audit")
            require(not output.exists(), "Analysis destination was created by another process")
            staged.rename(output)
    return receipt
