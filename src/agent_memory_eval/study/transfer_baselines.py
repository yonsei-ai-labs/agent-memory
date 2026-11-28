"""Compare a transfer reader with independent no-memory sessions on its own target."""

from collections import Counter

from agent_memory_eval.analysis.claim_evidence import paired_counts
from agent_memory_eval.analysis.efficiency import paired_effort
from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.study.cohort import is_cohort

EFFORT_METRICS = ("requests", "tokens", "usd", "business_calls", "customer_turns", "question_turns", "repeated_question_turns")
TOKEN_COMPONENTS = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")
COHORT_EFFORT_METRICS = (*EFFORT_METRICS, "assistant_turns", "memory_list", "memory_get",
                         *(f"{role}_{key}" for role in ("worker", "user_simulator") for key in TOKEN_COMPONENTS))


def baseline_slots(plan, observed):
    """Index 1 is the target first pass. Old τ² plans collected it, old BFCL plans did not."""
    default = 0 if plan["benchmark"]["adapter"] == "bfcl-relevance" and not is_cohort(plan) else 1
    count = plan["transfer"].get("target_no_memory_repeats", default)
    registered = {}
    for task in {pair["target"] for pair in plan["transfer"]["pairs"]}:
        for draw in range(1, plan["first_pass"]["draws"] + 1):
            records = {}
            runs = set()
            for item in observed.get((task, draw), []):
                index = item["index"]
                if type(index) is not int or not 1 <= index <= count or index in records:
                    raise ContractError("Duplicate or unregistered target baseline")
                status, success = item["status"], item["success"]
                if not ((status == "graded" and type(success) is bool) or (status == "missing" and success is None)):
                    raise ContractError("Invalid target baseline outcome")
                run = item.get("run")
                if (item["status"] == "graded" and not run) or (run and run in runs):
                    raise ContractError("Independent target baselines require distinct runs")
                if run:
                    runs.add(run)
                records[index] = item
            registered[task, draw] = [
                records.get(index, dict(index=index, run=None, status="missing", success=None,
                                        missing_reason="not_started", opening_sha256=None, first_worker_sha256=None,
                                        decision=None, effort_from_first_worker=None, token_usage_by_role=None))
                for index in range(1, count + 1)
            ]
    if observed.keys() - registered.keys():
        raise ContractError("Baseline supplied for an unregistered target or draw")
    return registered


def outcome(row, metric, rule=None):
    if metric == "official_success":
        return row.get("success")
    decision = row.get("decision") or {}
    if metric == "registered_decision":
        return decision.get("correct")
    if metric not in {"registered_call_executed", "reference_call_executed"}:
        raise ValueError(f"Unknown transfer metric {metric}")
    rule = rule or row.get("decision_rule") or {}
    if rule.get("kind") != "first_matching_tool" or rule.get("expected") is None:
        return None
    state = row.get("state_diagnostics") or {}
    if metric == "reference_call_executed" and not state.get("reference_actions"):
        return None
    if decision.get("correct") is None:
        return None
    if metric == "registered_call_executed" and not decision["correct"]:
        return False
    call = state.get("first_registered_call") or {}
    if not call and state.get("registered_calls_status") == "no_call":
        return False
    receipt = call.get("receipt") or {}
    if receipt.get("status") in {"reported_error", "rejected_before_execution"}:
        return False
    if receipt.get("status") != "returned":
        return None
    if receipt.get("returned_requested_entity") is False:
        return False
    if metric == "reference_call_executed":
        return call.get("matches_any_reference")
    return True


def metric_applies(row, metric):
    rule = row.get("decision_rule") or {}
    if metric == "official_success" or not rule:
        return True
    if metric == "registered_decision":
        return rule.get("kind") != "official_only"
    return rule.get("kind") == "first_matching_tool" and rule.get("expected") is not None


def _same_opening(a, b):
    return bool(a.get("opening_sha256")) and a["opening_sha256"] == b.get("opening_sha256")


def _same_no_memory_input(a, b):
    return (_same_opening(a, b) and bool(a.get("first_worker_sha256"))
            and a["first_worker_sha256"] == b.get("first_worker_sha256") and a["run"] != b["run"])


def observed_effort(item):
    effort = {**(item.get("effort_from_first_worker") or {}), **(item.get("dialogue") or {}),
              "business_calls": item.get("business_calls")}
    for role, values in (effort.get("by_role") or {}).items():
        effort.update({f"{role}_{key}": values.get(key) for key in TOKEN_COMPONENTS})
    evidence = item.get("evidence") or item.get("exposure") or {}
    if (evidence.get("answered_worker") and evidence["answered_worker"] == evidence.get("dispatched_worker")
            and not evidence.get("unresolved_worker")):
        reads = item.get("reads") or evidence.get("reads") or {}
        effort.update({tool: reads.get(tool, 0) for tool in ("memory_list", "memory_get")})
    return effort


def target_no_memory_summary(slots):
    """Count baseline sessions once even when several donor memories use the same target."""
    blocks = {}
    for row in slots:
        key = row["origin"], row["task"], row.get("draw", 1)
        block = row.get("target_baselines", [])
        if blocks.setdefault(key, block) != block:
            raise ContractError("Readers of one target opening disagree about its baseline sessions")
    baselines = [b for block in blocks.values() for b in block]
    known = [b["run"] for b in baselines if b.get("run")]
    if len(known) != len(set(known)):
        raise ContractError("The same baseline run was assigned to different target draws")
    first = [b[0] for b in blocks.values() if b]
    repeat_pairs = [(block[0], repeat) for block in blocks.values() for repeat in block[1:]]
    pure = {}
    for metric in ("official_success", "registered_decision"):
        pure[metric] = paired_counts(
            (outcome(b, metric) if _same_no_memory_input(a, b) else None, outcome(a, metric))
            for a, b in repeat_pairs
        )
    return dict(targets=len({task for _, task, _ in blocks}), target_draws=len(blocks),
                draws_without_registered_baseline=sum(not b for b in blocks.values()),
                assigned=len(baselines), recorded=len(known), graded=sum(b["status"] == "graded" for b in baselines),
                first_pass=dict(assigned=len(first), graded=sum(b["status"] == "graded" for b in first),
                                successes=sum(b["success"] is True for b in first)),
                first_pass_vs_no_memory_repeats=pure)


def target_transitions(rows, metric, *, include_effort=True):
    """Expected cells use independent, same-input target repeats conditional on the observed first pass."""
    registered = len(rows)
    rows = [row for row in rows if metric_applies(row, metric)]
    pairs, calibrated_pairs = [], []
    expected = dict.fromkeys(("gain", "loss", "both_success", "both_failure"), 0.0)
    distinct_first_passes = set()
    calibration_runs = set()
    missing = Counter()
    effort_pairs = []
    for row in rows:
        baselines = row.get("target_baselines", [])
        anchor = baselines[0] if baselines else {}
        rule = row.get("decision_rule")
        a, b = outcome(anchor, metric, rule), outcome(row, metric, rule)
        matched = _same_opening(anchor, row)
        if not matched:
            a = None
        pairs.append((b, a))
        if metric == "official_success" and include_effort:
            effort_pairs.append((b, a, observed_effort(row), observed_effort(anchor) if matched else {}))
        if a is None or b is None:
            if not anchor:
                reason = "baseline_not_registered"
            elif outcome(anchor, metric, rule) is None:
                reason = "baseline_unmeasured"
            elif not _same_opening(anchor, row):
                reason = "matched_opening_unavailable"
            else:
                reason = "reader_unmeasured"
            missing[reason] += 1
            continue
        distinct_first_passes.add(anchor["run"])
        repeats = [control for control in baselines[1:]
                   if _same_no_memory_input(anchor, control) and outcome(control, metric, rule) is not None]
        if not repeats:
            continue
        p = sum(outcome(control, metric, rule) for control in repeats) / len(repeats)
        expected["both_success" if a else "gain"] += p
        expected["loss" if a else "both_failure"] += 1 - p
        calibration_runs.update(control["run"] for control in repeats)
        calibrated_pairs.append((b, a))
    result = dict(observed=paired_counts(pairs), missing_reasons=dict(missing), matched_first_passes=len(distinct_first_passes),
                  targets=len({r["task"] for r in rows}),
                  not_applicable=registered - len(rows),
                  no_memory_expectation=dict(covered_pairs=len(calibrated_pairs), independent_repeat_runs=len(calibration_runs),
                                             observed_on_covered_pairs=paired_counts(calibrated_pairs),
                                             expected_counts=expected if calibrated_pairs else None))
    if metric == "official_success" and include_effort:
        cohort = any("bank_source_tasks" in r for r in rows)
        result["effort_from_first_worker"] = paired_effort(
            effort_pairs, COHORT_EFFORT_METRICS if cohort else EFFORT_METRICS,
            include_transitions=True, summarize_differences=cohort)
    return result
