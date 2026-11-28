"""Within-target comparisons of a frozen donor memory and an identically presented empty bank."""

from collections import Counter, defaultdict
from decimal import Decimal
import re

from agent_memory_eval.analysis.efficiency import combined_token_usage
from agent_memory_eval.analysis.policy_evidence import stored_outcome_check
from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.study.transfer_baselines import baseline_slots, outcome, target_no_memory_summary, target_transitions


OUTCOME_LINES = {"success": "Prior task outcome: success.", "failure": "Prior task outcome: failure."}
TITLE_OUTCOME = re.compile(r"\b(?:prior\s+)?task\s+outcome\s*[:=]?\s*(success|failure)\b", re.I)


def reader_arms(condition):
    return condition, condition + "__empty"


def decision_evidence(rule, steps, *, completed):
    """Use only answered worker requests. An absent action is conclusive only after normal termination."""
    title = body = False
    for step in steps:
        title |= step.get("title", False)
        body |= step["body"]
        for call in step["calls"]:
            if call["name"] not in rule["tools"]:
                continue
            correct = call["name"] == rule["expected"] and all(
                call["arguments"].get(k) == v for k, v in rule.get("arguments", {}).items()
            )
            return dict(correct=correct, call=call, request=step["request"], title_seen=title, body_seen=body)
    return dict(correct=rule["expected"] is None if completed else None, call=None, request=None,
                title_seen=title, body_seen=body)


def allocation_rows(plan, attempts, *, written_memories=None, target_baselines=None):
    """Materialize every registered target slot, including blocked, crashed and never-started sessions."""
    from agent_memory_eval.study.plan import arm_name, conditions

    expected = {}
    controls = baseline_slots(plan, target_baselines or {})
    for pair in plan["transfer"]["pairs"]:
        for draw in range(1, plan["first_pass"]["draws"] + 1):
            for repeat in range(1, plan["repeats"] + 1):
                for rep, reader in conditions(plan):
                    name = arm_name(rep, reader)
                    for bank, arm in zip(("real", "empty"), reader_arms(name)):
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
        rows.append(dict(row, **expected[key]))
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
                           feedback_success=note["feedback_success"], explicit_title_outcomes=title_outcomes,
                           first_line_check=stored_outcome_check(OUTCOME_LINES, note["feedback_success"], items),
                           token_usage_by_role=note.get("token_usage_by_role"))
    return [blocks[key] for key in sorted(blocks)]


def _outcome_counts(rows, metric):
    values = [outcome(r, metric) for r in rows]
    return dict(assigned=len(values), successes=values.count(True), failures=values.count(False), missing=values.count(None))


def _reading_conditions(slots):
    groups = defaultdict(list)
    same_input = defaultdict(list)
    for row in slots:
        if not row.get("reading_policy"):
            continue
        groups[row["reading_policy"], row["read_mode"], row["bank"]].append(row)
        receipt = row.get("evidence") or {}
        if (row.get("opening_sha256") and row.get("first_worker_sha256") and receipt.get("dispatched_worker")
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


def _contrast(pairs, metric):
    counts = Counter()
    per_target = defaultdict(list)
    for (_, target, _, _), arms in pairs.items():

        def value(bank):
            row = arms[bank]
            return row["success"] if metric == "success" else (row.get("decision") or {}).get("correct")

        real, empty = value("real"), value("empty")
        if real is None or empty is None:
            counts["missing"] += 1
        elif real == empty:
            counts["both_success" if real else "both_failure"] += 1
        else:
            counts["real_only" if real else "empty_only"] += 1
        lower = int(real or False) - int(empty is not False)
        upper = int(real is not False) - int(empty or False)
        per_target[target].append((lower, upper))
    means = [[sum(v[i] for v in cells) / len(cells) for i in (0, 1)] for cells in per_target.values()]
    return dict(
        assigned=len(pairs),
        targets=len(means),
        **{k: counts[k] for k in ("real_only", "empty_only", "both_success", "both_failure", "missing")},
        target_weighted_bounds=[sum(m[i] for m in means) / len(means) for i in (0, 1)] if means else None,
    )


def _exposure(arm):
    decisions = [r["decision"] for r in arm if (r.get("decision") or {}).get("correct") is not None]
    receipts = [r.get("evidence") or {} for r in arm]
    attempts = [r.get("reads") or {} for r in arm]
    return dict(
        decisions=len(decisions),
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
        db_mismatches = correct_decision_on_failure = 0
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
            db_mismatches += state["official_db_match"] is False
            changed = {f"{entity['table']}.{field}" for entity in state["target_entities"]
                       for field in entity["different_fields"]}
            fields.update(changed)
            if row["success"] is False:
                failed_fields.update(changed)
        summary[target] = dict(assigned=len(rows), official_failures=sum(r["success"] is False for r in rows),
                               correct_registered_decision_on_official_failure=correct_decision_on_failure,
                               state_status=dict(statuses), reasons=dict(reasons),
                               official_db_mismatches=db_mismatches if statuses["verified"] else None,
                               first_call_receipts=dict(responses), field_differences=dict(fields),
                               field_differences_on_official_failure=dict(failed_fields))
    return summary


def transfer_scorecard(rows):
    from agent_memory_eval.study.scorecard import effective_slots

    groups = defaultdict(lambda: defaultdict(dict))
    slots = list(effective_slots(rows).values())
    for row in slots:
        key = row["source"], row["task"], row["draw"], row["repeat"]
        groups[row["comparison"]][key][row["bank"]] = row
    comparisons = {}
    for name, pairs in groups.items():
        if any(set(arms) != {"real", "empty"} for arms in pairs.values()):
            raise ContractError("A transfer target is missing its paired allocation")
        exposure = {}
        reader_cost = {}
        versus_first_pass = {}
        for bank in ("real", "empty"):
            arm = [a[bank] for a in pairs.values()]
            exposure[bank] = _exposure(arm)
            versus_first_pass[bank] = {metric: target_transitions(arm, metric)
                                       for metric in ("official_success", "registered_decision")}
            attempts = [row for row in rows if row["comparison"] == name and row["bank"] == bank]
            usage = [role for row in attempts for role in row.get("billing", {}).values()]
            reader_cost[bank] = dict(
                http_calls=sum(u["http_calls"] for u in usage),
                tokens=sum(u["total_tokens"] for u in usage),
                reported_usd=str(sum((Decimal(u["reported_usd"]) for u in usage), Decimal(0))),
                unknown=sum(u["unknown_count"] for u in usage),
                token_usage=combined_token_usage(row.get("token_usage_by_role") for row in attempts),
                solve_token_usage=combined_token_usage((row.get("effort_from_first_worker") or {}).get("by_role")
                                                      for row in attempts),
            )
        comparisons[name] = dict(
            official_success=_contrast(pairs, "success"),
            registered_decision=_contrast(pairs, "decision"),
            exposure=exposure,
            reader_usage_all_attempts=reader_cost,
            versus_target_first_pass=versus_first_pass,
        )
    reading, same_input = _reading_conditions(slots)
    return dict(format="agent-memory-transfer-scorecard/v2", comparisons=comparisons,
                target_no_memory=target_no_memory_summary(slots), reading_conditions=reading,
                same_first_input_without_reads_or_exposure=same_input, writing_by_source=_writing_comparisons(slots),
                business_state_by_target=_business_state(slots))
