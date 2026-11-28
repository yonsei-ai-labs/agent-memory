"""Pair the effort spent on the same assignments, with explicit outcome and metric coverage."""

from collections import defaultdict
from decimal import Decimal
import json
from statistics import median

from agent_memory_eval.analysis.claim_evidence import paired_counts
from agent_memory_eval.contracts.core import ContractError, read_json
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.batch import request_measurements
from agent_memory_eval.recording.experience import _closed_run, _source
from agent_memory_eval.recording.journal import safe_path


METRICS = ("customer_turns", "business_calls", "memory_read_calls", "http_calls", "tokens", "usd")
END_SIGNALS = {"###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###"}


def request_effort_by_start(events, boundary, roles, *, reservations=None):
    """Count submitted or unresolved attempts by start phase, keeping unreported effort missing."""
    grouped = {"setup": [], "solve": []}
    for start, finish in request_measurements(events, reservations=reservations):
        if start["payload"]["actor_role"] in roles and finish["delivery_status"] != "not_submitted":
            phase = "setup" if start["event_seq"] < boundary else "solve"
            grouped[phase].append(finish)
    result = {}
    for name, rows in grouped.items():
        tokens = [(p.get("usage") or {}).get("total_tokens") for p in rows]
        costs = [p["cost_usd"] if p["cost_status"] == "observed" else None for p in rows]
        result[name] = dict(http_calls=len(rows), tokens=sum(tokens) if all(type(t) is int and t >= 0 for t in tokens) else None,
                            usd=str(sum((Decimal(str(v)) for v in costs), Decimal(0))) if None not in costs else None)
    return result


def token_usage_by_role(events, *, since=None, roles=None, reservations=None):
    """Token details for submitted or unresolved attempts. Missing components keep their totals missing."""
    result = {}
    for start, finish in request_measurements(events, reservations=reservations):
        role = start["payload"]["actor_role"]
        if (finish["delivery_status"] == "not_submitted" or (since is not None and start["event_seq"] < since)
                or (roles is not None and role not in roles)):
            continue
        cell = result.setdefault(role, dict(requests=0, input_tokens=0, output_tokens=0,
                                            cached_input_tokens=0, reasoning_tokens=0))
        cell["requests"] += 1
        usage = finish.get("usage") or {}
        prompt, completion = usage.get("prompt_tokens_details"), usage.get("completion_tokens_details")
        components = dict(input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                          cached_input_tokens=prompt.get("cached_tokens") if isinstance(prompt, dict) else None,
                          reasoning_tokens=completion.get("reasoning_tokens") if isinstance(completion, dict) else None)
        for name, value in components.items():
            cell[name] = cell[name] + value if cell[name] is not None and type(value) is int and value >= 0 else None
    return result


def combined_token_usage(measurements):
    """Sum distinct recorded sessions and retain missing usage components in each role."""
    totals = {}
    measured = missing = 0
    for roles in measurements:
        if roles is None:
            missing += 1
            continue
        measured += 1
        for role, values in roles.items():
            if role not in totals:
                totals[role] = values.copy()
                continue
            for field, value in values.items():
                previous = totals[role][field]
                totals[role][field] = None if previous is None or value is None else previous + value
    return dict(measured=measured, missing=missing, by_role=totals)


def first_decision_effort(events, *, token_breakdown=False, reservations=None):
    """Exclude setup, including generation of the first customer utterance, from both passes."""
    events = list(events)
    starts = [e["event_seq"] for e in events if e["type"] == "model.request_started"
              and e["payload"]["actor_role"] == "worker"]
    if not starts:
        return None
    roles = {"worker", "user_simulator"}
    if reservations is not None:
        reservations = tuple(reservations)
    solve = request_effort_by_start(events, min(starts), roles, reservations=reservations)["solve"]
    effort = dict(requests=solve["http_calls"], tokens=solve["tokens"], usd=solve["usd"])
    if token_breakdown:
        effort["by_role"] = token_usage_by_role(events, since=min(starts), roles=roles, reservations=reservations)
    return effort


def business_calls_by_role(messages):
    """Count issued business calls by speaker, including turns without speech. Receipts are separate."""
    counts = {"assistant": 0, "user": 0}
    for message in messages:
        for call in message.get("tool_calls") or []:
            if not call["name"].startswith("memory_"):
                role = message["role"]
                counts[role] = counts.get(role, 0) + 1
    return counts


def customer_questions(messages):
    """Locate explicit public questions and full-turn repeats after an intervening customer reply."""
    previous = {}
    questions = []
    customer_index = None
    for index, message in enumerate(messages):
        text = (message.get("content") or "").strip()
        if not text or text in END_SIGNALS or message.get("tool_calls"):
            continue
        if message["role"] == "user":
            customer_index = index
            continue
        if message["role"] != "assistant" or customer_index is None or "?" not in text:
            continue
        if text.startswith("{"):
            try:
                wrapper = json.loads(text)
            except ValueError:
                wrapper = None
            if isinstance(wrapper, dict):
                if wrapper.keys() != {"message"} or not isinstance(wrapper["message"], str):
                    continue
                text = wrapper["message"]
        if "?" not in text:
            continue
        wording = " ".join(text.casefold().split())
        row = dict(message_index=index, preceding_customer_index=customer_index)
        if wording in previous and previous[wording] < customer_index:
            row["repeated_from_index"] = previous[wording]
        previous[wording] = index
        questions.append(row)
    return questions


def _public_effort(messages):
    return dict(customer_turns=sum(m["role"] == "user" and bool(m.get("content"))
                                   and m["content"].strip() not in END_SIGNALS for m in messages),
                business_calls=business_calls_by_role(messages)["assistant"])


def episode_effort(row, public_messages, choices):
    """τ² second-pass effort. Source/writer charges are accounted for separately in the report."""
    result = row["result"]
    usage = [v for role, v in result.get("usage_by_role", {}).items() if role in {"worker", "user_simulator"}]
    measured = dict.fromkeys(METRICS)
    if usage:
        measured["http_calls"] = sum(v["http_attempts"] for v in usage)
        if all(v["unknown_token_requests"] == 0 for v in usage):
            measured["tokens"] = sum(v["reported_tokens"] for v in usage)
        if all(v["unknown_cost_requests"] == 0 for v in usage):
            measured["usd"] = str(sum(Decimal(v["reported_cost_usd_decimal"]) for v in usage))
    if result.get("grading_status") == "graded" and public_messages is not None:
        measured.update(_public_effort(public_messages))
        if all(s["status"] == "response" or s["delivery"] == "not_submitted" for s in choices):
            measured["memory_read_calls"] = sum(len(s["reads"]) for s in choices)
    return measured


def archived_effort(source, workspace, grades):
    """Reopen the sealed, same-task no-memory archive once. An unmounted archive leaves metrics missing."""
    folder = safe_path(workspace, source["root"])
    archive = dict(source_log_sha256=source["log_sha256"], status="unavailable", by_task={})
    if not folder.exists():
        return archive
    summary = read_json(asset_text(folder, dict(path="baseline-summary.json", sha256=source["summary_sha256"])).encode())
    reports = {r["run_id"]: r for r in summary["runs"]}
    with _source(folder, source["log_sha256"]) as (scan, blobs, _, experiment):
        if experiment != summary["experiment_id"]:
            raise ContractError("First-pass effort and summary name different experiments")
        by_run, roles = defaultdict(list), {}
        for e in scan["events"].values():
            if e["type"] == "model.request_finished":
                by_run[e["run_id"]].append(e["payload"])
            elif e["type"] == "model.request_started":
                roles[e["run_id"], e["payload"]["request_id"]] = e["payload"]["actor_role"]
        for row in source["cohort"]:
            task, run = row["task_id"], row["run_id"]
            start, _, scored = _closed_run(scan, run)
            reported = reports[run]
            if (start["condition_id"] != "no_memory" or start["payload"]["bank_id"] is not None
                    or reported["task_id"] != task or blobs.get(start["payload"]["task_ref"])["task_id"] != task
                    or reported["official_success"] is not grades[task] or scored["payload"]["official_success"] is not grades[task]
                    or reported["report_ref"] != scored["payload"]["official_artifact_ref"]):
                raise ContractError("First-pass effort does not belong to the paired baseline task and grade")
            submitted = [p for p in by_run[run] if roles[run, p["request_id"]] in {"worker", "user_simulator"}
                         and p["delivery_status"] != "not_submitted"]
            measure = _public_effort(blobs.get(reported["report_ref"])["messages"])
            tokens = [(p.get("usage") or {}).get("total_tokens") for p in submitted]
            costs = [p["cost_usd"] if p["cost_status"] == "observed" else None for p in submitted]
            worker = [p for p in submitted if roles[run, p["request_id"]] == "worker"]
            reads = None
            if all(p["status"] == "response" for p in worker):
                replies = [blobs.get(p["response_ref"])["choices"][0]["message"] for p in worker]
                reads = sum(c["function"]["name"] in {"memory_list", "memory_get"} for m in replies for c in m.get("tool_calls") or [])
            measure.update(http_calls=len(submitted), tokens=sum(tokens) if all(type(t) is int and t >= 0 for t in tokens) else None,
                           usd=str(sum(Decimal(str(c)) for c in costs)) if None not in costs else None, memory_read_calls=reads)
            archive["by_task"][task] = dict(run_id=run, metrics=measure)
    archive["status"] = "observed"
    return archive


def paired_effort(pairs, metrics=METRICS, *, include_transitions=False, summarize_differences=False):
    """Each pair contains (candidate success, reference success, candidate metrics, reference metrics)."""
    pairs = list(pairs)
    outcomes = paired_counts((a, b) for a, b, _, _ in pairs)
    groups = dict(jointly_graded=[p for p in pairs if None not in p[:2]],
                  both_success=[p for p in pairs if p[0] is True and p[1] is True])
    if include_transitions:
        groups["all_assigned"] = pairs
        for name, candidate, control in (("gain", True, False), ("loss", False, True), ("both_failure", False, False)):
            groups[name] = [p for p in pairs if p[0] is candidate and p[1] is control]
    result = dict(outcomes=outcomes)
    for group, rows in groups.items():
        measures = {}
        for metric in metrics:
            observed = [(a[metric], b[metric]) for _, _, a, b in rows
                        if a.get(metric) is not None and b.get(metric) is not None]
            values = [(Decimal(str(a)), Decimal(str(b))) for a, b in observed]
            candidate = sum((a for a, _ in values), Decimal(0))
            reference = sum((b for _, b in values), Decimal(0))
            measures[metric] = dict(observed=len(values), missing=len(rows) - len(values),
                                    candidate_total=str(candidate) if values else None,
                                    reference_total=str(reference) if values else None,
                                    difference=str(candidate - reference) if values else None,
                                    relative_difference_pct=(round(float(100 * (candidate - reference) / reference), 3)
                                                             if values and reference else None),
                                    lower=sum(a < b for a, b in values), equal=sum(a == b for a, b in values),
                                    higher=sum(a > b for a, b in values))
            if summarize_differences:
                differences = [a - b for a, b in values]
                measures[metric].update(
                    mean_paired_difference=str((candidate - reference) / len(values)) if values else None,
                    median_paired_difference=str(median(differences)) if values else None)
        result[group] = dict(pairs=len(rows), metrics=measures)
    return result
