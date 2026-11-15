"""Replay frozen worker requests one reply at a time; never run a business tool.

This is a retrospective prompt diagnostic, not a resumed episode or a memory
policy evaluation. Original wire seeds stay unchanged across repetitions.
"""

from collections import Counter
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import random

from jsonschema import Draft202012Validator, ValidationError

from agent_memory_eval.analysis.decision_replay import screen_reply
from agent_memory_eval.contracts.core import ContractError, RunContext, canonical, digest, new_id, read_json
from agent_memory_eval.contracts.validation import ROOT, validate_manifest
from agent_memory_eval.models.openrouter import OpenRouterClient, model_catalog, model_endpoints, resolve_endpoint, resolve_model
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry
from agent_memory_eval.runner.tau2_baseline import batch_stop, spend_guard


SELECTED = {("17", 1), ("23", 2), ("14", 2), ("42", 1), ("42", 2), ("39", 2), ("21", 1), ("20", 1)}
MEMO_PREFIX = "\n\nNotes from earlier work:\n"


def public_reply(message):
    return {k: deepcopy(message[k]) for k in ("role", "content", "tool_calls", "refusal") if k in message}


def load_ref(ref, text=None):
    if text is None:
        path = (ROOT / ref["path"]).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ContractError("Replay reference is outside the repository or missing")
        text = path.read_text()
    if digest(text.encode()) != ref["sha256"]:
        raise ContractError("Replay reference changed")
    return read_json(text.encode()), text


def case_key(row):
    return f"task-{row['task_id']}-rep-{row['repetition']}"


def case_metadata(row):
    return dict(case_id=case_key(row), task_id=row["task_id"], source_repetition=row["repetition"],
        origin_run_id=row["run_id"], message_index=row["decision"]["message_index"],
        source_request_id=row["decision"]["request_id"], source_request_sha256=row["decision"]["request_ref"]["sha256"],
        original_public_reply=row["decision"]["public_message"])


def selected_rows(diagnosis):
    if diagnosis.get("format") != "agent-memory-tau2-baseline-failure-diagnosis/v1":
        raise ContractError("Unsupported replay diagnosis")
    rows = [r for r in diagnosis["strict_replays"] if (r["task_id"], r["repetition"]) in SELECTED]
    if len(rows) != len(SELECTED) or len({case_key(r) for r in rows}) != len(SELECTED):
        raise ContractError("Replay requires the eight explicitly selected worker-error snapshots")
    return rows


def compile_replay(profile, *, require_ready=False, diagnosis_text=None, memo_text=None):
    try:
        canonical(profile)
        schema = read_json((ROOT / "schemas/decision-replay.schema.json").read_bytes())
        Draft202012Validator(schema).validate(profile)
    except (ValueError, TypeError, ValidationError) as error:
        raise ContractError("Invalid finite replay profile") from error
    diagnosis, diagnosis_text = load_ref(profile["diagnosis_ref"], diagnosis_text)
    memos, memo_text = load_ref(profile["memo_ref"], memo_text)
    if (memos.get("format") != "agent-memory-researcher-memos/v1"
            or memos.get("author_kind") != "researcher_curated" or memos.get("author_blinded_to_outcome") is not False):
        raise ContractError("This protocol compares researcher-curated memos; worker-written notes require a distinct arm/version")
    keys = {case_key(r) for r in selected_rows(diagnosis)}
    if set(memos["notes"]) != keys or any(not isinstance(t, str) or not t.strip() or len(t.encode()) > 2000 for t in memos["notes"].values()):
        raise ContractError("One bounded, nonempty procedural memo is required per snapshot")
    missing = [key for key in ("repetitions", "schedule_seed") if profile[key] is None]
    missing += ["guards." + key for key, value in profile["guards"].items() if value is None]
    ready = profile["status"] == "ready" and not missing
    if (require_ready or profile["status"] == "ready") and not ready:
        raise ContractError("Replay is not ready: " + ", ".join(missing or ["status=draft"]))
    return dict(profile=deepcopy(profile), diagnosis=diagnosis, diagnosis_text=diagnosis_text,
        memos=memos, memo_text=memo_text, ready=ready, unresolved_fields=missing)


def make_request(original, condition, memo):
    request = deepcopy(original)
    if condition == "reasoning_medium":
        request["reasoning"] = dict(effort="medium")
    elif condition == "memo_researcher":
        request["messages"][0]["content"] += MEMO_PREFIX + memo
    elif condition != "original":
        raise ContractError("Unknown replay condition")
    return request


def allocation_rows(profile, case_ids):
    rng = random.Random(profile["schedule_seed"])
    blocks = [(case, rep) for rep in range(1, profile["repetitions"] + 1) for case in sorted(case_ids)]
    rng.shuffle(blocks)
    rows = []
    for block, (case, rep) in enumerate(blocks, 1):
        conditions = list(profile["conditions"])
        rng.shuffle(conditions)
        rows += [dict(block=block, case_id=case, repetition=rep, condition=condition) for condition in conditions]
    return rows


def preflight_replay(profile, *, require_ready=False):
    plan = compile_replay(profile, require_ready=require_ready)
    diagnosis = plan["diagnosis"]
    refs = {ref["path"]: load_ref(ref)[0] for ref in diagnosis["sources"]}
    review = next(v for v in refs.values() if v.get("format") == "agent-memory-tau2-baseline-behavior-review/v1")
    root = (ROOT / review["source"]["root"]).resolve()
    if not root.is_relative_to(ROOT):
        raise ContractError("Replay source is outside the repository")
    raw_log = (root / "log.jsonl").read_bytes()
    if digest(raw_log) != diagnosis["raw_log_sha256"]:
        raise ContractError("Original replay log changed")
    blobs = BlobStore(root)
    last = read_json(raw_log.splitlines()[-1])
    control = blobs.get(blobs.get(last["state_ref"])["baseline_screening"]["control_ref"])
    db_asset = control["assets"]["data/tau2/domains/airline/db.json"]
    db_text = blobs.get(db_asset["text_ref"])
    if digest(db_text.encode()) != db_asset["sha256"]:
        raise ContractError("Saved judging source changed")
    cases = {}
    for row in selected_rows(diagnosis):
        original = blobs.get(row["decision"]["request_ref"])
        if original.get("reasoning") != dict(effort="low") or original["provider"].get("allow_fallbacks") is not False:
            raise ContractError("Original worker request is not the frozen low-reasoning single-provider input")
        key = case_key(row)
        for condition in profile["conditions"]:
            if len(canonical(make_request(original, condition, plan["memos"]["notes"][key]))) > 64000:
                raise ContractError("Replay intervention exceeds the existing transport input limit")
        cases[key] = dict(case_metadata(row), request=original)
    plan.update(cases=cases, db_text=db_text, db_text_sha256=db_asset["sha256"])
    return plan


def prepare_replay(profile):
    plan = preflight_replay(profile, require_ready=True)
    original = next(iter(plan["cases"].values()))["request"]
    parameters = {k: v for k, v in original.items() if k not in {"model", "provider", "stream", "messages"}}
    model = resolve_model(model_catalog(), original["model"], parameters)
    model = resolve_endpoint(model, model_endpoints(model["id"]), original["provider"], parameters)
    return dict(plan=plan, model=model)


def run_replay(journal, profile, prepared, *, opener=None):
    if journal.contract_version != "1.1" or journal.scan["records"]:
        raise ContractError("Decision replay requires a new v1.1 journal")
    plan = preflight_replay(profile, require_ready=True)
    if plan != prepared["plan"]:
        raise ContractError("Prepared replay inputs changed")
    cases = {}
    for key, case in plan["cases"].items():
        cases[key] = {k: deepcopy(v) for k, v in case.items() if k != "request"}
        cases[key]["request_ref"] = journal.blobs.put(case["request"])
        if cases[key]["request_ref"]["sha256"] != case["source_request_sha256"]:
            raise ContractError("Original request copy changed")
    schedule = [dict(r, run_id=new_id("r"), session_id=new_id("s")) for r in allocation_rows(profile, list(cases))]
    control = dict(profile=profile, diagnosis_text=plan["diagnosis_text"], memo_text=plan["memo_text"],
        cases=cases, schedule=schedule, db_text_ref=journal.blobs.put(plan["db_text"]), db_text_sha256=plan["db_text_sha256"],
        model=prepared["model"], business_tool_execution=False, official_benchmark_score=False)
    state = deepcopy(journal.state)
    state["decision_replay"] = dict(control_ref=journal.blobs.put(control), runtime_status="running")
    journal.append([], state=state)
    controller = RunContext(journal.experiment_id, None, None, None, "controller", "prepare", "observed")
    guard, gateway = spend_guard(profile["guards"]), RecordingGateway(journal)
    stopped = None
    for allocation in schedule:
        stopped = stopped or batch_stop(guard)
        if stopped:
            journal.append([controller.event("business.transition", dict(origin="decision_replay.skipped", **allocation, reason=stopped))])
            continue
        case = plan["cases"][allocation["case_id"]]
        ctx = RunContext(journal.experiment_id, allocation["run_id"], allocation["session_id"], allocation["condition"], "diagnostic", "solve", "observed")
        journal.append([ctx.at("prepare").event("run.started", dict(bank_id=None, start_snapshot_id=None, fresh_session=True,
            source_run_ids=[case["origin_run_id"]], task_ref=journal.blobs.put(dict(case_id=allocation["case_id"]))))])
        request = make_request(case["request"], allocation["condition"], plan["memos"]["notes"][allocation["case_id"]])
        registry = SourceRegistry(journal, ctx)
        template = registry.template({k: v for k, v in request.items() if k != "messages"}, roles=("worker",))
        messages = [(registry.template(dict(message=m), roles=("worker",)), "/message") for m in request["messages"]]
        message_ref, request_id, code, judgement = None, None, None, None
        try:
            client = OpenRouterClient(prepared["model"], guard, route=request["provider"], **({"opener": opener} if opener else {}))
            message, _, request_id = gateway.call(ctx, registry, "worker", template, messages, client, max_requests=1)
            finish = next(e for e in reversed(list(journal.events.values())) if e["type"] == "model.request_finished" and e["run_id"] == ctx.run_id)
            response = journal.blobs.get(finish["payload"]["response_ref"])
            if (response.get("model") not in {prepared["model"]["id"], prepared["model"].get("canonical_slug")}
                    or response.get("provider") != prepared["model"]["selected_endpoint"]["provider_name"]):
                raise ContractError("Returned model/provider differs from the pinned replay route")
            message = public_reply(message)
            message_ref = journal.blobs.put(message)
            judgement = screen_reply(case["task_id"], message, read_json(plan["db_text"].encode()))
        except Exception as error:
            code = getattr(error, "code", type(error).__name__)
            finishes = [e for e in journal.events.values() if e["type"] == "model.request_finished" and e["run_id"] == ctx.run_id]
            if finishes:
                request_id = finishes[-1]["payload"]["request_id"]
            journal.append([ctx.event("runtime.error", dict(code=code))])
        journal.append([ctx.at("grade").event("business.transition", dict(origin="decision_replay.result",
            case_id=allocation["case_id"], request_id=request_id, message_ref=message_ref, error_code=code, judgement=judgement))])
        # These replies have no official task score. The argument screen stays in business.transition.
        journal.append([ctx.at("grade").event("outcome.scored", dict(grading_status="not_available", official_success=None,
            official_score=None, official_artifact_ref=None))])
        state = deepcopy(journal.state)
        state["provenance"] = {}
        journal.append([ctx.at("seal").event("run.ended", dict(termination_reason="single_reply" if code is None else str(code),
            grading_status="not_available", end_snapshot_id=None))], state=state)
        stopped = batch_stop(guard)
        if code:
            stopped = stopped or (code.lower() if code in {"SPEND_LIMIT", "CALL_LIMIT", "TIME_LIMIT"} else "runtime_error")
    state = deepcopy(journal.state)
    state["decision_replay"]["runtime_status"] = "finished"
    journal.append([controller.at("seal").event("business.transition", dict(origin="decision_replay.finished", stop_reason=stopped))], state=state)
    journal.export_views()
    return audit_replay(journal.root)


def audit_replay(root):
    root = Path(root)
    scan, state, experiment = read_experiment(root)
    if scan["tail_bytes"] or scan["incomplete_runs"] or scan["pending_requests"] or state.get("decision_replay", {}).get("runtime_status") != "finished":
        raise ContractError("Replay evidence is incomplete")
    blobs = BlobStore(root)
    control_ref = state["decision_replay"]["control_ref"]
    control = blobs.get(control_ref)
    plan = compile_replay(control["profile"], require_ready=True, diagnosis_text=control["diagnosis_text"], memo_text=control["memo_text"])
    if control["business_tool_execution"] is not False or control["official_benchmark_score"] is not False:
        raise ContractError("Replay was mislabeled as an episode or official score")
    originals = {case_key(r): r for r in selected_rows(plan["diagnosis"])}
    if set(control["cases"]) != set(originals):
        raise ContractError("Replay snapshot set changed")
    for key, case in control["cases"].items():
        if (case["request_ref"] != originals[key]["decision"]["request_ref"]
                or {k: v for k, v in case.items() if k != "request_ref"} != case_metadata(originals[key])):
            raise ContractError("Original snapshot hash changed")
    schedule = control["schedule"]
    if [{k: v for k, v in r.items() if k not in {"run_id", "session_id"}} for r in schedule] != allocation_rows(plan["profile"], list(control["cases"])):
        raise ContractError("Replay allocation changed")
    if len({r["run_id"] for r in schedule}) != len(schedule) or len({r["session_id"] for r in schedule}) != len(schedule):
        raise ContractError("Replay reused run/session identities")
    db_text = blobs.get(control["db_text_ref"])
    if (digest(db_text.encode()) != control["db_text_sha256"]
            or control["db_text_sha256"] != plan["diagnosis"]["native_source_hashes"]["data/tau2/domains/airline/db.json"]):
        raise ContractError("Judging data changed")
    db = read_json(db_text.encode())
    origins = {}
    for record in scan["records"]:
        head = blobs.get(record["state_ref"])
        if head["decision_replay"]["control_ref"] != control_ref or any(head[k] for k in ("banks", "versions", "snapshots", "cursors", "return_bytes", "assignments")):
            raise ContractError("Replay control or memory state changed")
        for event in record["events"]:
            if event["type"] == "model.request_started":
                origins[event["payload"]["request_id"]] = head["provenance"][event["payload"]["request_id"]]
    events = list(scan["events"].values())
    allowed_runs = {r["run_id"] for r in schedule}
    if any(e["record_kind"] != "observed" or e["type"].startswith("memory.")
            or e["run_id"] is not None and e["run_id"] not in allowed_runs for e in events):
        raise ContractError("Unexpected replay event or run")
    started = [e for e in events if e["type"] == "run.started"]
    skipped = [e for e in events if e["payload"].get("origin") == "decision_replay.skipped"]
    if ([e["run_id"] for e in started] != [r["run_id"] for r in schedule[:len(started)]]
            or [e["payload"]["run_id"] for e in skipped] != [r["run_id"] for r in schedule[len(started):]]):
        raise ContractError("Replay starts/skips differ from schedule order")
    finished = [e for e in events if e["payload"].get("origin") == "decision_replay.finished"]
    if len(finished) != 1 or finished[0] is not events[-1]:
        raise ContractError("Missing final replay completion")
    rows, calls, cost, unknown, tokens = [], 0, Decimal(0), False, 0
    for allocation in schedule:
        run_events = [e for e in events if e["run_id"] == allocation["run_id"]]
        skips = [e for e in events if e["payload"].get("origin") == "decision_replay.skipped" and e["payload"]["run_id"] == allocation["run_id"]]
        if skips:
            if run_events or len(skips) != 1 or skips[0]["payload"]["reason"] != finished[0]["payload"]["stop_reason"]:
                raise ContractError("Invalid skipped replay allocation")
            rows.append(dict(allocation, status="skipped", reason=skips[0]["payload"]["reason"]))
            continue
        starts = [e for e in run_events if e["type"] == "model.request_started"]
        finishes = [e for e in run_events if e["type"] == "model.request_finished"]
        results = [e for e in run_events if e["payload"].get("origin") == "decision_replay.result"]
        ends = [e for e in run_events if e["type"] == "run.ended"]
        outcomes = [e for e in run_events if e["type"] == "outcome.scored"]
        run_starts = [e for e in run_events if e["type"] == "run.started"]
        if not len(starts) == len(finishes) == len(results) == len(ends) == len(outcomes) == len(run_starts) == 1:
            raise ContractError("Replay must contain exactly one request and result per started run")
        case = control["cases"][allocation["case_id"]]
        expected = make_request(blobs.get(case["request_ref"]), allocation["condition"], plan["memos"]["notes"][allocation["case_id"]])
        began, finish, result = starts[0], finishes[0]["payload"], results[0]["payload"]
        if (any(e["session_id"] != allocation["session_id"] or e["condition_id"] != allocation["condition"] for e in run_events)
                or began["payload"]["actor_role"] != "worker" or run_starts[0] is not run_events[0]
                or run_starts[0]["payload"] != dict(bank_id=None, start_snapshot_id=None, fresh_session=True,
                    source_run_ids=[case["origin_run_id"]], task_ref=run_starts[0]["payload"]["task_ref"])
                or finish["request_id"] != began["payload"]["request_id"] or result["request_id"] != finish["request_id"]
                or result["case_id"] != allocation["case_id"]):
            raise ContractError("Replay request/run identity or fresh-session contract changed")
        if canonical(blobs.get(began["payload"]["request_ref"])) != canonical(expected):
            raise ContractError("Replay request changed outside the declared intervention")
        origin = origins[began["payload"]["request_id"]]
        manifest = blobs.get(began["payload"]["input_manifest_ref"])
        if validate_manifest(root, manifest, began, blobs.get(origin["sources_ref"]), scan["events"], origin["approved_templates"]):
            raise ContractError("Unexpected memory-service exposure in a prompt-only replay")
        if outcomes[0]["payload"] != dict(grading_status="not_available", official_success=None, official_score=None, official_artifact_ref=None):
            raise ContractError("Single-reply probe acquired an official task score")
        calls += finish["delivery_status"] != "not_submitted"
        unknown |= finish["cost_status"] == "unknown"
        tokens += (finish.get("usage") or {}).get("total_tokens", 0)
        if finish["response_ref"] is not None and blobs.get(finish["response_ref"]).get("usage") != finish["usage"]:
            raise ContractError("Replay usage differs from the original response")
        if finish["cost_status"] == "observed":
            response = blobs.get(finish["response_ref"])
            if Decimal(str(response["usage"]["cost"])) != Decimal(str(finish["cost_usd"])):
                raise ContractError("Replay cost differs from the response")
            cost += Decimal(str(finish["cost_usd"]))
        message = None
        reason = None
        if result["error_code"] is None:
            response = blobs.get(finish["response_ref"])
            message = public_reply(response["choices"][0]["message"])
            reason = response["choices"][0]["finish_reason"]
            if (blobs.get(result["message_ref"]) != message or result["judgement"] != screen_reply(case["task_id"], message, db)
                    or response.get("model") not in {control["model"]["id"], control["model"].get("canonical_slug")}
                    or response.get("provider") != control["model"]["selected_endpoint"]["provider_name"]):
                raise ContractError("Replay response or argument screen changed")
        rows.append(dict(allocation, status="response" if result["error_code"] is None else "error",
            error_code=result["error_code"], judgement=result["judgement"], finish_reason=reason,
            request_id=finish["request_id"], request_ref=began["payload"]["request_ref"], response_ref=finish["response_ref"],
            usage=finish["usage"], delivery_status=finish["delivery_status"], reported_cost_usd=finish["cost_usd"], cost_status=finish["cost_status"]))
    summary = dict(format="agent-memory-decision-replay-results/v1", intent=plan["profile"]["intent"], experiment_id=experiment,
        status="completed" if not unknown and all(r["status"] == "response" for r in rows) else "partial", planned=len(schedule),
        counts=dict(Counter(r["status"] for r in rows)), http_calls=calls, reported_cost_usd_decimal=str(cost), unknown_cost=unknown,
        reported_tokens=tokens, requests_by_role=dict(worker=calls),
        unknown_cost_requests=sum(r.get("cost_status") == "unknown" for r in rows),
        stop_reason=finished[0]["payload"]["stop_reason"], business_tool_execution=False, official_task_scores=False,
        memo_author_kind=plan["memos"]["author_kind"], log_sha256=digest((root / "log.jsonl").read_bytes()), runs=rows)
    path = root / "decision-replay-summary.json"
    if path.exists() and canonical(read_json(path.read_bytes())) != canonical(summary):
        raise ContractError("Replay summary differs from original evidence")
    return summary
