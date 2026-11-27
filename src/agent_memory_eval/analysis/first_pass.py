"""Compare registered memory policies against the no-memory run that supplied their experience."""

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from agent_memory_eval.analysis.claim_evidence import paired_counts
from agent_memory_eval.analysis.clustered_outcomes import clustered_contrast
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest


AXES = ("condition", "delivery", "form")
DEFAULT_AXES = ("condition", "delivery")
FORMAT = "agent-memory-first-pass-comparison/v1"
EFFORT = ("tokens", "requests", "usd")


def _grades(rows):
    grades = [r["official_success"] for r in rows]
    return dict(assigned=len(grades), graded=sum(v is not None for v in grades),
                successes=grades.count(True), missing=grades.count(None))


def _paired(pairs):
    counts = paired_counts((r["official_success"], s["official_success"]) for s, r in pairs)
    observed = counts["assigned"] - counts["missing"]
    failed = counts["gain"] + counts["both_failure"]
    succeeded = counts["loss"] + counts["both_success"]
    return dict(**counts, graded=observed, difference=(counts["gain"] - counts["loss"]) / observed if observed else None,
                recovery_rate=counts["gain"] / failed if failed else None,
                regression_rate=counts["loss"] / succeeded if succeeded else None)


def _effort(groups, width):
    """Each metric uses only slots with measurements for every member of the comparison."""
    totals = {}
    for metric in EFFORT:
        values = [[(r.get("decision_effort") or {}).get(metric) for r in group] for group in groups]
        covered = [row for row in values if None not in row]
        for row in covered:
            for value in row:
                if metric == "usd":
                    try:
                        amount = Decimal(str(value))
                    except InvalidOperation:
                        amount = Decimal("NaN")
                    require(amount.is_finite() and amount >= 0, "Invalid solve cost")
                else:
                    require(type(value) is int and value >= 0, "Invalid solve token or request count")
        sums = [sum((Decimal(str(row[i])) for row in covered), Decimal(0)) for i in range(width)]
        totals[metric] = dict(covered_slots=len(covered),
                              totals=[(str(v) if metric == "usd" else int(v)) for v in sums] if covered else [None] * width)
    return totals


def _receipt(row):
    if row is None:
        return None
    decision = row.get("decision")
    return dict(run=row["run"], status=row["status"], success=row["official_success"], reason=row.get("reason"),
                writer_run=row.get("writer_run"), snapshot=row.get("snapshot"), evidence=row.get("evidence", {}),
                decision_sha256=decision.get("request_sha256") if decision is not None else None,
                decision_memory_fields=sorted({v["field"] for v in decision["memory"]}) if decision is not None else None)


def compare_first_pass(rows, *, policy_axes=DEFAULT_AXES, cluster_unit, details=True):
    """Use registered allocations, including failed or unstarted ones. Axes outside the policy are held fixed."""
    axes = tuple(policy_axes)
    require(axes and len(set(axes)) == len(axes) and set(axes) <= set(AXES), "Invalid policy axes")
    require(isinstance(cluster_unit, str) and cluster_unit.strip(), "Missing sampling unit")
    rows = list(rows)
    sources, seen_runs, task_clusters = {}, set(), {}
    for row in rows:
        task, cluster, run = row["task"], row["cluster"], row["run"]
        require(task and cluster and task_clusters.setdefault(task, cluster) == cluster, "Inconsistent sampling unit")
        require(row["phase"] in {"source", "solve"} and row["status"] in {"graded", "ungraded", "not_started"},
                "Unknown observation phase or status")
        grade = row["official_success"]
        require((type(grade) is bool if row["status"] == "graded" else grade is None), "Grade and coverage disagree")
        require(run or row["status"] == "not_started", "Missing recorded run")
        if run is not None:
            require(run not in seen_runs, "Duplicate run identifier")
            seen_runs.add(run)
        if row["phase"] == "source":
            key = task, run
            require(key not in sources and row["condition"] == "no_memory" and not row.get("snapshot")
                    and not row.get("source_run") and not row.get("writer_run"), "Invalid no-memory source")
            sources[key] = row
    fixed = tuple(k for k in AXES if k not in axes) + ("writer_draw", "reader_repeat")
    slots = defaultdict(dict)
    by_policy = defaultdict(list)
    memories = {}
    for row in rows:
        if row["phase"] != "solve":
            continue
        source_key = row["task"], row["source_run"]
        require(source_key in sources and (row["status"] == "not_started" or row["source_run"]),
                "Reader has no matching first pass")
        if row.get("writer_run") and row.get("snapshot"):
            draw = (*source_key, row["condition"], row["form"], row["writer_draw"])
            memory = row["writer_run"], row["snapshot"]
            require(memories.setdefault(draw, memory) == memory, "Same writer draw resolved to different memories")
        policy = tuple(row[k] for k in axes)
        slot = (*source_key, *(row[k] for k in fixed))
        require(policy not in slots[slot], "Duplicate policy allocation for the same first-pass slot")
        slots[slot][policy] = row
        by_policy[policy].append((sources[source_key], row))

    policies = sorted(by_policy, key=canonical)
    labels = [dict(zip(axes, p, strict=True)) for p in policies]
    common, receipts = [], []
    for slot, cells in sorted(slots.items(), key=lambda item: canonical(item[0])):
        source = sources[slot[:2]]
        readers = [cells.get(p) for p in policies]
        complete = source["official_success"] is not None and all(r and r["official_success"] is not None for r in readers)
        if complete:
            common.append((source, *readers))
        if details:
            receipts.append(dict(task=slot[0], cluster=source["cluster"], **dict(zip(fixed, slot[2:], strict=True)),
                                 source=_receipt(source), readers=[_receipt(r) for r in readers]))

    summaries = []
    for index, policy in enumerate(policies, 1):
        pairs = by_policy[policy]
        readers = [r for _, r in pairs]
        form_pairs = defaultdict(list)
        for s, r in pairs:
            form_pairs[r["form"]].append((s, r))
        contrasts = [(s["cluster"], s["task"], tuple(r[k] for k in AXES if k not in axes),
                      (s["run"], r["writer_draw"], r["reader_repeat"]), s["official_success"], r["official_success"])
                     for s, r in pairs]
        decisions = [r["decision"] for r in readers if r.get("decision") is not None]
        summaries.append(dict(policy=labels[index - 1], outcomes=_grades(readers), versus_first_pass=_paired(pairs),
                              by_form=[dict(form=f, versus_first_pass=_paired(ps))
                                       for f, ps in sorted(form_pairs.items(), key=lambda item: canonical(item[0]))],
                              clustered=clustered_contrast(contrasts, cluster_unit=cluster_unit),
                              common_versus_first_pass=_paired([(g[0], g[index]) for g in common]),
                              first_decisions_observed=len(decisions),
                              first_decisions_with_body=sum(any(v["field"] == "body" for v in d["memory"])
                                                            for d in decisions)))
    wins = [g for g in common if all(r["official_success"] for r in g)]
    result = dict(format=FORMAT, policy_axes=list(axes), matched_on=["task", "source_run", *fixed],
                  no_memory_first_pass=_grades(sources.values()), policies=summaries,
                  common=dict(assigned_slots=len(slots), coassigned_slots=sum(len(c) == len(policies) for c in slots.values()),
                              graded_slots=len(common),
                              distinct_sources=len({g[0]["run"] for g in common}),
                              distinct_clusters=len({g[0]["cluster"] for g in common}),
                              policy_order=labels,
                              first_pass_successes=sum(g[0]["official_success"] for g in common),
                              policy_successes=[sum(g[i]["official_success"] for g in common)
                                                for i in range(1, len(policies) + 1)]),
                  all_success_effort=dict(scope="from_first_worker_request_through_end_of_solve",
                                         slots=len(wins), order=["no_memory_first_pass", *labels],
                                         distinct_sources=len({g[0]["run"] for g in wins}),
                                         distinct_clusters=len({g[0]["cluster"] for g in wins}),
                                         metrics=_effort(wins, len(policies) + 1)),
                  observations_sha256=digest(canonical(rows)))
    if details:
        result["slots"] = receipts
    return result
