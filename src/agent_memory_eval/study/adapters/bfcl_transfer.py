"""Two-pass BFCL transfer on the registered hosted worker, with paired native target decisions."""

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count
from math import ceil
import os
from pathlib import Path
import time

from agent_memory_eval.adapters.bfcl_task_start import REVISION, open_relevance
from agent_memory_eval.analysis.efficiency import first_decision_effort, token_usage_by_role
from agent_memory_eval.analysis.recorded_decisions import require, sealed_bytes
from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.analysis.selection import worker_choices
from agent_memory_eval.contracts.core import MemoryPolicy, RunContext, canonical, digest, read_json
from agent_memory_eval.experiments.batch_recovery import stage_lease
from agent_memory_eval.experiments.forms_batch import endpoint_terms
from agent_memory_eval.experiments.forms_supplement import cooldown_seconds
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.memory.writing import (
    REQUIRED_STORE_CONTRACT,
    WriterFeedback,
    _experience,
    freeze_public_experience,
    read_public_experience,
    writer_inputs,
    writer_template,
    write_from_experience,
)
from agent_memory_eval.models.expense_ledger import ExpenseLedger, read_run_reservations
from agent_memory_eval.models.openrouter import (
    OpenRouterClient,
    ProviderError,
    model_catalog,
    model_endpoints,
    request_reservation_usd,
    resolve_endpoint,
    resolve_model,
)
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.batch import request_measurements, usage_from_events
from agent_memory_eval.recording.journal import BlobStore, Journal, read_experiment, safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.runner.bfcl_relevance import grade_reply, hosted_tools, load_inputs, normalized_first, solve
from agent_memory_eval.study.adapters.tau_banking import approval_for
from agent_memory_eval.study.cohort import check_through_stage, condition_banks, is_cohort, pool_sources, source_components
from agent_memory_eval.study.cohort_evidence import item_exposure
from agent_memory_eval.study.followup_budget import approval_amount, register_grants, registration_freeze
from agent_memory_eval.study.plan import (
    arm_name,
    conditions,
    content_text,
    model_profile,
    plan_digest,
    require_declared_models,
)
from agent_memory_eval.study.records import fixed, read
from agent_memory_eval.study.transfer import allocation_rows


def hosted_model(worker):
    model = resolve_model(model_catalog(), worker["model_id"], worker["parameters"])
    params = dict(worker["parameters"], tools=[], tool_choice="auto")
    return resolve_endpoint(model, model_endpoints(model["id"]), worker["provider"], params)


def schedule(plan):
    donors = defaultdict(list)
    for pair in plan["transfer"]["pairs"]:
        donors[pair["source"]].append(pair)
    policies = conditions(plan)
    for source, pairs in donors.items():
        for draw in range(1, plan["first_pass"]["draws"] + 1):
            tasks = []
            for pair in pairs:
                for rep, reader in policies:
                    for n in range(1, plan["repeats"] + 1):
                        for bank, arm in condition_banks(plan, arm_name(rep, reader)):
                            tasks.append(
                                dict(
                                    source=source,
                                    target=pair["target"],
                                    draw=draw,
                                    repeat=n,
                                    form=rep,
                                    reader=reader,
                                    bank=bank,
                                    condition=arm,
                                )
                            )
            yield source, draw, sorted(tasks, key=lambda t: digest(canonical([plan["id"], t])))


def run_key(study, *parts):
    return "r_" + digest(canonical([study, *parts]))[:32]


def run_events(scan, run):
    return [e for e in scan["events"].values() if e["run_id"] == run]


def terminal(events):
    return next((e["payload"] for e in events if e["type"] == "run.ended"), None)


def last_run(scan, study, key):
    """Follow only deterministic, append-only replacements for unsubmitted interruptions."""
    if key not in scan["starts"]:
        return None
    while (later := run_key(study, key, "unsubmitted")) in scan["starts"]:
        key = later
    return key


def submitted_or_ended(events, ended, reserved):
    deferred_codes = {"STUDY_LIMIT", "OPENROUTER_API_KEY_MISSING"}
    deferred = (ended and ended["termination_reason"] in deferred_codes) or any(
        e["type"] == "runtime.error" and e["payload"].get("code") in deferred_codes for e in events
    )
    return reserved or bool(ended and not deferred)


def once(journal, guard, key, stage, action):
    """Recover only unsubmitted attempts and preserve every possibly charged request."""
    study, run = journal.experiment_id, key
    while run in journal.scan["starts"]:
        events = run_events(journal.scan, run)
        ended = terminal(events)
        if submitted_or_ended(events, ended, run in guard.reserved_runs):
            return run, ended
        run = run_key(study, run, "unsubmitted")
    guard.stage, guard.run_id = stage, run
    guard.unknown_cost = False  # Any unreconciled older charge stays fully reserved.
    try:
        result = action(run)
        if isinstance(result, dict) and result.get("error_code") == "OPENROUTER_API_KEY_MISSING":
            raise ProviderError("OPENROUTER_API_KEY_MISSING")
    except ProviderError as exc:
        if exc.code == "OPENROUTER_API_KEY_MISSING":
            raise
        if run in journal.scan["starts"] and not terminal(run_events(journal.scan, run)):
            start = journal.scan["starts"][run]
            ctx = RunContext(study, run, start["session_id"], start["condition_id"], "diagnostic", "solve", "observed")
            journal.append([ctx.at("grade").event("outcome.scored", dict(
                grading_status="not_available", official_success=None, official_score=None, official_artifact_ref=None,
            ))])
            journal.append([ctx.at("seal").event("run.ended", dict(
                termination_reason=exc.code, grading_status="not_available", end_snapshot_id=None,
            ))])
    return run, terminal(run_events(journal.scan, run))


def retry_delay(journal, run, minimum):
    """Rebuild the retry deadline from this failed attempt's durable HTTP receipt, including after a restart."""
    for event in reversed(run_events(journal.scan, run)):
        payload = event["payload"]
        if event["type"] == "model.request_finished" and payload["response_ref"]:
            response = journal.blobs.get(payload["response_ref"])
            receipt = response.get("error") if isinstance(response, dict) else None
            if isinstance(receipt, dict):
                received = datetime.fromisoformat(event["timestamp_utc"])
                wait = cooldown_seconds(receipt, minimum, received)
                return max(0, ceil(wait - (datetime.now(UTC) - received).total_seconds()))
    return minimum


def bounded_reader_batches(dest, tasks):
    """Read the prospective retry policy attached to each disjoint set of original BFCL targets."""
    directory = dest / "reader-batches"
    claimed = set()
    for path in sorted(directory.glob("*/registration.json")):
        batch = read(safe_path(directory, path.relative_to(directory)))
        selected = batch["tasks"]
        require(batch["id"] == path.parent.name and isinstance(selected, list) and selected
                and len(selected) == len(set(selected)) and set(selected) <= tasks and claimed.isdisjoint(selected),
                "Overlapping or unregistered BFCL reader assignments")
        claimed.update(selected)
        waits = ()
        if "operations" in batch:
            ops = batch["operations"]
            require(isinstance(ops, dict) and set(ops) == {"max_concurrent_tasks", "transient_backoff_seconds"},
                    "Unexpected BFCL reader operations")
            waits = ops["transient_backoff_seconds"]
            require(type(ops["max_concurrent_tasks"]) is int and ops["max_concurrent_tasks"] == 1
                    and isinstance(waits, list) and waits and all(type(w) is int and w > 0 for w in waits)
                    and waits == sorted(waits), "Expected serial BFCL readers with registered provider backoffs")
        yield batch, waits


def reader_attempts(retry, waits):
    """Apply the registered retry limit or the new batch's shared reservation limits."""
    return count() if waits else range(1 + len(retry["wait_seconds"]))


def native_request_limit(assigned, limits, retry):
    attempts = 1 + len(retry["wait_seconds"])
    return ((assigned["source"] + assigned.get("target_no_memory", 0)) * limits["source_calls"]
            + assigned["write"] * limits["writer_calls"]
            + assigned["solve"] * max(limits["preload_calls"], limits["on_demand_calls"]) * attempts)


def reader_retry_wire(events, reason):
    """Price the identical opening wire or the request denied by a mid-session funding stop."""
    wires = [e["payload"]["request_ref"] for e in events if e["type"] == "model.request_started"]
    return wires[-1] if reason == "STUDY_LIMIT" else wires[0]


def reader_outcome_runs(scan, study, job, retry, waits):
    """Every physical session for a new operations batch is a separate, chronological outcome row."""
    for attempt in reader_attempts(retry, waits):
        key = run_key(study, job, attempt)
        if key not in scan["starts"]:
            return
        if waits:
            while key in scan["starts"]:
                yield key
                key = run_key(study, key, "unsubmitted")
        else:
            yield last_run(scan, study, key)


def interrupted_reader_reason(events, blobs, retry):
    """Recover only a durable official grade or the last worker request's confirmed registered HTTP error."""
    if any(e["type"] == "outcome.scored" and e["payload"]["grading_status"] == "graded" for e in events):
        return "business_decision"
    event = next((e for e in reversed(events) if e["type"] in {"model.request_started", "model.request_finished"}), None)
    if event and event["type"] == "model.request_finished":
        payload = event["payload"]
        if payload["status"] == "error" and payload["delivery_status"] == "response_received" and payload["response_ref"]:
            response = blobs.get(payload["response_ref"])
            detail = response.get("error") if isinstance(response, dict) else None
            if isinstance(detail, dict) and detail.get("code") in retry["http_codes"]:
                return f"HTTP_{detail['code']}"
    return None


def seal_interrupted_reader(journal, run, retry):
    events = run_events(journal.scan, run)
    reason = interrupted_reader_reason(events, journal.blobs, retry)
    if reason is None:
        return None
    start = journal.scan["starts"][run]
    ctx = RunContext(journal.experiment_id, run, start["session_id"], start["condition_id"],
                     "diagnostic", "solve", "observed")
    graded = reason == "business_decision"
    disposition = dict(termination_reason=reason, grading_status="graded" if graded else "not_available",
                       end_snapshot_id=start["payload"]["start_snapshot_id"] if graded else None)
    closing = []
    if not any(e["type"] == "outcome.scored" for e in events):
        closing.append(ctx.at("grade").event("outcome.scored", dict(grading_status="not_available",
                       official_success=None, official_score=None, official_artifact_ref=None)))
    closing.append(ctx.at("seal").event("run.ended", disposition))
    journal.append(closing)
    return disposition


def note_binding(note):
    return {key: note[key] for key in ("source_task", "source_run", "writer_run", "snapshot")}


def cohort_journals(groups, pools):
    """Put tasks linked by any registered note in the same journal; disconnected pools share no state."""
    components = source_components((target, sources) for (target, _), sources in pools.items())
    directories = {}
    for component in components:
        name = "cohort" if len(components) == 1 else "cohort/" + component[0]
        directories.update(dict.fromkeys(component, name))
    journals = defaultdict(list)
    for group in groups:
        journals[directories[group[0]]].append(group)
    return journals


def completion(dest, journals, summary, *, cohort):
    logs = [f"{name}/log.jsonl" for name in journals]
    if cohort:
        require(all((dest / log).is_file() for log in logs), "Completed BFCL cohort is missing a registered journal")
    files = ["registration.json", "expenses.jsonl", *logs]
    return dict(summary=summary, files={f: digest((dest / f).read_bytes()) for f in files if (dest / f).is_file()})


def cohort_checkpoint(dest, journals, stage, *, create=False):
    """Later phases append to the same journals. Verify the previously committed byte prefixes."""
    path = dest / f"through-{stage}.json"
    names = {"registration.json", "expenses.jsonl", *(f"{name}/log.jsonl" for name in journals)}
    if path.exists():
        refs = read(path)
        require(set(refs) == names, "BFCL phase checkpoint differs from its registered journals")
        for name, ref in refs.items():
            file = dest / name
            if name == "expenses.jsonl" and not ref["bytes"] and not file.exists():
                require(ref["sha256"] == digest(b""), "Invalid empty expense checkpoint")
            else:
                require(file.is_file(), f"BFCL phase checkpoint is missing {name}")
                sealed_bytes(file, ref["sha256"], ref["bytes"])
    elif create:
        refs = {}
        for name in names:
            file = dest / name
            raw = b"" if name == "expenses.jsonl" and not file.exists() else file.read_bytes()
            refs[name] = dict(bytes=len(raw), sha256=digest(raw))
        fixed(path, refs)
    else:
        return None
    return refs


def writer_instruction(plan, root, form):
    writing = plan["writing"]
    return content_text(root, writing["instruction"]) + "\n" + content_text(root, writing["representations"][form])


def reader_config(p, plan, job, root):
    reader = plan["reading"][job["reader"]]
    mode = reader["read_mode"]
    notice = content_text(root, reader["notice"]) if "notice" in reader else ""
    cfg = dict(p["cfg"], memory_notice_by_delivery={mode: notice}, read_tool_descriptions=reader.get("tool_descriptions"))
    if is_cohort(plan):
        cfg["read_page_size"] = len(p["pools"][job["target"], job["draw"]])
    return cfg, mode


def target_outcome(folder, evidence, run, cfg, mode, items, case, is_empty, expected_start, opening_ref,
                   reservations, *, bank_items=None, cohort=False):
    scan, _, _ = evidence
    events = run_events(scan, run)
    end = terminal(events)
    blobs = BlobStore(folder)
    public = read_json(expected_start.public_input)
    tools = hosted_tools(public["functions"])
    require(blobs.get(scan["starts"][run]["payload"]["task_ref"]) == public["messages"][0],
            "BFCL run started on a different target question")
    choices = worker_choices(folder, run_id=run, evidence=evidence)["choices"]
    if any(step["delivery"] == "pending" for step in choices):
        deliveries = {start["payload"]["request_id"]: finish["delivery_status"]
                      for start, finish in request_measurements(events, reservations=reservations)}
        for step in choices:
            step["delivery"] = deliveries[step["request_id"]]
    require(opening_ref or (not choices and not end), "Missing BFCL target opening")
    if opening_ref:
        require(blobs.get(opening_ref) == expected_start.record(), "Changed registered BFCL target opening")
    if choices:
        first = blobs.get(choices[0]["request_ref"])
        require(normalized_first(first, cfg, mode, items) == dict(
            model=cfg["model"], messages=[dict(role="system", content=cfg["worker_instruction"]), *public["messages"]],
            tools=tools, **cfg["wire_parameters"],
        ), "BFCL reader did not receive the registered fresh target input")
    sent = [s for s in choices if s["delivery"] == "response_received"]
    first_worker = sent[0]["request_ref"]["sha256"] if sent else None
    reads = Counter(r["name"] for s in choices for r in s["reads"])
    returns = [e["payload"] for e in events if e["type"] == "memory.returned"]
    returned_fields = Counter(f for p in returns for item in p["items"] for f in item["fields"])
    included = Counter()
    answered_fields = set()
    for step in sent:
        fields = {f["field"] for f in step["visible_memory"]}
        included.update(fields)
        if step["status"] == "response":
            answered_fields.update(fields)
            included["body_with_response"] += "body" in fields
    grade, decision, success = None, None, None
    reason = end["termination_reason"] if end else "interrupted"
    if end:
        (score,) = [e["payload"] for e in events if e["type"] == "outcome.scored"]
        success = score["official_success"]
    if success is not None:
        grade = grade_reply(choices[-1]["response"], case[0], is_empty, tools)
        require(
            grade == blobs.get(score["official_artifact_ref"]) and grade["valid"] is success,
            "Target action disagrees with native BFCL grade",
        )
        decision = dict(correct=success, call=grade["any_call"])
    historical_policy_errors = {"private_read_limit", "malformed_business_call", "mixed_private_business_response",
                                "unavailable_memory_tool", "WRITER_OUTPUT_TRUNCATED"}
    if not cohort and choices and reason in historical_policy_errors:
        decision = dict(correct=False, call=None)
    if decision is not None:
        decision.update(request=choices[-1]["request_id"], body_seen="body" in answered_fields,
                        title_seen="title" in answered_fields)
    business_calls = None
    if any(step["status"] == "response" for step in choices) and all(
        step["status"] == "response" or step["delivery"] == "not_submitted" for step in choices
    ):
        business_calls = sum(
            call["function"]["name"] not in {"memory_get", "memory_list", "memory_store"}
            for step in choices if step["status"] == "response" for call in step["response"].get("tool_calls") or []
        )
    result = dict(
        success=success,
        status="graded" if success is not None else "missing",
        decision=decision,
        missing_reason=None if success is not None else reason,
        termination_reason=reason,
        official_components=grade,
        opening_sha256=digest(expected_start.public_input) if opening_ref else None,
        first_worker_sha256=first_worker,
        business_calls=business_calls,
        effort_from_first_worker=first_decision_effort(events, token_breakdown=True, reservations=reservations),
        token_usage_by_role=token_usage_by_role(events, reservations=reservations),
        run=run,
        reads=dict(reads),
        evidence=dict(
            status="completed" if end else "interrupted", first_worker=first_worker, dispatched_worker=len(sent),
            title_requests=included["title"], body_requests=included["body"],
            body_requests_with_response=included["body_with_response"], reads=dict(reads),
            returns=dict(Counter(p["operation"] for p in returns)), returned_titles=returned_fields["title"],
            returned_bodies=returned_fields["body"],
            unresolved_worker=sum(s["delivery"] in {"pending", "unknown"} for s in choices),
        ),
        billing=usage_from_events(events, reservations=reservations),
    )
    if cohort:
        policy_errors = historical_policy_errors | {"WRITER_INCOMPLETE_TOOL_CALLS", "incomplete_or_malformed_response"}
        result["policy_failure"] = reason if choices and reason in policy_errors else None
        result["evidence"]["answered_worker"] = sum(s["status"] == "response" for s in choices)
    if bank_items is not None:
        cutoff = decision["request"] if grade is not None else None
        result["item_exposure"] = item_exposure(bank_items, choices, events, cutoff, contents=items, blobs=blobs)
    return result


def audited_reader(folder, evidence, run, job, cfg, mode, notes, case, is_empty, target_start, reservations):
    scan, state, _ = evidence
    start = scan["starts"][run]
    head = start["payload"]
    parents = [parent for note in notes for parent in (note["source_run"], note["writer_run"])]
    require(head["fresh_session"] and head["source_run_ids"] == parents, "Wrong transfer session")
    cohort = job["bank"] in {"own", "mixed"}
    origin = "bfcl.cohort_assignment" if cohort else "bfcl.transfer_assignment"
    events = run_events(scan, run)
    assignments = [e["payload"] for e in events if e["payload"].get("origin") == origin]
    opening_ref = None
    if assignments:
        (assignment,) = assignments
        require(assignment["bank"] == job["bank"], "Changed reader bank")
        if cohort:
            require(assignment["sources"] == [note_binding(note) for note in notes], "Changed registered cohort pool")
        else:
            require(assignment["source_task"] == job["source"] and assignment["donor_snapshot"] == notes[0]["snapshot"],
                    "Changed donor or control")
        opening_ref = assignment["target_start_ref"]
    blobs = BlobStore(folder)
    items = snapshot_items(blobs, state, head["start_snapshot_id"])
    expected = [] if job["bank"] == "empty" else [item for note in notes for item in note["items"]]
    require(items == expected and (job["bank"] == "empty" or items), "Changed reader snapshot contents")
    bank_items = None
    if cohort:
        require(start["condition_id"] == job["condition"] and opening_ref, "Missing cohort assignment or condition")
        bank = state["banks"][head["bank_id"]]
        snapshot = state["snapshots"][head["start_snapshot_id"]]
        require(bank["owner_run"] == run and bank["parent_snapshot"] == head["start_snapshot_id"]
                and bank["policy"] == asdict(MemoryPolicy(True, "none", False, cfg["read_page_size"], read_mode=mode))
                and bank["heads"] == bank["start_heads"] == snapshot["heads"]
                and bank["order"] == bank["start_order"] == snapshot["order"],
                "Cohort session bank or retrieval policy changed")
        expected_snapshots = [note["snapshot"] for note in notes]
        if len(expected_snapshots) == 1:
            require(head["start_snapshot_id"] == expected_snapshots[0], "Own bank differs from its writer snapshot")
        else:
            require(snapshot["parent_snapshot_ids"] == expected_snapshots,
                    "Mixed bank differs from its registered writer snapshots")
        bank_items = [dict(note_binding(note), item_id=item["id"], version_id=item["version"])
                      for note in notes for item in note["items"]]
    result = target_outcome(folder, evidence, run, cfg, mode, items, case, is_empty, target_start, opening_ref,
                            reservations, bank_items=bank_items, cohort=cohort)
    own, = [note for note in notes if note["source_task"] == job["source"]]
    return dict(result, source_run=own["source_run"], writer_run=own["writer_run"], donor_snapshot=own["snapshot"])


def audited_writer(folder, evidence, plan, root, workspace, cfg, task, form, source, writer, end, reservations):
    scan, state, _ = evidence
    blobs = BlobStore(folder)
    stored = state["memory_writes"][writer]
    exp = read_public_experience(blobs, scan, stored["experience_ref"])
    feedback = WriterFeedback(plan["writing"]["feedback"])
    require(exp["source_run_id"] == source and scan["starts"][writer]["condition_id"] == form
            and stored["feedback"]["level"] == feedback.level, "Wrong transfer writer")
    template = writer_template(dict(model=cfg["model"], **cfg["wire_parameters"]),
                               writer_instruction(plan, root, form), completion_contract=REQUIRED_STORE_CONTRACT)
    template["messages"] = [template.pop("system"), *writer_inputs(blobs, scan, exp, feedback)]
    events = run_events(scan, writer)
    req = next(e["payload"] for e in events if e["type"] == "model.request_started")
    require(blobs.get(req["request_ref"]) == template, "Writer saw inputs outside its registered donor")
    snapshot = end["end_snapshot_id"]
    manifest = state["snapshots"][snapshot]
    bank = state["banks"][manifest["bank_id"]]
    items = snapshot_items(blobs, state, snapshot)
    stored_versions = {(e["payload"]["item_id"], e["payload"]["version_id"]): e["payload"]["body_ref"]
                       for e in events if e["type"] == "memory.stored"}
    require(bank["sealed"] and bank["owner_run"] == writer and bank["heads"] == manifest["heads"]
            and len(items) == 1 and stored_versions.get((items[0]["id"], items[0]["version"])) ==
            state["versions"][items[0]["version"]], "Writer snapshot lacks its sealed item version")
    success = scan["events"][exp["outcome_event_id"]]["payload"]["official_success"]
    return dict(
        form=form, source_task=task, source_run=source, writer_run=writer, snapshot=snapshot, source_success=success,
        feedback_success=success if feedback.level != "none" else None, items=items,
        public_experience_ref=dict(path=str((folder / stored["experience_ref"]["path"]).relative_to(workspace)),
                                   sha256=stored["experience_ref"]["sha256"]),
        token_usage_by_role=token_usage_by_role(events, reservations=reservations.get(writer, [])),
        source_token_usage_by_role=token_usage_by_role(run_events(scan, source), reservations=reservations.get(source, [])),
    )


def audited_baseline(folder, evidence, run, target, draw, index, p, reservations):
    scan, _, _ = evidence
    head = scan["starts"][run]
    events = run_events(scan, run)
    require(
        head["condition_id"] == "no_memory" and head["payload"]["fresh_session"]
        and head["payload"]["bank_id"] is None and head["payload"]["start_snapshot_id"] is None
        and head["payload"]["source_run_ids"] == [] and not any(e["type"].startswith("memory.") for e in events),
        "Target no-memory run received a prior session or memory",
    )
    roles = [e["payload"]["actor_role"] for e in events if e["type"] == "model.request_started"]
    require(roles in ([], ["worker"]), "Unexpected participant or extra decision in a BFCL no-memory control")
    (assignment,) = [e["payload"] for e in events if e["payload"].get("origin") == "bfcl.target_no_memory"]
    require(assignment["index"] == index and assignment["draw"] == draw, "Wrong target no-memory repetition")
    result = target_outcome(folder, evidence, run, p["cfg"], None, [], p["cases"][target], p["is_empty"],
                            p["starts"][target], assignment["target_start_ref"], reservations, cohort=bool(p["pools"]))
    return dict(index=index, **result)


def target_rows(plan, attempts, **receipts):
    rows = allocation_rows(plan, attempts, **receipts)
    for row in rows:
        for solve in [row, *row["target_baselines"]]:
            solve.setdefault("opening_sha256", None)
            solve.setdefault("first_worker_sha256", None)
            solve.setdefault("business_calls", None)
            if is_cohort(plan):
                solve.setdefault("policy_failure", None)
            solve["state_diagnostics"] = dict(status="not_applicable", reason="bfcl_relevance_has_no_database")
            solve["dialogue"] = dict(status="not_applicable", reason="single_native_question_without_customer_simulator")
    return rows


def require_credentials():
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        raise ProviderError("OPENROUTER_API_KEY_MISSING")


def collect_source(journal, guard, plan, p, task, draw, client):
    anchor = None
    if is_cohort(plan):
        anchor = dict(target_start=p["starts"][task], index=1, draw=draw, writer_source=True)
    return once(
        journal, guard, run_key(plan["id"], task, draw, "source"), "source",
        lambda run: solve(journal, p["cfg"], p["cases"][task], client, p["is_empty"], run_id=run,
                          target_baseline=anchor),
    )


def collect_writers(journal, guard, plan, root, p, task, draw, first, client):
    source_run, end = first
    if not end or end["grading_status"] != "graded" or guard.denied:
        return {}
    experience = freeze_public_experience(journal, source_run)
    cfg, notes = p["cfg"], {}
    for form in p["forms"]:
        if guard.denied:
            break
        instruction = writer_instruction(plan, root, form)
        writer, end = once(
            journal, guard, run_key(plan["id"], task, draw, "write", form), "write",
            lambda run: write_from_experience(
                journal, experience, WriterFeedback(plan["writing"]["feedback"]), instruction=instruction,
                policy=MemoryPolicy(True, "post_task", False, 1, read_mode="none", max_items=1),
                client=client, model_config=dict(model=cfg["model"], **cfg["wire_parameters"]),
                max_requests=cfg["limits"]["writer_calls"], condition_id=form, run_id=run,
                completion_contract=REQUIRED_STORE_CONTRACT,
            ),
        )
        if end and end["termination_reason"] == "memory_write_completed":
            notes[task, draw, form] = dict(source_task=task, source_run=source_run, writer_run=writer,
                                          snapshot=end["end_snapshot_id"])
    return notes


def job_sources(p, job):
    if job["bank"] == "mixed":
        return p["pools"][job["target"], job["draw"]]
    return [job["source"]]


def collect_reader(journal, guard, plan, root, p, job, notes, client, sleep, *, waits=()):
    members = job_sources(p, job)
    draw, form = job["draw"], job["form"]
    if guard.denied or any((task, draw, form) not in notes for task in members):
        return
    source, target = job["source"], job["target"]
    own = notes[source, draw, form]
    writer = dict(run_id=own["writer_run"], snapshot_id=own["snapshot"])
    transfer = dict(source_task=source, source_run=own["source_run"], target_start=p["starts"][target], bank=job["bank"])
    if is_cohort(plan):
        transfer.update(pool=[note_binding(notes[task, draw, form]) for task in members], condition=job["condition"])
    cfg, mode = reader_config(p, plan, job, root)
    retry = plan["retry"]
    retryable = {f"HTTP_{code}" for code in retry["http_codes"]}
    previous = end = None
    schedule = waits or retry["wait_seconds"]
    for attempt in reader_attempts(retry, waits):
        key = run_key(plan["id"], job, attempt)
        if attempt and key not in journal.scan["starts"]:
            if waits:
                wire = reader_retry_wire(run_events(journal.scan, previous), end["termination_reason"])
                if not guard.can_reserve(request_reservation_usd(journal.blobs.get(wire), client.model)):
                    guard.denied = True
                    return
            if end["termination_reason"] in retryable:
                sleep(retry_delay(journal, previous, schedule[min(attempt, len(schedule)) - 1]))
        previous, end = once(
            journal, guard, key, "solve",
            lambda run: solve(journal, cfg, p["cases"][target], client, p["is_empty"], run_id=run, writer=writer,
                              form=form, mode=mode, transfer=transfer),
        )
        if guard.denied or not end:
            return
        if end["termination_reason"] not in retryable and not (waits and end["termination_reason"] == "STUDY_LIMIT"):
            return


def verify_writer_sources(plan, dest, prepared, sources):
    """Audit the released native first passes and any writer progress without opening a journal for writing."""
    study = plan["id"]
    require(is_cohort(plan) and plan["first_pass"]["draws"] == 1
            and set(sources) == set(plan["benchmark"]["tasks"]), "Expected one released BFCL source per cohort task")
    require(read(dest / "registration.json")["preflight"] == prepared["record"], "BFCL source registration changed")
    journals = prepared["journals"]
    source_refs = cohort_checkpoint(dest, journals, "source")
    require(source_refs is not None and not (dest / "completion.json").exists(),
            "A writer continuation needs a completed source checkpoint and no reader completion")
    require((dest / "registration.json").stat().st_size == source_refs["registration.json"]["bytes"],
            "BFCL source registration has bytes beyond its checkpoint")
    write_refs = cohort_checkpoint(dest, journals, "write")
    bound = (dest / "write-continuation.json").is_file()
    require(bound or write_refs is None, "BFCL writer checkpoint has no continuation")
    if write_refs or not bound:
        for name, ref in (write_refs or source_refs).items():
            require((dest / name).stat().st_size == ref["bytes"], f"Unregistered activity after BFCL checkpoint: {name}")
    expenses = read_run_reservations(dest / "expenses.jsonl")
    firsts, stages = {}, {}
    settled_slots, stored_slots = [], []
    first_pending_seen, deferred_reservation = False, None
    for name, groups in journals.items():
        folder = dest / name
        evidence = read_experiment(folder)
        scan, _, experiment = evidence
        require(experiment == study and not scan["tail_bytes"], "Invalid or incomplete BFCL continuation journal")
        ref = source_refs[f"{name}/log.jsonl"]
        cutoff = sealed_bytes(folder / "log.jsonl", ref["sha256"], ref["bytes"]).count(b"\n")
        source_runs = {e["run_id"] for row in scan["records"][:cutoff] for e in row["events"]}
        local_sources, local_writers = set(), set()
        blobs = BlobStore(folder)
        for task, draw, _ in groups:
            release = sources[task]
            run = run_key(study, task, draw, "source")
            while run in source_runs and run != release["run_id"]:
                events = run_events(scan, run)
                require(not submitted_or_ended(events, terminal(events), run in expenses),
                        "Released BFCL source replaces a submitted first pass")
                local_sources.add(run)
                run = run_key(study, run, "unsubmitted")
            require(run == release["run_id"] and run in source_runs, f"Wrong BFCL first pass for {task}")
            local_sources.add(run)
            baseline = audited_baseline(folder, evidence, run, task, draw, 1, prepared, expenses.get(run, []))
            events = run_events(scan, run)
            end = terminal(events)
            require(end and scan["starts"][run]["record_kind"] == "observed"
                    and baseline["success"] is release["success"], f"Changed BFCL source outcome for {task}")
            if baseline["success"] is None:
                seal, = [row["record_sha256"] for row in scan["records"][:cutoff]
                         if any(e["run_id"] == run and e["type"] == "run.ended" for e in row["events"])]
                require(seal == release["seal"] and not release["public_context_sha256"],
                        f"Changed ungraded BFCL source for {task}")
                firsts[task] = (run, end)
                continue
            experience = _experience(blobs, scan, run)
            context, _ = writer_inputs(blobs, scan, experience, WriterFeedback(plan["writing"]["feedback"]))
            require(experience["source_commit_sha256"] == release["seal"]
                    and digest(context["content"].encode()) == release["public_context_sha256"],
                    f"Changed BFCL writer source seal or public input for {task}")
            firsts[task] = (run, end)
            for form in prepared["forms"]:
                writer = run_key(study, task, draw, "write", form)
                done = False
                writer_events = []
                while writer in scan["starts"]:
                    head = scan["starts"][writer]
                    require(not done and head["stage"] == "collection" and head["condition_id"] == form
                            and head["payload"]["source_run_ids"] == [run] and head["payload"]["fresh_session"]
                            and head["payload"]["task_ref"] == experience["task_ref"],
                            "Repeated or misassigned BFCL writer")
                    local_writers.add(writer)
                    writer_events = run_events(scan, writer)
                    requests = list(request_measurements(writer_events, reservations=expenses.get(writer, [])))
                    require(len(requests) <= prepared["cfg"]["limits"]["writer_calls"]
                            and all(start["payload"]["actor_role"] == "writer" for start, _ in requests),
                            "Unexpected role or extra request in BFCL writing")
                    end = terminal(writer_events)
                    done = submitted_or_ended(writer_events, end, writer in expenses)
                    if end and end["termination_reason"] == "memory_write_completed":
                        stored_slots.append((task, draw, form))
                    writer = run_key(study, writer, "unsubmitted")
                if done:
                    settled_slots.append((task, draw, form))
                elif not first_pending_seen:
                    first_pending_seen = True
                    if any(e["type"] == "runtime.error" and e["payload"]["code"] == "STUDY_LIMIT" for e in writer_events):
                        last = next(e for e in reversed(writer_events) if e["type"] == "model.request_started")
                        wire = blobs.get(last["payload"]["request_ref"])
                        deferred_reservation = str(request_reservation_usd(wire, prepared["terms"]))
        writer_banks = {scan["starts"][w]["payload"]["bank_id"] for w in local_writers}
        require(local_sources == source_runs and set(scan["starts"]) == local_sources | local_writers
                and all(e["run_id"] in local_writers or (
                    e["run_id"] is None and e["type"] == "bank.snapshot_created"
                    and e["payload"]["bank_id"] in writer_banks and not e["payload"]["parent_snapshot_ids"])
                    for row in scan["records"][cutoff:] for e in row["events"]),
                "Unregistered BFCL source, writer or reader activity")
        require(stages.keys().isdisjoint(scan["starts"]), "BFCL continuation journals share a run")
        stages.update(dict.fromkeys(local_sources, "source"))
        stages.update(dict.fromkeys(local_writers, "write"))
    require(all(stages.get(run) == entry["stage"] and entry["model"] == prepared["cfg"]["model"]
                for run, entries in expenses.items() for entry in entries),
            "BFCL reservation outside the registered stage, model or run")
    eligible = sum(source["success"] is not None for source in sources.values()) * len(prepared["forms"])
    require(not write_refs or len(settled_slots) == eligible, "BFCL writer checkpoint is missing registered assignments")
    ref = source_refs["expenses.jsonl"]
    source_expense_rows = sealed_bytes(dest / "expenses.jsonl", ref["sha256"], ref["bytes"]).count(b"\n")
    return dict(firsts=firsts, eligible=eligible, settled=len(settled_slots), stored=len(stored_slots),
                settled_slots=settled_slots, stored_slots=stored_slots, source_expense_rows=source_expense_rows,
                deferred_reservation=deferred_reservation)


class WriterExpenseLedger(ExpenseLedger):
    """Replay the original source ceiling through its byte checkpoint, then apply the separate writer allowance."""

    def __init__(self, path, source_rows, source_cap, source_calls, writer_cap, writer_calls):
        require(source_rows > 0, "BFCL source checkpoint contains no expense reservations")
        self.remaining_source_rows = source_rows
        self.writer_cap, self.writer_calls = approval_amount(writer_cap), writer_calls
        super().__init__(path, source_cap, source_calls, float("inf"))
        require(self.remaining_source_rows == 0, "Incomplete BFCL source expense prefix")

    def apply(self, row):
        if self.remaining_source_rows:
            require("reserved" not in row or row["stage"] == "source", "Non-source request inside BFCL source checkpoint")
            super().apply(row)
            self.remaining_source_rows -= 1
            if not self.remaining_source_rows:
                self.source_spent, self.source_held, self.source_calls = self.spent, self.held, self.calls
                self.cap = self.spent + self.held + self.writer_cap
                self.max_calls = self.calls + self.writer_calls
        else:
            require(row["seq"] > self.source_calls and ("reserved" not in row or row["stage"] == "write"),
                    "Non-writer expense after BFCL source checkpoint")
            super().apply(row)

    def summary(self, *, stored, settled, eligible):
        return dict(completed_through=None if self.denied else "write", budget_deferred=self.denied,
                    reported_usd=str(self.spent - self.source_spent or 0),
                    reserved_usd=str(self.held - self.source_held or 0), requests=self.calls - self.source_calls,
                    stored_writers=stored, settled_writers=settled, eligible_writers=eligible)


class BfclTransfer:
    name = "bfcl-relevance"
    execution_mode = "hosted"

    def resolve(self, plan, workspace, root, *, checkout=None):
        require_declared_models(plan)
        require(not plan["first_pass"].get("reuse"), "Hosted BFCL collects its own no-memory donors")
        profile = model_profile(plan, root)
        worker = read_json(asset_text(root, profile["connection"]).encode())["worker"]
        terms = profile["endpoint_terms"]["worker"]
        require(
            worker["model_id"] == terms["model"] and worker["provider"]["only"] == [terms["provider"]],
            "Worker profile differs from the hosted transport",
        )
        setup = read_json(asset_text(root, plan["benchmark"]["setup"]).encode())
        require(
            setup["format"] == "bfcl-hosted-transfer-setup/v1" and setup["revision"] == REVISION, "Wrong BFCL setup"
        )
        checkout = Path(checkout or Path(workspace) / ".local/benchmark-portability/bfcl-6ea5797")
        categories = defaultdict(list)
        for task in plan["benchmark"]["tasks"]:
            cat = task.rsplit("_", 1)[0]
            require(cat in {"live_relevance", "irrelevance"}, "Unsupported BFCL task category")
            categories[cat].append(task)
        cases, is_empty = load_inputs(checkout, dict(setup, tasks=categories))
        params = dict(worker["parameters"], stream=False, provider=worker["provider"])
        gen = dict(seed=params["seed"], worker_tokens=params["max_completion_tokens"])
        cfg = dict(
            setup,
            model=terms["model"],
            model_revision=profile["expected_revisions"]["worker"],
            generation=gen,
            wire_parameters=params,
        )
        starts, by_id = {}, {}
        for cat, row in cases:
            native = open_relevance(
                row, cat, dict(model=cfg["model"], revision=cfg["model_revision"], generation=gen)
            )
            hosted_tools(row["function"])
            starts[row["id"]], by_id[row["id"]] = native, (cat, row)
        source_inputs = {starts[p["source"]].public_input for p in plan["transfer"]["pairs"]}
        for p in plan["transfer"]["pairs"]:
            if is_cohort(plan):
                require(p["target"] == p["source"], "A cohort writer must use its own target first pass")
            else:
                require(starts[p["target"]].public_input not in source_inputs, "Duplicate source and target public input")
            require(
                p["decision"]["expected"] == (by_id[p["target"]][0] == "live_relevance"), "Incorrect target rubric"
            )
        groups = list(schedule(plan))
        forms = list(dict.fromkeys(rep for rep, _ in conditions(plan)))
        assigned = dict(
            source=len(groups), write=len(groups) * len(forms), solve=sum(len(jobs) for _, _, jobs in groups)
        )
        record = dict(
            format="agent-memory-transfer-resolved/v1",
            adapter=self.name,
            plan_sha256=plan_digest(plan),
            revision=REVISION,
            worker=cfg["model"],
            model_revision=cfg["model_revision"],
            cases=[dict(task=t, public_input_sha256=digest(s.public_input)) for t, s in starts.items()],
            stages=["source", "write", "solve"],
            assigned=assigned,
        )
        pools = {}
        if is_cohort(plan):
            require(plan["transfer"].get("target_no_memory_repeats", 1) == 1,
                    "The cohort first pass is the single target baseline")
            pools = {(task, draw): pool_sources(plan, task, draw) for task, draw, _ in groups}
            journals = cohort_journals(groups, pools)
            record.update(
                first_pass_baseline="source",
                cohort_pools=[dict(target=task, draw=draw, own=[task], mixed=mixed, list_page_size=len(mixed))
                              for (task, draw), mixed in pools.items()],
                cohort_journals=[dict(directory=name, tasks=[t for t, _, _ in local])
                                 for name, local in journals.items()],
            )
        elif "target_no_memory_repeats" in plan["transfer"]:
            repeats = plan["transfer"]["target_no_memory_repeats"]
            require(type(repeats) is int and repeats >= 1, "Register a positive number of target no-memory solves")
            targets = dict.fromkeys(pair["target"] for pair in plan["transfer"]["pairs"])
            controls = [
                dict(target=t, draw=d, repeats=repeats)
                for t in targets for d in range(1, plan["first_pass"]["draws"] + 1)
            ]
            record["target_no_memory"] = controls
            assigned["target_no_memory"] = len(controls) * repeats
            record["stages"].insert(0, "target_no_memory")
            groups = [(c["target"], c["draw"], None) for c in controls] + groups
        if not is_cohort(plan):
            journals = defaultdict(list)
            for group in groups:
                journals[f"{group[0]}--{group[1]}"].append(group)
        return dict(
            record=record,
            checkout=checkout,
            cfg=cfg,
            cases=by_id,
            starts=starts,
            groups=groups,
            pools=pools,
            journals=journals,
            forms=forms,
            is_empty=is_empty,
            worker=worker,
            terms=terms,
        )

    def run_writers(self, plan, workspace, root, *, resolved, registration, continuation, sources, progress=None,
                    model_resolver=hosted_model, client_factory=OpenRouterClient):
        """Continue only the registered writers from the released, native source journals."""
        from agent_memory_eval.study.writer_stage import verify_continuation

        dest = Path(workspace) / "runs" / plan["id"]
        require(dest.is_dir(), "Missing original BFCL study")
        with stage_lease(dest):
            cap = verify_continuation(plan, workspace, root, continuation, registration, sources)
            current = self.resolve(plan, workspace, root, checkout=resolved.get("checkout"))
            require(current["record"] == resolved["record"], "BFCL writer schedule changed since preflight")
            resolved = current
            scope = dest / "write-continuation.json"
            require(read(dest / "registration.json") == registration
                    and registration["approval"].get("through_stage") == "source"
                    and registration["worker"] == resolved["terms"],
                    "BFCL writer continuation differs from its source-only registration")
            authorization = read(dest / "authorization.json")
            require({k: v for k, v in authorization.items() if k != "max_requests"} == registration["approval"],
                    "Original BFCL authorization changed")
            if scope.exists():
                require(read(scope) == continuation, "BFCL writer continuation changed")
            status = verify_writer_sources(plan, dest, resolved, sources)
            cfg, eligible = resolved["cfg"], status["eligible"]
            guard = WriterExpenseLedger(
                dest / "expenses.jsonl", status["source_expense_rows"], registration["approval"]["max_usd"],
                resolved["record"]["assigned"]["source"] * cfg["limits"]["source_calls"],
                cap, eligible * cfg["limits"]["writer_calls"],
            )
            require(guard.source_spent + guard.source_held == Decimal(continuation["prior_source_covered_usd"])
                    and eligible == continuation["eligible_writers"], "BFCL source costs or writer eligibility changed")
            if status["settled"] == eligible:
                require(scope.is_file(), "Completed BFCL writers have no recorded continuation")
                cohort_checkpoint(dest, resolved["journals"], "write", create=True)
                return guard.summary(stored=status["stored"], settled=status["settled"], eligible=eligible)
            deferred = status["deferred_reservation"]
            if deferred is not None and not guard.can_reserve(Decimal(deferred)):
                guard.denied = True
                return guard.summary(stored=status["stored"], settled=status["settled"], eligible=eligible)
            if model_resolver is hosted_model or client_factory is OpenRouterClient:
                require_credentials()
            model = model_resolver(resolved["worker"])
            require(endpoint_terms(dict(worker=model))["worker"] == resolved["terms"]
                    and model["canonical_slug"] == cfg["model_revision"], "Hosted worker revision or endpoint terms changed")
            limits = plan.get("limits", {})
            writer = client_factory(model, guard, route=resolved["worker"]["provider"],
                                    max_input_bytes=limits.get("writer_max_input_bytes", limits.get("max_input_bytes", 128000)))
            if isinstance(writer, OpenRouterClient):
                require_credentials()
            fixed(scope, continuation)
            count = 0
            stored, settled = set(status["stored_slots"]), set(status["settled_slots"])
            for name, groups in resolved["journals"].items():
                with Journal(dest / name, plan["id"], contract_version="1.2") as journal:
                    for task, draw, _ in groups:
                        notes = collect_writers(journal, guard, plan, root, resolved, task, draw,
                                                status["firsts"][task], writer)
                        stored.update(notes)
                        for form in resolved["forms"]:
                            key = run_key(plan["id"], task, draw, "write", form)
                            run = last_run(journal.scan, plan["id"], key)
                            if run:
                                events = run_events(journal.scan, run)
                                if submitted_or_ended(events, terminal(events), run in guard.reserved_runs):
                                    settled.add((task, draw, form))
                        count += 1
                        if progress:
                            progress("write", count, len(resolved["groups"]), guard.spent - guard.source_spent)
                        if guard.denied:
                            return guard.summary(stored=len(stored), settled=len(settled), eligible=eligible)
            require(len(settled) == eligible, "BFCL writing finished without processing every eligible slot")
            cohort_checkpoint(dest, resolved["journals"], "write", create=True)
            return guard.summary(stored=len(stored), settled=len(settled), eligible=eligible)

    def run(
        self,
        plan,
        workspace,
        root,
        *,
        approval,
        increases=(),
        checkout=None,
        progress=None,
        model_resolver=hosted_model,
        client_factory=OpenRouterClient,
        identity=None,
        sleep=time.sleep,
        through_stage=None,
    ):
        grants = [approval, *increases]
        check_through_stage(plan, through_stage, approved_through=grants[-1].get("through_stage"))
        prepared = self.resolve(plan, workspace, root, checkout=checkout)
        record, cfg = prepared["record"], prepared["cfg"]
        require(
            all(
                g == approval_for(plan, record, max_usd=g["max_usd"], user_authorization=g["user_authorization"],
                                  through_stage=g.get("through_stage"))
                and g["user_authorization"].strip()
                and approval_amount(g["max_usd"]) <= Decimal(plan["budget"]["max_usd"])
                for g in grants
            ),
            "Spending is not authorized for this transfer",
        )
        dest = Path(workspace) / "runs" / plan["id"]
        dest.mkdir(parents=True, exist_ok=True)
        assigned = record["assigned"]
        limits = cfg["limits"]
        max_calls = native_request_limit(assigned, limits, plan["retry"])
        with stage_lease(dest):
            implementation = identity or implementation_identity(root)
            if (dest / "registration.json").is_file():
                implementation = registration_freeze(
                    root, plan["id"], read(dest / "registration.json")["implementation"], implementation
                )
            fixed(
                dest / "registration.json",
                dict(preflight=record, approval=approval, implementation=implementation, worker=prepared["terms"]),
            )
            guard = ExpenseLedger(dest / "expenses.jsonl", grants[-1]["max_usd"], max_calls, float("inf"))
            register_grants(dest, grants, max_calls, guard.calls)
            closed = dest / "completion.json"
            if is_cohort(plan):
                for stage in ("source", "write"):
                    cohort_checkpoint(dest, prepared["journals"], stage)
            if closed.is_file():
                summary = read(closed)["summary"]
                require(
                    read(closed) == completion(dest, prepared["journals"], summary, cohort=is_cohort(plan)),
                    "Changed completed BFCL recording"
                )
                return dict(summary, completed_through=through_stage) if through_stage else summary
            if model_resolver is hosted_model or client_factory is OpenRouterClient:
                require_credentials()
            model = model_resolver(prepared["worker"])
            require(
                endpoint_terms(dict(worker=model))["worker"] == prepared["terms"]
                and model["canonical_slug"] == cfg["model_revision"],
                "Hosted worker revision or endpoint terms changed",
            )
            client = client_factory(
                model,
                guard,
                route=prepared["worker"]["provider"],
                max_input_bytes=plan.get("limits", {}).get("max_input_bytes", 128000),
            )
            writer = client_factory(
                model,
                guard,
                route=prepared["worker"]["provider"],
                max_input_bytes=plan.get("limits", {}).get("writer_max_input_bytes", client.max_input_bytes),
            )
            if isinstance(client, OpenRouterClient) or isinstance(writer, OpenRouterClient):
                require_credentials()
            if is_cohort(plan):
                self._cohort(plan, root, dest, prepared, guard, client, writer, sleep, progress, through_stage)
            else:
                for index, (task, draw, jobs) in enumerate(prepared["groups"], 1):
                    self._group(plan, root, dest, prepared, task, draw, jobs, guard, client, writer, sleep)
                    if progress:
                        progress("transfer", index, len(prepared["groups"]), guard.spent)
                    if guard.denied:
                        break
            summary = dict(
                reported_usd=str(guard.spent),
                reserved_usd=str(guard.held),
                requests=guard.calls,
                budget_deferred=guard.denied,
            )
            if through_stage is not None:
                if not guard.denied:
                    cohort_checkpoint(dest, prepared["journals"], through_stage, create=True)
                summary["completed_through"] = None if guard.denied else through_stage
            elif not guard.denied:
                fixed(closed, completion(dest, prepared["journals"], summary, cohort=is_cohort(plan)))
            return summary

    def _group(self, plan, root, dest, p, task, draw, jobs, guard, client, writer_client, sleep):
        with Journal(dest / f"{task}--{draw}", plan["id"], contract_version="1.2") as journal:
            if jobs is None:
                for index in range(1, plan["transfer"]["target_no_memory_repeats"] + 1):
                    if guard.denied:
                        break
                    once(
                        journal, guard, run_key(plan["id"], task, draw, "target_no_memory", index),
                        "target_no_memory",
                        lambda run: solve(
                            journal, p["cfg"], p["cases"][task], client, p["is_empty"], run_id=run,
                            target_baseline=dict(target_start=p["starts"][task], draw=draw, index=index),
                        ),
                    )
                return
            first = collect_source(journal, guard, plan, p, task, draw, client)
            notes = collect_writers(journal, guard, plan, root, p, task, draw, first, writer_client)
            for job in jobs:
                collect_reader(journal, guard, plan, root, p, job, notes, client, sleep)

    def _cohort(self, plan, root, dest, p, guard, client, writer_client, sleep, progress, through_stage=None):
        firsts, notes = {}, {}
        for phase in ("source", "write", "solve"):
            count = 0
            for name, groups in p["journals"].items():
                if guard.denied:
                    return
                with Journal(dest / name, plan["id"], contract_version="1.2") as journal:
                    for task, draw, jobs in groups:
                        if guard.denied:
                            return
                        if phase == "source":
                            firsts[task, draw] = collect_source(journal, guard, plan, p, task, draw, client)
                        elif phase == "write":
                            notes.update(collect_writers(journal, guard, plan, root, p, task, draw,
                                                         firsts[task, draw], writer_client))
                        else:
                            for job in jobs:
                                collect_reader(journal, guard, plan, root, p, job, notes, client, sleep)
                        count += 1
                        if progress:
                            progress(phase, count, len(p["groups"]), guard.spent)
            if phase == through_stage:
                return

    def outcomes(self, plan, workspace, root, *, checkout=None):
        p = self.resolve(plan, workspace, root, checkout=checkout)
        dest = Path(workspace) / "runs" / plan["id"]
        target_baselines = {}
        if not (dest / "registration.json").is_file():
            return target_rows(plan, [], target_baselines=target_baselines)
        require(read(dest / "registration.json")["preflight"] == p["record"], "BFCL transfer registration changed")
        study, cohort = plan["id"], is_cohort(plan)
        closed = dest / "completion.json"
        if closed.exists():
            seal = read(closed)
            require(seal == completion(dest, p["journals"], seal["summary"], cohort=cohort),
                    "Changed completed BFCL recording")
        reservations = read_run_reservations(dest / "expenses.jsonl")
        backoffs = {task: waits for batch, waits in bounded_reader_batches(dest, set(plan["benchmark"]["tasks"]))
                    for task in batch["tasks"]} if cohort else {}
        rows, written_memories = [], {}
        known_sessions, stages = set(), {}
        for name, groups in p["journals"].items():
            folder = dest / name
            if not (folder / "log.jsonl").exists():
                continue
            evidence = read_experiment(folder)
            scan, _, experiment = evidence
            require(experiment in {None, study}, "Journal belongs to another transfer study")
            starts = list(scan["starts"].values())
            sessions = [e["session_id"] for e in starts]
            require(len(set(sessions)) == len(sessions) and known_sessions.isdisjoint(sessions),
                    "BFCL transfer sessions must be independent")
            known_sessions.update(sessions)
            if cohort:
                phases = []
                for start in starts:
                    run = start["run_id"]
                    require(run not in stages, "BFCL cohort journals share a run id")
                    stage = "write" if start["stage"] == "collection" else (
                        "source" if start["payload"]["bank_id"] is None else "solve")
                    stages[run] = stage
                    phases.append(p["record"]["stages"].index(stage))
                require(phases == sorted(phases), "Cohort first passes, writers and readers ran out of phase")
            if not any(scan[k] for k in ("tail_bytes", "incomplete_runs", "pending_requests")):
                audit_requests(folder, evidence=evidence)
            source_ends = {}
            for task, draw, jobs in groups:
                if jobs is None:
                    controls = target_baselines[task, draw] = []
                    for i in range(1, plan["transfer"]["target_no_memory_repeats"] + 1):
                        run = last_run(scan, study, run_key(study, task, draw, "target_no_memory", i))
                        if run is not None:
                            controls.append(audited_baseline(folder, evidence, run, task, draw, i, p,
                                                             reservations.get(run, [])))
                    continue
                source_run = last_run(scan, study, run_key(study, task, draw, "source"))
                if source_run is None:
                    continue
                source_end = source_ends[task, draw] = terminal(run_events(scan, source_run))
                if cohort:
                    target_baselines[task, draw] = [audited_baseline(folder, evidence, source_run, task, draw, 1, p,
                                                                    reservations.get(source_run, []))]
                elif source_end and source_end["grading_status"] == "graded":
                    source_start = scan["starts"][source_run]["payload"]
                    public = read_json(p["starts"][task].public_input)
                    (req,) = [e["payload"] for e in run_events(scan, source_run) if e["type"] == "model.request_started"]
                    require(
                        source_start["fresh_session"] and source_start["bank_id"] is None and not source_start["source_run_ids"]
                        and BlobStore(folder).get(req["request_ref"]) == dict(
                            model=p["cfg"]["model"],
                            messages=[dict(role="system", content=p["cfg"]["worker_instruction"]), *public["messages"]],
                            tools=hosted_tools(public["functions"]), **p["cfg"]["wire_parameters"],
                        ),
                        "Unexpected BFCL donor input",
                    )
                for form in p["forms"]:
                    writer = last_run(scan, study, run_key(study, task, draw, "write", form))
                    if writer is None:
                        continue
                    end = terminal(run_events(scan, writer))
                    if end and end["termination_reason"] == "memory_write_completed":
                        written_memories[task, draw, form] = audited_writer(
                            folder, evidence, plan, root, workspace, p["cfg"], task, form, source_run,
                            writer, end, reservations,
                        )
            for source, draw, jobs in groups:
                if jobs is None:
                    continue
                for job in jobs:
                    target, form = job["target"], job["form"]
                    cfg, mode = reader_config(p, plan, job, root)
                    slot = dict(task=target, source=source, draw=draw, repeat=job["repeat"],
                                condition=job["condition"], origin=study)
                    members = job_sources(p, job)
                    missing = [task for task in members if (task, draw, form) not in written_memories]
                    runs = list(reader_outcome_runs(scan, study, job, plan["retry"], backoffs.get(target)))
                    if not runs and missing and closed.exists():
                        ungraded = any((source_ends.get((task, draw)) or {}).get("grading_status") != "graded"
                                       for task in missing)
                        rec = dict(slot, attempt=0, success=None, status="missing", decision=None,
                                   missing_reason="donor_ungraded" if ungraded else "writer_unavailable")
                        if cohort:
                            rec["unavailable_source_tasks"] = missing
                        rows.append(rec)
                    for attempt, run in enumerate(runs):
                        require(not missing, "Reader lacks a registered donor writer")
                        notes = [written_memories[task, draw, form] for task in members]
                        result = audited_reader(folder, evidence, run, job, cfg, mode, notes, p["cases"][target],
                                                p["is_empty"], p["starts"][target], reservations.get(run, []))
                        rows.append(dict(slot, attempt=attempt, **result))
        if cohort:
            submitted = sorted((entry for run in reservations.values() for entry in run), key=lambda e: e["seq"])
            require(all(stages.get(e["run"]) == e["stage"] for e in submitted),
                    "A cohort reservation belongs to the wrong journal stage")
            phases = [p["record"]["stages"].index(e["stage"]) for e in submitted]
            require(phases == sorted(phases), "Submitted requests crossed a global cohort phase barrier")
        return target_rows(plan, rows, written_memories=written_memories, target_baselines=target_baselines)
