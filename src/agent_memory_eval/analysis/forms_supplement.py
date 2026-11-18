"""Offline verification and pooled analysis of the two separately sealed forms batches."""

import csv
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_memory_eval.analysis.forms_results import summarize, summarize_checked
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import endpoint_terms, ordered_jobs, write_json
from agent_memory_eval.experiments.forms_supplement import BASE_COMMIT, COMPLETION, OUT_ROOT, PROFILE_FORMAT, SOURCE_ROOT
from agent_memory_eval.models.openrouter import request_reservation_usd
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, safe_path
from agent_memory_eval.reproduction.runner import input_leases, inventory_inputs, stage_inputs


PROFILE = "protocols/pilots/tau2-two-pass-memory-forms-supplement-v1.json"
PROFILE_SHA256 = "5f3a9631e7b1449c2315ef4d719d50fbc45897a08da6456a301388b68f731a22"
IDENTITY = ("index", "phase", "task_id", "repetition", "arm", "directory", "run_id")
TOLERANCE = Decimal("1e-8")


def require(test, message):
    if not test:
        raise ContractError(message)


def timestamp(value):
    date = datetime.fromisoformat(value)
    require(date.tzinfo is not None, "Billing audit requires timezone-aware timestamps")
    return date


def identity(row):
    return {key: row[key] for key in IDENTITY}


def metadata(raw):
    completion = safe_path(raw, "completion.json").read_bytes()
    seal = read_json(completion)
    control = read_json(asset_text(raw, dict(path="control.json", sha256=seal["control_sha256"])).encode())
    rows = read_json(asset_text(raw, dict(path="allocations.json", sha256=seal["allocation_sha256"])).encode())
    return dict(control=control, seal=seal, rows=rows, completion_sha256=digest(completion))


def merge_rows(original, supplement):
    """Only a matching never-started disposition can be replaced by supplemental evidence."""
    require(len({r["run_id"] for r in original}) == len(original) and
            len({r["index"] for r in original}) == len(original), "Duplicate original assignment")
    pending = [r for r in original if r["status"] == "not_started"]
    require(len(pending) == len(supplement), "Supplement does not cover the sealed untouched assignments")
    for old, new in zip(pending, supplement):
        require(old["phase"] == "solve" and "result" not in old and identity(old) == identity(new),
                "Supplement reuses an attempted assignment or changes its identity/order")
    by_index = {r["index"]: r for r in supplement}
    merged = []
    for r in original:
        in_supplement = r["index"] in by_index
        source = "supplement" if in_supplement else "original"
        final = by_index[r["index"]] if in_supplement else r
        require((final["status"] == "not_started") == ("result" not in final),
                "Attempt and result disagree in the pooled ledger")
        merged.append(dict(final, analysis_origin=source,
                           episode_origin=source if final["phase"] == "solve" and "result" in final else None))
    return merged


def zero_bill():
    return dict(requests=0, http_calls=0, total_tokens=0, reported_usd=Decimal(0), unknown_count=0)


def bills_total(bills):
    return dict(http_calls=sum(v["http_calls"] for v in bills.values()),
                known_usd=str(sum((v["reported_usd"] for v in bills.values()), Decimal(0))),
                unknown_calls=sum(v["unknown_count"] for v in bills.values()))


def add_bills(total, per_role):
    for actor, bill in per_role.items():
        dest = total.setdefault(actor, zero_bill())
        for key, amount in bill.items():
            dest[key] += amount


def result_usage(bills):
    return {role: dict(http_calls=b["http_calls"], reported_usd=str(b["reported_usd"]),
                       total_tokens=b["total_tokens"], unknown_count=b["unknown_count"]) for role, b in bills.items()}


def wire_evidence(folder, row, models, seen, *, seeds=None, settings=None, supplemental=False):
    """Recompute role usage and any 429 from the actual request and provider-response blobs."""
    result = row["result"]
    raw = safe_path(folder, "log.jsonl").read_bytes()
    require(digest(raw) == result["log_sha256"] and row["run_id"] == result["run_id"]
            and row["status"] == result["status"], "Sealed job differs from allocation")
    blobs, starts, finishes, bills, sends = BlobStore(folder), {}, set(), {}, []
    failure, last_request, runtime_code = None, None, None
    unpriced_send = False
    for line in raw.splitlines():
        for event in read_json(line)["events"]:
            payload, kind = event["payload"], event["type"]
            if kind in {"model.request_started", "model.request_finished"}:
                require(event["run_id"] == row["run_id"], "Foreign request in sealed job journal")
            if kind == "runtime.error":
                runtime_code = payload["code"]
            if kind == "model.request_started":
                request_id = payload["request_id"]
                require(request_id not in seen, "Duplicate request identity across allocations")
                seen.add(request_id)
                starts[request_id] = (payload, timestamp(event["timestamp_utc"]))
            elif kind == "model.request_finished":
                request_id = payload["request_id"]
                last_request = request_id
                require(request_id in starts and request_id not in finishes, "Unmatched request finish")
                finishes.add(request_id)
                start, began = starts[request_id]
                ended = timestamp(event["timestamp_utc"])
                require(ended >= began, "Request timestamps are reversed")
                role = start["actor_role"]
                bill = bills.setdefault(role, zero_bill())
                bill["requests"] += 1
                submitted = payload["delivery_status"] != "not_submitted"
                response = blobs.get(payload["response_ref"]) if payload["response_ref"] else None
                usage = response.get("usage") if isinstance(response, dict) else None
                price = usage.get("cost") if isinstance(usage, dict) else None
                observed = type(price) in (float, int) and Decimal(str(price)).is_finite() and price >= 0
                cost = payload["cost_status"]
                raw_usage = usage if isinstance(usage, dict) else None
                require((cost == "observed") == observed and payload["usage"] == raw_usage
                        and payload["cost_usd"] == (price if observed else None), "Provider and recorded usage or charge differ")
                bill["total_tokens"] += (raw_usage or {}).get("total_tokens") or 0
                if observed:
                    bill["reported_usd"] += Decimal(str(price))
                if cost == "unknown":
                    require(submitted, "Unsubmitted request marked as an unpriced HTTP call")
                    bill["unknown_count"] += 1
                if not submitted:
                    require(cost == "not_applicable" and response is None, "Unsubmitted request has a provider charge or response")
                    continue
                require(failure is None and not unpriced_send, "Another HTTP request followed an unresolved send in the same episode")
                unpriced_send = cost == "unknown"
                bill["http_calls"] += 1
                wire = blobs.get(start["request_ref"])
                model = models["worker" if role == "writer" else role]
                endpoint = model["selected_endpoint"]
                require(wire["model"] == model["id"] and wire["stream"] is False and
                        wire["provider"] == dict(only=[endpoint["tag"]], allow_fallbacks=False, require_parameters=True),
                        "Submitted wire differs from the frozen role route")
                if seeds is not None:
                    require(("seed" not in wire if role == "user_simulator" else
                            wire.get("seed") == seeds["writer" if role == "writer" else "native_and_solver"]),
                            "Submitted wire differs from the frozen role and repetition seed")
                if settings is not None:
                    fingerprint = digest(canonical({k: v for k, v in wire.items() if k not in {"messages", "seed"}}))
                    if supplemental:
                        require(fingerprint in settings.get(role, ()), "Supplement changed the role's tool interface or model settings")
                    else:
                        settings.setdefault(role, set()).add(fingerprint)
                if payload["status"] == "response":
                    require(response["model"] == model["id"] and response["provider"] == endpoint["provider_name"],
                            "Accepted provider response differs from the frozen role route")
                bound = request_reservation_usd(wire, model)
                sends.append((began, ended, bound, Decimal(str(price)) if observed else Decimal(0)))
                error = response.get("error") if isinstance(response, dict) else None
                if (result.get("error_code") == "HTTP_429" and isinstance(error, dict)
                        and type(error.get("code")) is int and error["code"] == 429):
                    require(payload["status"] == "error" and payload["delivery_status"] == "response_received"
                            and cost in {"unknown", "observed"}, "429 has no received HTTP error evidence")
                    failure = dict(request_id=request_id, actor=role, request_sha256=start["request_ref"]["sha256"],
                                   response_sha256=payload["response_ref"]["sha256"], cost_status=cost,
                                   reserved_usd=str(bound if cost == "unknown" else Decimal(0)),
                                   retry_after_seconds=error.get("retry_after_seconds"), retry_after_utc=error.get("retry_after_utc"))
    require(set(starts) == finishes, "Unclosed request in a sealed allocation")
    if row["phase"] == "solve":
        require((result.get("error_code") == "HTTP_429") == (runtime_code == "HTTP_429") == bool(failure),
                "429 result, transport classification and provider evidence disagree")
    if failure:
        require(failure["request_id"] == last_request and result.get("grading_status") != "graded" and result.get("official_score") is None
                and result.get("official_success") is None and row["status"] == "error", "Failed 429 was treated as graded")
    reported = result["billing_by_role"]
    require(set(reported) == set(bills) and all(
        dict(reported[role], reported_usd=Decimal(reported[role]["reported_usd"])) == bill
        for role, bill in bills.items()), "Per-role billing differs from sealed provider evidence")
    return bills, sends, failure


def counts(rows):
    solves = [row for row in rows if row["phase"] == "solve"]
    return dict(writer_allocations=len(rows) - len(solves), episode_allocations=len(solves),
                attempted_episodes=sum("result" in r for r in solves),
                graded_episodes=sum(r.get("result", {}).get("grading_status") == "graded" for r in solves),
                ungraded_started_episodes=sum("result" in r and r["result"]["grading_status"] != "graded" for r in solves),
                successful_episodes=sum(r.get("result", {}).get("official_success") is True for r in solves),
                unstarted_episodes=sum(r["status"] == "not_started" for r in solves))


def check_billing(raw, batch, limits, started_at, state, seen, *, supplemental, seeds_by_rep=None):
    bills, failures = {}, {}
    other_unknown, pending = False, False
    begin, end = timestamp(batch["control"]["started_at"]), timestamp(batch["seal"]["ended_at"])
    deadline = started_at + timedelta(seconds=limits["max_seconds"])
    for row in batch["rows"]:
        if row["status"] == "not_started":
            require(row.get("reason") and row["reason"] == batch["seal"]["stop_reason"],
                    "Unstarted allocation differs from the recorded stop")
            pending = True
            continue
        require(not pending and not other_unknown, "Execution continued beyond its sealed stop")
        role_bills, sends, failure = wire_evidence(safe_path(raw, row["directory"]), row,
            batch["control"]["model_catalog_entries"], seen, seeds=seeds_by_rep[row["repetition"]] if seeds_by_rep else None,
            settings=state.get("settings"), supplemental=supplemental)
        add_bills(bills, role_bills)
        unknown = sum(b["unknown_count"] for b in role_bills.values())
        require(row["result"]["unknown_cost"] is bool(unknown), "Job and wire unknown-cost flags differ")
        for began, finished, reservation, cost in sends:
            require(begin <= began <= finished <= end and began < deadline, "Submitted request outside authorized time")
            require(state["next_send"] is None or began >= state["next_send"], "Request precedes prior finish or 429 cooldown")
            require(state["calls"] < limits["max_calls"] and
                    state["known"] + state["reserved"] + reservation <= Decimal(str(limits["max_usd"])) + TOLERANCE,
                    "Request exceeds carry-forward call or monetary budget")
            state["calls"] += 1
            state["known"] += cost
            state["next_send"] = finished
        if supplemental:
            require(row.get("http_429") == failure, "Ledger 429 evidence differs from the actual provider error")
        if failure:
            failures[row["index"]] = failure
            retry = failure["retry_after_seconds"]
            require(retry is None or (type(retry) is int and retry >= 0), "Invalid 429 Retry-After interval")
            state["next_send"] += timedelta(seconds=max(60, retry or 0))
            if failure["retry_after_utc"]:
                state["next_send"] = max(state["next_send"], timestamp(failure["retry_after_utc"]))
        if unknown:
            if unknown == 1 and failure and failure["cost_status"] == "unknown":
                state["reserved"] += Decimal(failure["reserved_usd"])
            else:
                other_unknown = True
    require(state["known"] + state["reserved"] <= Decimal(str(limits["max_usd"])) + TOLERANCE,
            "Known charges plus unresolved reservations exceed the study cap")
    if supplemental:
        require(batch["seal"]["other_unpriced_stop"] is other_unknown, "Unresolved non-429 stop differs")
        require(not other_unknown or batch["seal"]["stop_reason"] == "unknown_cost", "Non-429 unknown cost did not stop study")
    return bills, failures


def verify_scope(workspace, profile, original, supplement):
    root, extra = original["control"], supplement["control"]
    parent_profile = root["profile"]
    require(original["completion_sha256"] == COMPLETION and original["seal"]["stop_reason"] == "unknown_cost",
            "Original batch is not the authorized frozen study")
    require(root["implementation"]["implementation_commit"] == BASE_COMMIT and
            parent_profile == read_json(asset_text(workspace, profile["parent_profile_ref"]).encode()) and
            root["design"] == read_json(asset_text(workspace, parent_profile["design_ref"]).encode()),
            "Parent implementation, profile or frozen design differs")
    require(profile["format"] == PROFILE_FORMAT and profile["status"] == "ready" and profile.get("authorization")
            and profile["source_root"] == SOURCE_ROOT and profile["output_root"] == OUT_ROOT
            and profile["http_429_cooldown_seconds"] == 60 and extra["profile"] == profile,
            "Supplement is outside its approved scope")
    require(extra["source_completion_sha256"] == COMPLETION and extra["source_root"] == SOURCE_ROOT
            and extra["original_started_at"] == root["started_at"]
            and extra["implementation"]["implementation_commit"] == profile["implementation_commit"]
            and extra["implementation"]["worktree_has_uncommitted_implementation"] is False
            and endpoint_terms(extra["model_catalog_entries"]) == endpoint_terms(root["model_catalog_entries"]),
            "Supplement changed the frozen runtime, source identity or role endpoints")
    require(timestamp(root["started_at"]) <= timestamp(original["seal"]["ended_at"])
            <= timestamp(profile["prepared_at"]) <= timestamp(extra["started_at"])
            <= timestamp(supplement["seal"]["ended_at"]), "Deployment timestamps are inconsistent")
    rows = original["rows"]
    planned = ordered_jobs(root["design"])
    require(len(rows) == len(planned) and all(all(row[k] == val for k, val in expected.items())
            and row["directory"] == f'jobs/{row["index"]:04d}' for row, expected in zip(rows, planned)),
            "Original allocation order differs from the design")
    pending = [identity(r) for r in rows if r["status"] == "not_started"]
    require([r["index"] for r in pending] == list(range(277, 585))
            and sum(r["phase"] == "write" and r["status"] in {"empty", "completed"} for r in rows) == 232
            and counts(rows)["graded_episodes"] == 43 and counts(rows)["attempted_episodes"] == 44
            and profile["pending_sha256"] == digest(canonical(pending)), "Original untouched suffix or outcomes differ")
    return pending


def verify_receipts(workspace, profile, original, supplement, pending, parent_bills, parent_errors, extra_bills, state):
    base, add = bills_total(parent_bills), bills_total(extra_bills)
    original_seal, seal = original["seal"], supplement["seal"]
    require(set(parent_errors) == {276} and parent_errors[276]["cost_status"] == "unknown"
            and Decimal(parent_errors[276]["reserved_usd"]) == Decimal("0.0018567")
            and base["unknown_calls"] == 1 and original_seal["unknown_cost"] is True
            and base["http_calls"] == original_seal["http_calls"]
            and abs(Decimal(base["known_usd"]) - Decimal(str(original_seal["reported_usd"]))) <= TOLERANCE,
            "Original 429 or carry-forward accounting differs from the approval")
    carry = dict(**base, original_429=parent_errors[276], pending_sha256=digest(canonical(pending)))
    require(supplement["control"]["carry"] == carry, "Supplement control misstates the original balance or reserve")
    parent = original["control"]
    preflight = read_json(asset_text(workspace, profile["preflight_ref"]).encode())
    expected = dict(parent_completion_sha256=COMPLETION, parent_implementation=BASE_COMMIT,
                    parent_guards=parent["profile"]["guards"], started_at=parent["started_at"], untouched=len(pending),
                    sealed_writer_banks_required=sum(r["arm"] in {"fact", "episode", "lesson", "procedure", "summary"} for r in pending),
                    scope=[pending[0]["index"], pending[-1]["index"]], **carry)
    require(preflight["implementation_commit"] == profile["implementation_commit"] and preflight["parent"] == expected,
            "Frozen preflight differs from independently verified scope and billing")
    require(seal["supplement"] == add and seal["combined_http_calls"] == state["calls"]
            and Decimal(seal["combined_known_usd"]) == state["known"]
            and Decimal(seal["combined_reserved_usd"]) == state["reserved"]
            and seal["combined_unknown_calls"] == base["unknown_calls"] + add["unknown_calls"],
            "Supplement or combined completion ledger differs from the verified provider evidence")


def completion_usage(bill, reserved):
    return dict(http_calls=bill["http_calls"], reported_usd=float(bill["known_usd"]), known_usd_decimal=bill["known_usd"],
                unknown_cost=bool(bill["unknown_calls"]), unknown_cost_requests=bill["unknown_calls"],
                reserved_usd=str(reserved))


def stage_view(view, control, rows, sources, inventory, refs, stop, ended_at, bill, reserve):
    (view / "jobs").mkdir(parents=True)
    for raw, item in sources:
        prefix = item["path"] + "/"
        files = [dict(row, path=row["path"][len(prefix):]) for row in inventory if row["path"].startswith(prefix + "jobs/")]
        stage_inputs(raw, view, files, dict(inputs=[]))
    write_json(view / "control.json", dict(control, analysis_view=dict(format="forms-combined-analysis-view/v1", sources=refs)))
    write_json(view / "allocations.json", rows)
    write_json(view / "completion.json", dict(stop_reason=stop, ended_at=ended_at, **completion_usage(bill, reserve),
        allocation_sha256=digest((view / "allocations.json").read_bytes()), control_sha256=digest((view / "control.json").read_bytes())))
    for path in (view / "control.json", view / "allocations.json", view / "completion.json"):
        path.chmod(0o444)
    for path in view.rglob("*"):
        if path.is_dir():
            path.chmod(0o555)
    view.chmod(0o555)


def write_episode_origins(path, merged):
    with path.open("x", newline="", encoding="utf-8") as stream:
        table = csv.DictWriter(stream, fieldnames=["job", "run_id", "task_id", "repetition", "condition",
            "disposition_origin", "episode_origin", "solve_status", "grading_status"])
        table.writeheader()
        for row in merged:
            if row["phase"] == "solve":
                table.writerow(dict(job=row["index"], run_id=row["run_id"], task_id=row["task_id"], repetition=row["repetition"],
                    condition=row["arm"], disposition_origin=row["analysis_origin"], episode_origin=row["episode_origin"],
                    solve_status=row["status"], grading_status=row.get("result", {}).get("grading_status")))


def audit(workspace, ready, output, *, expected_completion=None):
    workspace, output = Path(workspace).resolve(), Path(output).absolute()
    profile_bytes = Path(ready).read_bytes()
    require(digest(profile_bytes) == PROFILE_SHA256, "Ready profile differs from the approved supplemental deployment")
    profile = read_json(profile_bytes)
    roots = [safe_path(workspace, name) for name in (SOURCE_ROOT, OUT_ROOT)]
    require(not output.exists() and output.resolve() == output and all(
        not output.is_relative_to(raw) and not raw.is_relative_to(output) for raw in roots),
        "Use a new analysis destination outside both sealed runs")
    items = [dict(kind="run_batch", path=name, completion_sha256=checksum)
             for name, checksum in ((SOURCE_ROOT, COMPLETION), (OUT_ROOT, expected_completion or
                                         digest(safe_path(roots[1], "completion.json").read_bytes())))]
    entry = dict(inputs=items)
    with input_leases(workspace, entry):
        original, supplement = [metadata(raw) for raw in roots]
        require(supplement["completion_sha256"] == items[1]["completion_sha256"], "Supplement completion changed")
        pending = verify_scope(workspace, profile, original, supplement)
        merged = merge_rows(original["rows"], supplement["rows"])
        control = original["control"]
        limits, began = control["profile"]["guards"], timestamp(control["started_at"])
        state = dict(calls=0, known=Decimal(0), reserved=Decimal(0), next_send=None, settings={})
        seen = set()
        seeds = {row["repetition"]: row for row in control["design"]["model"]["seed_pairs"]}
        base_bills, base_errors = check_billing(roots[0], original, limits, began, state, seen, supplemental=False, seeds_by_rep=seeds)
        base_reserve = state["reserved"]
        extra_bills, extra_errors = check_billing(roots[1], supplement, limits, began, state, seen, supplemental=True, seeds_by_rep=seeds)
        verify_receipts(workspace, profile, original, supplement, pending, base_bills, base_errors, extra_bills, state)
        inventory = inventory_inputs(workspace, entry)
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=output.parent) as tmp:
            staged = Path(tmp) / "audit"
            view = staged / "combined-view"
            refs = {source: dict(path=item["path"], completion_sha256=item["completion_sha256"])
                    for source, item in zip(("original", "supplement"), items)}
            total = {}
            add_bills(total, base_bills)
            add_bills(total, extra_bills)
            final = supplement["seal"]
            stage_view(view, control, merged, zip(roots, items), inventory, refs, final["stop_reason"],
                       final["ended_at"], bills_total(total), state["reserved"])
            # The ordinary reviewer independently verifies all closed journals, blobs, memory deliveries and native scores.
            combined_result = summarize(view)
            require(combined_result["usage_by_role"] == result_usage(total),
                    "Pooled journal review differs from independently reconstructed role usage")
            extra_reserve = state["reserved"] - base_reserve
            extra_completion = {key: final[key] for key in ("stop_reason", "ended_at", "allocation_sha256", "control_sha256")}
            extra_completion.update(completion_usage(bills_total(extra_bills), extra_reserve))
            source_results = [("original", original["rows"], original["seal"], base_bills),
                              ("supplement", supplement["rows"], extra_completion, extra_bills)]
            for name, rows, completion, bills in source_results:
                write_json(staged / (name + "-results.json"), summarize_checked(control["design"], completion, rows,
                                                                                result_usage(bills), control["source_outcomes"]))
            write_json(staged / "combined-results.json", combined_result)
            write_episode_origins(staged / "episode-origins.csv", merged)
            receipt = dict(format="two-pass-forms-supplement-audit/v1", status="passed", sources=refs,
                ready_profile_sha256=digest(profile_bytes), approval=profile["authorization"], guards=limits,
                role_settings_sha256={k: sorted(v) for k, v in state["settings"].items()},
                original_started_at=control["started_at"], pending_sha256=profile["pending_sha256"],
                input_file_count=len(inventory), input_manifest_sha256=digest(canonical(inventory)),
                original=dict(**counts(original["rows"]), **bills_total(base_bills), reserved_usd=str(base_reserve),
                              http_429=base_errors),
                supplement=dict(**counts(supplement["rows"]), **bills_total(extra_bills),
                                reserved_usd=str(extra_reserve), http_429=extra_errors),
                combined=dict(**counts(merged), **bills_total(total), reserved_usd=str(state["reserved"])),
                combined_view_completion_sha256=digest((view / "completion.json").read_bytes()))
            write_json(staged / "audit.json", receipt)
            require(inventory_inputs(workspace, entry) == inventory, "Sealed input changed during verification")
            require(not output.exists(), "Analysis destination was created by another process")
            staged.rename(output)
    return receipt
