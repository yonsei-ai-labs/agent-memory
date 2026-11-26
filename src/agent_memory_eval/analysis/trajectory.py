"""Report source evidence, the first public decision and progress at the recorded stopping point."""

from collections import Counter, defaultdict
from decimal import Decimal
from itertools import groupby

from agent_memory_eval.analysis.decision_attribution import Action
from agent_memory_eval.analysis.recorded_decisions import READS, require
from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.recording.batch import usage_from_events


FORMAT = "agent-memory-trajectory-outcomes/v1"


def allocation(task, cluster, phase, condition, *, form=None, delivery=None, draw=None, repeat=None):
    return dict(task=task, cluster=cluster, phase=phase, condition=condition, form=form, delivery=delivery,
                writer_draw=draw, reader_repeat=repeat, run=None, source_run=None, writer_run=None,
                snapshot=None, status="not_started", reason=None, official_success=None, first_input_sha256=None,
                decision=None, reads_before_decision=None, read_returns=None, effort=None,
                progress=None, source_cues=None, evidence={})


def decision_path(log):
    """Count issued private calls separately from verified fields in the business decision's input."""
    requests = [r for r in log.requests if r["actor_role"] == "worker"]
    reads = Counter()
    returns = Counter()
    decision = None
    first_input = None
    for req in requests:
        finish = log.finishes.get(req["request_id"])
        if not finish or finish["delivery_status"] == "not_submitted":
            break
        first_input = first_input or req["request_ref"]["sha256"]
        if finish["status"] != "response":
            break
        raw = log.blobs.get(finish["response_ref"])
        choice = raw["choices"][0]
        if choice["finish_reason"] == "length" or (raw.get("local_generation") or {}).get("decode_error"):
            break
        calls = choice["message"].get("tool_calls") or []
        private = [call for call in calls if call["function"]["name"] in READS]
        reads.update(c["function"]["name"] for c in private)
        for call in private:
            name = call["function"]["name"]
            receipt = log.returns.get(call["id"])
            if receipt:
                require(receipt[0] == name.removeprefix("memory_"), "Memory call and returned operation differ")
                returns[name] += 1
        if private:
            if len(private) != len(calls):
                break
            continue
        spans = log.visible(req, log.blobs.get(req["request_ref"]))
        fields = sorted({(s["item_id"], s["version_id"], s["field"]) for s in spans})
        decision = dict(request_id=req["request_id"], request_sha256=req["request_ref"]["sha256"],
                        manifest_sha256=req["input_manifest_ref"]["sha256"], response_sha256=finish["response_ref"]["sha256"],
                        memory=[dict(item=i, version=v, field=f) for i, v, f in fields],
                        calls=[dict(id=c["id"], **Action.from_function(c["function"]).record()) for c in calls])
        break
    return dict(first_input_sha256=first_input, reads_before_decision=dict(reads), read_returns=dict(returns),
                decision=decision)


def recorded_effort(log, *, reused_local_generations=0):
    bills = usage_from_events(log.events.values())
    dispatched = [f for f in log.finishes.values() if f["delivery_status"] != "not_submitted"]
    counted = sum((f.get("usage") or {}).get("total_tokens") is not None for f in dispatched)
    return dict(requests=len(log.requests), dispatched=len(dispatched), reused_local_generations=reused_local_generations,
                tokens_reported_on=counted,
                reported_tokens=sum(b["total_tokens"] for b in bills.values()) if counted else None,
                reported_usd=str(sum((Decimal(b["reported_usd"]) for b in bills.values()), Decimal(0))),
                unknown_cost_requests=sum(b["unknown_count"] for b in bills.values()))


def cue(source, current):
    """Keys identify the same observable clue on either side. None denotes unavailable evidence."""
    return dict(source=None if source is None else sorted(set(source)),
                current=None if current is None else sorted(set(current)))


def source_opportunities(rows, definitions):
    sources = [r for r in rows if r["phase"] == "source"]

    def counts_for(name, selected):
        counts = Counter()
        clusters = defaultdict(set)
        for row in selected:
            value = (row["source_cues"] or {}).get(name) or {}
            previous, current = value.get("source"), value.get("current")
            counts["source_observed"] += previous is not None
            counts["current_observed"] += current is not None
            comparable = previous is not None and current is not None
            counts["observed"] += comparable
            counts["unknown"] += not comparable
            for key, evidence in (("with_source_evidence", previous), ("with_current_evidence", current),
                                  ("with_source_only_evidence", set(previous) - set(current) if comparable else None)):
                if evidence:
                    counts[key] += 1
                    clusters[key].add(row["cluster"])
        return dict(assigned=len(selected), **{key: counts[key] for key in
            ("observed", "unknown", "source_observed", "current_observed", "with_source_evidence",
             "with_current_evidence", "with_source_only_evidence")},
            distinct_clusters=len({r["cluster"] for r in selected}),
            clusters_with_source_evidence=len(clusters["with_source_evidence"]),
            clusters_with_source_only_evidence=len(clusters["with_source_only_evidence"]))

    graded = [r for r in sources if r["status"] == "graded"]
    return {name: dict(**counts_for(name, sources), graded_sources=counts_for(name, graded)) for name in definitions}


def validate_opportunity_requirement(rule, cues):
    require(isinstance(rule, dict) and set(rule) == {"cue", "evidence", "minimum_clusters"}
            and isinstance(rule["cue"], str) and rule["cue"] in cues and rule["evidence"] in ("source", "source_only")
            and type(rule["minimum_clusters"]) is int and rule["minimum_clusters"] >= 1,
            "Invalid registered source opportunity requirement")


def assess_opportunity(rule, opportunities):
    if rule is None:
        return None
    validate_opportunity_requirement(rule, opportunities)
    counts = opportunities[rule["cue"]]["graded_sources"]
    observed = counts["clusters_with_" + rule["evidence"] + "_evidence"]
    covered = counts["observed"] if rule["evidence"] == "source_only" else counts["source_observed"]
    return dict(**rule, eligible_graded_sources=counts["assigned"], unknown_graded_sources=counts["assigned"] - covered,
                observed_clusters=observed, met=observed >= rule["minimum_clusters"])


def source_admission(report):
    assessment = report["source_requirement"]
    require(report["scope"] == "sources" and assessment is not None, "Expected a preregistered source-only assessment")
    reason = None
    if not assessment["met"]:
        reason = "source_evidence_unconfirmed" if assessment["unknown_graded_sources"] else "source_evidence_below_minimum"
    return dict(format="agent-memory-source-admission/v1", scope="whole_study", decision="continue" if assessment["met"] else "stop",
                reason=reason, protocol_sha256=report["bindings"]["protocol_sha256"], source=report["bindings"]["source"],
                observations_sha256=report["observations_sha256"], assessment=assessment)


def total_effort(rows):
    values = [r["effort"] for r in rows if r["effort"] is not None]
    if not values:
        return None
    counts = {key: sum(v[key] for v in values) for key in
              ("requests", "dispatched", "reused_local_generations", "tokens_reported_on", "unknown_cost_requests")}
    return dict(observed_runs=len(values), **counts, reported_tokens=sum(v["reported_tokens"] or 0 for v in values)
                if counts["tokens_reported_on"] else None, reported_usd=str(sum((Decimal(v["reported_usd"]) for v in values), Decimal(0))))


def _conditions(rows, metrics):
    result = []
    key = lambda r: (r["phase"], r["condition"])
    for (phase, condition), items in groupby(sorted(rows, key=key), key):
        items = list(items)
        decisions = [r["decision"] for r in items if r["decision"] is not None]
        progress = {}
        for status in ("graded", "ungraded", "not_started"):
            eligible = [r for r in items if r["status"] == status]
            cells = {}
            for metric in metrics:
                values = [r["progress"][metric] for r in eligible if (r["progress"] or {}).get(metric) is not None]
                cells[metric] = dict(observed=len(values), total=sum(values) if values else None,
                                     nonzero=sum(bool(v) for v in values) if values else None)
            progress[status] = dict(assigned=len(eligible), metrics=cells, effort=total_effort(eligible))
        result.append(dict(phase=phase, condition=condition, assigned=len(items),
            started=sum(r["status"] != "not_started" for r in items),
            graded=sum(r["status"] == "graded" for r in items),
            official_successes=sum(r["official_success"] is True for r in items) if any(r["status"] == "graded" for r in items) else None,
            decisions=len(decisions), decisions_with_body=sum(any(s["field"] == "body" for s in d["memory"]) for d in decisions),
            decisions_with_title=sum(any(s["field"] == "title" for s in d["memory"]) for d in decisions),
            decisions_with_function=sum(bool(d["calls"]) for d in decisions),
            read_calls=dict(sum((Counter(r["reads_before_decision"] or {}) for r in items), Counter())),
            read_returns=dict(sum((Counter(r["read_returns"] or {}) for r in items), Counter())),
            stops=dict(Counter(r["reason"] for r in items if r["status"] != "graded")), progress=progress))
    return result


def repetition_variation(rows, metrics):
    groups = defaultdict(list)
    for row in rows:
        if row["phase"] == "solve":
            groups[row["task"], row["condition"], row["delivery"], row["form"], row["writer_draw"]].append(row)
    repeated = [g for g in groups.values() if len(g) > 1]
    varied = dict(registered=len(repeated), missing_decisions=0, different_inputs_or_banks=0,
                  comparable=0, changed_function_sequence=0, changed_functions_or_arguments=0)
    measures = {key: dict(observed_groups=0, changed_groups=0) for key in metrics}
    for group in repeated:
        if any(r["decision"] is None or not r["first_input_sha256"] for r in group):
            varied["missing_decisions"] += 1
            continue
        if len({(r["writer_run"], r["snapshot"], r["first_input_sha256"]) for r in group}) != 1:
            varied["different_inputs_or_banks"] += 1
            continue
        varied["comparable"] += 1
        calls = [r["decision"]["calls"] for r in group]
        varied["changed_function_sequence"] += len({tuple(c["name"] for c in cs) for cs in calls}) > 1
        choices = [canonical([{k: v for k, v in c.items() if k != "id"} for c in cs]) for cs in calls]
        varied["changed_functions_or_arguments"] += len(set(choices)) > 1
        for key, cell in measures.items():
            values = [(r["progress"] or {}).get(key) for r in group]
            if None not in values:
                cell["observed_groups"] += 1
                cell["changed_groups"] += len(set(values)) > 1
    return dict(groups=varied, metrics=measures)


def fault_summary(errors):
    summary = []
    key = lambda r: (r["phase"], r["reader_repeat"] or 0, r["actor"], r["code"], r["delivery"])
    for (phase, rep, actor, code, delivery), group in groupby(sorted(errors, key=key), key):
        group = list(group)
        sizes = [r["wire_bytes"] for r in group]
        summary.append(dict(phase=phase, reader_repeat=rep or None, actor=actor, code=code, delivery=delivery,
                            requests=len(group), min_wire_bytes=min(sizes), max_wire_bytes=max(sizes),
                            first_role_requests=sum(r["role_request_number"] == 1 for r in group),
                            later_role_requests=sum(r["role_request_number"] > 1 for r in group),
                            min_first_role_wire_bytes=min(r["first_role_wire_bytes"] for r in group),
                            max_first_role_wire_bytes=max(r["first_role_wire_bytes"] for r in group),
                            with_retry_after=sum(r["retry_after_seconds"] is not None or r["retry_after_utc"] is not None
                                                 for r in group)))
    return summary


def trajectory_report(benchmark, bindings, rows, *, metrics, cues, sources_only, request_errors=(), requirement=None):
    rows = list(rows)
    request_errors = list(request_errors)
    keys = set()
    runs = set()
    sources = {r["task"]: r for r in rows if r["phase"] == "source"}
    require(len(sources) == sum(r["phase"] == "source" for r in rows), "Ambiguous source task")
    for row in rows:
        require(row["phase"] in ("source", "solve") and (not sources_only or row["phase"] == "source")
                and row["task"] and row["cluster"], "Invalid phase or missing sampling unit")
        key = tuple(row[k] for k in ("task", "phase", "condition", "form", "delivery", "writer_draw", "reader_repeat"))
        require(key not in keys, "Duplicate registered trajectory")
        keys.add(key)
        if row["phase"] == "solve":
            origin = sources.get(row["task"])
            require(origin is not None and row["source_run"] == origin["run"] and row["cluster"] == origin["cluster"],
                    "Reader does not belong to its source")
        require(row["status"] in {"graded", "ungraded", "not_started"} and
                (type(row["official_success"]) is bool if row["status"] == "graded" else row["official_success"] is None),
                "Official grade and coverage disagree")
        require(row["status"] == "graded" or bool(row["reason"]), "Missing reason for unavailable official grade")
        if row["status"] != "not_started":
            require(row["run"] and row["run"] not in runs, "Missing or reused trajectory run")
            runs.add(row["run"])
        else:
            require(all(row[k] is None for k in ("decision", "first_input_sha256", "progress", "source_cues",
                                                 "reads_before_decision", "read_returns", "effort")),
                    "Unstarted trajectory has observations")
        require(set(row["progress"] or {}) <= metrics.keys() and set(row["source_cues"] or {}) <= cues.keys(),
                "Undeclared trajectory metric")
        require(row["phase"] == "source" or row["source_cues"] is None, "Reader observations cannot establish source opportunity")
        require(all(v is None or isinstance(v, (int, bool)) and v >= 0 for v in (row["progress"] or {}).values()),
                "Progress must be an observed count or unavailable")
    opportunities = source_opportunities(rows, cues)
    return dict(format=FORMAT, benchmark=benchmark, scope="sources" if sources_only else "sources_and_readers",
                bindings=bindings, metric_definitions=metrics, cue_definitions=cues,
                source_opportunities=opportunities, source_requirement=assess_opportunity(requirement, opportunities),
                by_condition=_conditions(rows, metrics), same_input_repetitions=repetition_variation(rows, metrics),
                observation_count=len(rows), observations_sha256=digest(canonical(rows)), observations=rows,
                request_error_summary=fault_summary(request_errors), request_errors_sha256=digest(canonical(request_errors)),
                request_errors=request_errors)


def compact_report(report):
    return {k: v for k, v in report.items() if k not in ("observations", "request_errors")}
