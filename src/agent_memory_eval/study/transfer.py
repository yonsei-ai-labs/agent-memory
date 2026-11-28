"""Pair memory conditions within each target and trace their notes, decisions and outcomes."""

from collections import Counter, defaultdict
from decimal import Decimal
from itertools import combinations
import re

from agent_memory_eval.analysis.efficiency import combined_token_usage, paired_effort
from agent_memory_eval.analysis.policy_evidence import stored_outcome_check
from agent_memory_eval.analysis.tau_transfer_state import registered_arguments_match
from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.study.cohort import condition_banks, is_cohort, pool_sources, source_components
from agent_memory_eval.study.transfer_baselines import (
    COHORT_EFFORT_METRICS, baseline_slots, metric_applies, observed_effort, outcome, target_no_memory_summary,
    target_transitions,
)


OUTCOME_LINES = {"success": "Prior task outcome: success.", "failure": "Prior task outcome: failure."}
TITLE_OUTCOME = re.compile(r"\b(?:prior\s+)?task\s+outcome\s*[:=]?\s*(success|failure)\b", re.I)


def reader_arms(condition):
    return condition, condition + "__empty"


def decision_evidence(rule, steps, *, completed):
    """Use only answered worker requests. An absent action is conclusive only after normal termination."""
    official_only = rule.get("kind") == "official_only"
    title = body = False
    for step in steps:
        title |= step.get("title", False)
        body |= step["body"]
        if official_only:
            if step.get("public_response"):
                return dict(correct=None, call=None, request=step["request"], title_seen=title, body_seen=body,
                            measurement="first_public_worker_response")
            continue
        for call in step["calls"]:
            if call["name"] not in rule["tools"]:
                continue
            correct = call.get("arguments_valid", True) and call["name"] == rule["expected"] and registered_arguments_match(
                call["name"], call["arguments"], rule.get("arguments", {}))
            return dict(correct=correct, call=call, request=step["request"], title_seen=title, body_seen=body)
    if official_only:
        return dict(correct=None, call=None, request=None, title_seen=None, body_seen=None,
                    measurement="first_public_worker_response")
    return dict(correct=rule["expected"] is None if completed else None, call=None, request=None,
                title_seen=title, body_seen=body)


def allocation_rows(plan, attempts, *, written_memories=None, target_baselines=None):
    """Materialize every registered target slot, including blocked, crashed and never-started sessions."""
    from agent_memory_eval.study.plan import arm_name, conditions

    expected = {}
    controls = baseline_slots(plan, target_baselines or {})
    cohort = is_cohort(plan)
    groups = {}
    if cohort:
        pools = ((pair["target"], pool_sources(plan, pair["target"])) for pair in plan["transfer"]["pairs"])
        groups = {task: group[0] for group in source_components(pools) for task in group}
    for pair in plan["transfer"]["pairs"]:
        for draw in range(1, plan["first_pass"]["draws"] + 1):
            scopes = {"own": [pair["target"]], "mixed": pool_sources(plan, pair["target"], draw)} if cohort else {}
            for repeat in range(1, plan["repeats"] + 1):
                for rep, reader in conditions(plan):
                    name = arm_name(rep, reader)
                    for bank, arm in condition_banks(plan, name):
                        key = pair["source"], pair["target"], draw, repeat, arm
                        expected[key] = dict(
                            source=pair["source"],
                            task=pair["target"],
                            draw=draw,
                            repeat=repeat,
                            condition=arm,
                            comparison=name,
                            bank=bank,
                            writing_form=rep,
                            reading_policy=reader,
                            read_mode=plan["reading"][reader]["read_mode"],
                            target_baselines=controls[pair["target"], draw],
                            source_cue=pair["source_cue"],
                            decision_rule=pair["decision"],
                        )
                        if "customer_relation" in pair:
                            expected[key]["customer_relation"] = pair["customer_relation"]
                        if cohort:
                            sources = scopes[bank]
                            expected[key]["memory_group"] = groups[pair["target"]]
                            expected[key]["bank_source_tasks"] = sources
                            expected[key]["assigned_writers"] = [
                                _writer_ref(task, (written_memories or {}).get((task, draw, rep))) for task in sources
                            ]
                        memory = (written_memories or {}).get((pair["source"], draw, rep))
                        if memory is not None:
                            # This is the writer's sealed output, even for an empty-bank control.
                            # Its presence in an analysis row does not imply exposure to the reader.
                            expected[key]["written_memory"] = memory
    rows, seen = [], set()
    for row in attempts:
        key = row["source"], row["task"], row["draw"], row["repeat"], row["condition"]
        if key not in expected or (key, row["attempt"]) in seen:
            raise ContractError("Unregistered or duplicate transfer outcome")
        seen.add((key, row["attempt"]))
        joined = dict(row, **expected[key])
        if cohort and joined.get("item_exposure") is not None:
            joined["item_exposure"] = _visible_outcome_markers(joined)
        rows.append(joined)
    observed = {key for key, _ in seen}
    for key, slot in expected.items():
        if key not in observed:
            rows.append(
                dict(
                    slot,
                    origin=plan["id"],
                    attempt=0,
                    success=None,
                    status="missing",
                    missing_reason="not_started",
                    decision=None,
                )
            )
    return rows


def _writer_ref(task, note):
    if not note:
        return dict(task=task, status="unavailable")
    return dict(task=task, status="observed", source_run=note["source_run"], writer_run=note["writer_run"],
                snapshot=note["snapshot"], source_success=note.get("source_success"),
                items=[dict(id=item["id"], version=item["version"], outcome_markers=dict(
                    title=sorted({label.lower() for label in TITLE_OUTCOME.findall(item["title"])}),
                    body=[label for label, line in OUTCOME_LINES.items() if item["body"].splitlines()[:1] == [line]],
                )) for item in note["items"]])


def _visible_outcome_markers(row):
    markers = {(source["task"], item["id"], item["version"]): (source["source_success"], item["outcome_markers"])
               for source in row["assigned_writers"] for item in source.get("items", [])}
    trace = []
    for item in row["item_exposure"]:
        key = item["source_task"], item["item_id"], item["version_id"]
        if key not in markers:
            raise ContractError("Visible outcome marker has no registered writer item")
        success, stored = markers[key]
        fields = item["before_decision_fields"]
        visible = status = None
        if fields is not None:
            visible = {field: stored[field] for field in fields}
            labels = {label for values in visible.values() for label in values}
            if not labels:
                status = "no_explicit_marker"
            elif len(labels) > 1:
                status = "conflicting"
            elif success is None:
                status = "unknown_source_outcome"
            else:
                status = "matches_source" if _source_label(success) in labels else "opposes_source"
        trace.append(dict(item, visible_outcome_markers=visible, visible_outcome_status=status))
    return trace


def _writing_comparisons(rows):
    """Present sealed notes from the same public experience together, once per writer."""
    blocks = {}
    for row in rows:
        form = row.get("writing_form")
        if not form:
            continue
        key = row["origin"], row["source"], row["draw"]
        block = blocks.setdefault(key, dict(origin=key[0], source_task=key[1], draw=key[2], source_run=None,
                                            public_experience_ref=None, source_token_usage_by_role=None,
                                            target_cues={}, forms={}))
        block["target_cues"][row["task"]] = row["source_cue"]
        forms = block["forms"]
        forms.setdefault(form, dict(status="not_observed"))
        note = row.get("written_memory")
        if not note or not note.get("writer_run"):
            continue
        if forms[form]["status"] == "observed":
            if forms[form]["writer_run"] != note["writer_run"] or forms[form]["snapshot"] != note["snapshot"]:
                raise ContractError("One writing slot names different sealed outputs")
            continue
        if block["source_run"] not in (None, note["source_run"]):
            raise ContractError("A writing comparison must use the same source run")
        block["source_run"] = note["source_run"]
        if note.get("source_token_usage_by_role") is not None:
            block["source_token_usage_by_role"] = note["source_token_usage_by_role"]
        ref = note.get("public_experience_ref")
        if ref:
            previous = block["public_experience_ref"]
            if previous and previous["sha256"] != ref["sha256"]:
                raise ContractError("Writers of the same source received different public experiences")
            block["public_experience_ref"] = ref
        items = note["items"]
        title_outcomes = sorted({label.lower() for item in items for label in TITLE_OUTCOME.findall(item["title"])})
        forms[form] = dict(status="observed", writer_run=note["writer_run"], snapshot=note["snapshot"], items=items,
                           source_success=note.get("source_success", note.get("feedback_success")),
                           feedback_success=note["feedback_success"], explicit_title_outcomes=title_outcomes,
                           first_line_check=stored_outcome_check(OUTCOME_LINES, note["feedback_success"], items),
                           token_usage_by_role=note.get("token_usage_by_role"))
        if "bank_source_tasks" in row:
            forms[form]["size"] = dict(items=len(items),
                                       title_utf8_bytes=sum(len(i["title"].encode("utf-8")) for i in items),
                                       body_utf8_bytes=sum(len(i["body"].encode("utf-8")) for i in items))
    return [blocks[key] for key in sorted(blocks)]


def _outcome_counts(rows, metric):
    values = [outcome(r, metric) for r in rows if metric_applies(r, metric)]
    return dict(assigned=len(rows), applicable=len(values), not_applicable=len(rows) - len(values),
                successes=values.count(True), failures=values.count(False), missing=values.count(None))


def _reading_conditions(slots):
    groups = defaultdict(list)
    same_input = defaultdict(list)
    for row in slots:
        if not row.get("reading_policy"):
            continue
        groups[row["reading_policy"], row["read_mode"], row["bank"]].append(row)
        receipt = row.get("evidence") or {}
        answered = "bank_source_tasks" not in row or receipt.get("answered_worker") == receipt.get("dispatched_worker")
        if (answered and row.get("opening_sha256") and row.get("first_worker_sha256") and receipt.get("dispatched_worker")
                and receipt.get("unresolved_worker") == 0
                and receipt.get("title_requests") == 0 and receipt.get("body_requests") == 0
                and not any((row.get("reads") or {}).values())):
            key = row["origin"], row["task"], row["opening_sha256"], row["first_worker_sha256"]
            same_input[key].append(row)
    by_bank = [
        dict(reading_policy=reader, read_mode=mode, bank=bank, official_success=_outcome_counts(rows, "official_success"),
             registered_decision=_outcome_counts(rows, "registered_decision"))
        for (reader, mode, bank), rows in sorted(groups.items())
    ]
    matched = [
        dict(origin=origin, task=task, opening_sha256=opening, first_worker_sha256=wire,
             conditions=sorted({r["condition"] for r in rows}), official_success=_outcome_counts(rows, "official_success"),
             registered_decision=_outcome_counts(rows, "registered_decision"))
        for (origin, task, opening, wire), rows in sorted(same_input.items()) if len(rows) >= 2
    ]
    return by_bank, matched


def _contrast(pairs, metric, candidate, control):
    counts = Counter()
    per_target = defaultdict(list)
    complete = defaultdict(list)
    group_by_task = {}
    for (_, target, _, _), arms in pairs.items():
        if not metric_applies(arms[candidate], metric):
            counts["not_applicable"] += 1
            continue
        treated, base = (outcome(arms[bank], metric) for bank in (candidate, control))
        if treated is None or base is None:
            counts["missing"] += 1
        elif treated == base:
            counts["both_success" if treated else "both_failure"] += 1
        else:
            counts[(candidate if treated else control) + "_only"] += 1
        if "memory_group" in arms[candidate]:
            group_by_task[target] = arms[candidate]["memory_group"]
        if treated is not None and base is not None:
            complete[target].append(int(treated) - int(base))
        lower = int(treated or False) - int(base is not False)
        upper = int(treated is not False) - int(base or False)
        per_target[target].append((lower, upper))
    means = [[sum(v[i] for v in cells) / len(cells) for i in (0, 1)] for cells in per_target.values()]
    observed = {task: sum(diffs) / len(diffs) for task, diffs in complete.items()}
    result = dict(
        assigned=len(pairs),
        applicable=len(pairs) - counts["not_applicable"], not_applicable=counts["not_applicable"],
        targets=len(means),
        targets_with_complete_pairs=len(observed),
        complete_pair_target_weighted_difference=sum(observed.values()) / len(observed) if observed else None,
        **{k: counts[k] for k in (candidate + "_only", control + "_only", "both_success", "both_failure", "missing")},
        target_weighted_bounds=[sum(m[i] for m in means) / len(means) for i in (0, 1)] if means else None,
    )
    if metric == "official_success" and group_by_task:
        grouped = defaultdict(list)
        for task, difference in observed.items():
            grouped[group_by_task[task]].append(difference)
        total = sum(observed.values())
        result["shared_memory_groups"] = dict(
            registered=len(set(group_by_task.values())), with_complete_pairs=len(grouped),
            leave_one_group_out={
                group: (total - sum(values)) / (len(observed) - len(values))
                for group, values in sorted(grouped.items()) if len(values) < len(observed)
            },
        )
    return result


def _exposure(arm):
    decisions = [r["decision"] for r in arm if (r.get("decision") or {}).get("correct") is not None]
    cutoffs = [r["decision"] for r in arm if (r.get("decision") or {}).get("request")
               or (r.get("decision") or {}).get("correct") is not None]
    receipts = [r.get("evidence") or {} for r in arm]
    attempts = [r.get("reads") or {} for r in arm]
    return dict(
        decisions=len(decisions),
        observed_decision_cutoffs=len(cutoffs),
        title_seen_at_observed_cutoff=sum(d.get("title_seen") is True for d in cutoffs),
        body_seen_at_observed_cutoff=sum(d.get("body_seen") is True for d in cutoffs),
        body_seen=sum(d["body_seen"] for d in decisions),
        title_measured=sum(d.get("title_seen") is not None for d in decisions),
        title_seen=sum(d.get("title_seen") is True for d in decisions),
        title_only=sum(d.get("title_seen") is True and not d["body_seen"] for d in decisions),
        slots_listing=sum(bool(r.get("memory_list")) for r in attempts),
        slots_getting=sum(bool(r.get("memory_get")) for r in attempts),
        slots_with_dispatched_worker=sum(bool(r.get("dispatched_worker")) for r in receipts),
        slots_with_title_in_input=sum(bool(r.get("title_requests")) for r in receipts),
        slots_with_body_in_input=sum(bool(r.get("body_requests")) for r in receipts),
        slots_with_returned_fields_measured=sum(r.get("returned_titles") is not None for r in receipts),
        slots_with_title_returned=sum(bool(r.get("returned_titles")) for r in receipts),
        slots_with_body_returned=sum(bool(r.get("returned_bodies")) for r in receipts),
    )


def _business_state(slots):
    by_target = defaultdict(list)
    for row in slots:
        by_target[row["task"]].append(row)
    summary = {}
    for target, rows in by_target.items():
        statuses, reasons, responses, fields, failed_fields = (Counter() for _ in range(5))
        additional_fields = Counter()
        db_measured = db_mismatches = correct_decision_on_failure = 0
        for row in rows:
            state = row.get("state_diagnostics") or {}
            statuses[state.get("status", "unavailable")] += 1
            if state.get("reason"):
                reasons[state["reason"]] += 1
            call = state.get("first_registered_call")
            if call:
                responses[call["receipt"]["status"]] += 1
            if row["success"] is False and (row.get("decision") or {}).get("correct") is True:
                correct_decision_on_failure += 1
            if state.get("status") != "verified":
                continue
            match = state.get("official_db_match")
            db_measured += type(match) is bool
            db_mismatches += match is False
            changed = {f"{entity['table']}.{field}" for entity in state["target_entities"]
                       for field in entity["different_fields"]}
            fields.update(changed)
            additional_fields.update({f"{entity['table']}.{field}" for entity in state.get("additional_entities", ())
                                      for field in entity["different_fields"]})
            if row["success"] is False:
                failed_fields.update(changed)
        summary[target] = dict(assigned=len(rows), official_failures=sum(r["success"] is False for r in rows),
                               first_registered_call=_outcome_counts(rows, "registered_decision"),
                               registered_call_executed=_outcome_counts(rows, "registered_call_executed"),
                               reference_call_executed=_outcome_counts(rows, "reference_call_executed"),
                               correct_registered_decision_on_official_failure=correct_decision_on_failure,
                               state_status=dict(statuses), reasons=dict(reasons),
                               official_db_measured=db_measured, official_db_mismatches=db_mismatches if db_measured else None,
                               first_call_receipts=dict(responses), field_differences=dict(fields),
                               field_differences_on_official_failure=dict(failed_fields))
        if additional_fields:
            summary[target]["additional_entity_field_differences"] = dict(additional_fields)
    return summary


def _source_label(success):
    return "success" if success is True else "failure" if success is False else "unknown"


def _source_outcomes(rows):
    groups = defaultdict(list)
    for row in rows:
        note = row.get("written_memory") or {}
        source = note.get("source_success", note.get("feedback_success"))
        if "bank_source_tasks" in row and row.get("target_baselines"):
            first = row["target_baselines"][0]
            if note and (note.get("source_run") != first.get("run")
                         or source is not None and source is not first.get("success")):
                raise ContractError("The cohort writer and target baseline disagree on their common first pass")
            source = first.get("success")
        groups[_source_label(source)].append(row)
    return {label: target_transitions(group, "official_success") for label, group in groups.items()}


def _pool_selection(rows):
    cells = defaultdict(Counter)
    skipped = measured = read_status_measured = list_only = with_get = first_get_outside = 0
    for row in rows:
        trace = row.get("item_exposure")
        if trace is None:
            continue
        sources = {s["task"]: s for s in row["assigned_writers"]}
        expected = {(source["task"], item["id"], item["version"]) for source in sources.values()
                    for item in source.get("items", [])}
        observed = {(item["source_task"], item["item_id"], item["version_id"]) for item in trace}
        if expected != observed or len(trace) != len(observed):
            raise ContractError("Observed pool items differ from the assigned writers")
        measured += 1
        receipt = row.get("evidence") or {}
        choice_offered = row["read_mode"] == "on_demand"
        choice_observed = bool(choice_offered and receipt.get("answered_worker")
                               and receipt["answered_worker"] == receipt.get("dispatched_worker")
                               and receipt.get("unresolved_worker") == 0)
        if choice_observed:
            read_status_measured += 1
            reads = row.get("reads") or {}
            skipped += not any(reads.values())
            list_only += bool(reads.get("memory_list")) and not reads.get("memory_get")
            if reads.get("memory_get"):
                with_get += 1
                first_get_outside += not any(item["first_get_rank"] == 1 for item in trace)
        for item in trace:
            source = sources[item["source_task"]]
            if any(item[k] != source[k] for k in ("source_run", "writer_run", "snapshot")):
                raise ContractError("Pool item references a different source experience or writer")
            relation = "own" if item["source_task"] == row["task"] else "other"
            cell = cells[relation, _source_label(source.get("source_success"))]
            cell["items"] += 1
            cell["items_with_lookup_offered"] += choice_offered
            cell["items_with_observed_choice"] += choice_observed
            cell["items_with_unobserved_choice"] += choice_offered and not choice_observed
            cell["get_requested_in_observed_choices"] += choice_observed and bool(item["get_requested"])
            cell["get_requested_in_incomplete_choices"] += not choice_observed and bool(item["get_requested"])
            cell["get_requested"] += bool(item["get_requested"])
            cell["first_selected"] += item["first_get_rank"] == 1
            cell["title_returned"] += "title" in item["returned_fields"]
            cell["body_returned"] += "body" in item["returned_fields"]
            cell["title_in_input"] += "title" in item["input_fields"]
            cell["body_in_input"] += "body" in item["input_fields"]
            before = item["before_decision_fields"]
            if before is not None:
                cell["decision_measured"] += 1
                cell["body_before_decision"] += "body" in before
                if item.get("visible_outcome_status"):
                    cell["outcome_" + item["visible_outcome_status"]] += 1
    return dict(assigned=len(rows), trace_measured=measured, read_status_measured=read_status_measured,
                without_read_calls=skipped, list_only_slots=list_only, slots_with_get=with_get,
                first_get_outside_bank=first_get_outside,
                items=[dict(relation=k[0], source_outcome=k[1], **v) for k, v in sorted(cells.items())])


def _policy_contrasts(slots):
    """Change one writing or reading policy while fixing the target, the other policy and the bank."""
    groups = defaultdict(lambda: defaultdict(dict))
    modes = {}
    for row in slots:
        form, reader, bank = row["writing_form"], row["reading_policy"], row["bank"]
        key = row["source"], row["task"], row["draw"], row["repeat"]
        groups["writing_form", reader, bank][form][key] = row
        groups["reading_policy", form, bank][reader][key] = row
        modes[reader] = row["read_mode"]
    result = []
    for (axis, fixed_policy, bank), levels in sorted(groups.items()):
        policies = sorted(levels, key=lambda p: (axis == "reading_policy" and modes.get(p) == "preload", p))
        for control, candidate in combinations(policies, 2):
            if levels[control].keys() != levels[candidate].keys():
                raise ContractError("Writing or reading comparison does not cover the same assigned target slots")
            pairs = {key: {control: row, candidate: levels[candidate][key]} for key, row in levels[control].items()}
            result.append(dict(
                varied=axis, fixed_policy=fixed_policy, bank=bank, candidate=candidate, control=control,
                official_success=_contrast(pairs, "official_success", candidate, control),
                registered_decision=_contrast(pairs, "registered_decision", candidate, control),
                effort_all_assigned=paired_effort(
                    [(a[candidate]["success"], a[control]["success"], observed_effort(a[candidate]),
                      observed_effort(a[control])) for a in pairs.values()], COHORT_EFFORT_METRICS,
                    include_transitions=True, summarize_differences=True)["all_assigned"],
            ))
    return result


def transfer_scorecard(rows):
    from agent_memory_eval.study.scorecard import effective_slots

    groups = defaultdict(lambda: defaultdict(dict))
    if len({r["origin"] for r in rows}) > 1:
        raise ContractError("Score transfer registrations separately so their independent first passes are retained")
    slots = list(effective_slots(rows).values())
    cohort = any("bank_source_tasks" in r for r in rows)
    candidate, control = ("mixed", "own") if cohort else ("real", "empty")
    metrics = ("official_success", "registered_decision", "registered_call_executed", "reference_call_executed")
    for row in slots:
        key = row["source"], row["task"], row["draw"], row["repeat"]
        groups[row["comparison"]][key][row["bank"]] = row
    comparisons = {}
    for name, pairs in groups.items():
        if any(set(arms) != {candidate, control} for arms in pairs.values()):
            raise ContractError("A transfer target is missing its paired allocation")
        exposure = {}
        reader_cost = {}
        versus_first_pass = {}
        by_source, selection = {}, {}
        for bank in (candidate, control):
            arm = [a[bank] for a in pairs.values()]
            exposure[bank] = _exposure(arm)
            by_source[bank] = _source_outcomes(arm)
            if cohort:
                selection[bank] = _pool_selection(arm)
            versus_first_pass[bank] = {metric: target_transitions(arm, metric)
                                       for metric in ("official_success", "registered_decision")}
            attempts = [row for row in rows if row["comparison"] == name and row["bank"] == bank]
            usage = [role for row in attempts for role in row.get("billing", {}).values()]
            unknown_tokens = sum(u.get("unknown_token_requests", 0) for u in usage)
            reported_tokens = sum(u["total_tokens"] for u in usage)
            unknown_cost = sum(u["unknown_count"] for u in usage)
            reported_cost = str(sum((Decimal(u["reported_usd"]) for u in usage), Decimal(0)))
            held = sum((Decimal(str(u.get("held_usd", "0"))) for u in usage), Decimal(0))
            reader_cost[bank] = dict(
                http_calls=sum(u["http_calls"] for u in usage),
                tokens=None if unknown_tokens else reported_tokens, reported_tokens=reported_tokens,
                usd=None if unknown_cost else reported_cost, reported_usd=reported_cost, unknown=unknown_cost,
                held_usd=str(held),
                unknown_token_requests=unknown_tokens, pending_reservations=sum(u.get("pending_reservations", 0) for u in usage),
                token_usage=combined_token_usage(row.get("token_usage_by_role") for row in attempts),
                solve_token_usage=combined_token_usage((row.get("effort_from_first_worker") or {}).get("by_role")
                                                      for row in attempts),
            )
        comparisons[name] = dict(
            **{metric: _contrast(pairs, metric, candidate, control) for metric in metrics},
            exposure=exposure,
            reader_usage_all_attempts=reader_cost,
            versus_target_first_pass=versus_first_pass,
            transitions_by_source_outcome=by_source,
        )
        if cohort:
            comparisons[name]["item_selection"] = selection
            comparisons[name]["mixed_minus_own_effort"] = paired_effort(
                [(a["mixed"]["success"], a["own"]["success"], observed_effort(a["mixed"]), observed_effort(a["own"]))
                 for a in pairs.values()], COHORT_EFFORT_METRICS, include_transitions=True, summarize_differences=True)
            if all(a["own"]["decision_rule"]["kind"] == "call_presence" for a in pairs.values()):
                categories = defaultdict(dict)
                for key, arms in pairs.items():
                    category = "live_relevance" if arms["own"]["decision_rule"]["expected"] else "irrelevance"
                    categories[category][key] = arms
                contrasts = {category: _contrast(group, "official_success", candidate, control)
                             for category, group in categories.items()}
                comparisons[name]["bfcl_call_presence"] = dict(by_category=contrasts, equal_category_bounds=[
                    sum(group["target_weighted_bounds"][i] for group in contrasts.values()) / 2 for i in (0, 1)
                ] if len(contrasts) == 2 else None)
    reading, same_input = _reading_conditions(slots)
    return dict(format="agent-memory-cohort-scorecard/v1" if cohort else "agent-memory-transfer-scorecard/v2", comparisons=comparisons,
                **(dict(policy_contrasts=_policy_contrasts(slots)) if cohort else {}),
                target_no_memory=target_no_memory_summary(slots), reading_conditions=reading,
                same_first_input_without_reads_or_exposure=same_input, writing_by_source=_writing_comparisons(slots),
                business_state_by_target=_business_state(slots))
