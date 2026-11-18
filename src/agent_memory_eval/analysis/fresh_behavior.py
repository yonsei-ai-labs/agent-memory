"""Analyst-only one-to-one alignment of native airline mutations and their arguments."""

from collections import Counter, defaultdict

from agent_memory_eval.contracts.core import ContractError, canonical


def field_values(arguments):
    values = {}
    for key, value in arguments.items():
        if key in {"user_id", "reservation_id"}:
            continue
        if key == "flights":
            values["flight_numbers"] = [f["flight_number"] for f in value]
            values["flight_dates"] = [f["date"] for f in value]
        elif key in {"payment_methods", "passengers"}:
            values[key] = sorted(value, key=canonical)
        else:
            values[key] = value
    return values


def action_key(name, arguments):
    return name, arguments.get("reservation_id") or arguments.get("user_id")


def inspect_mutations(messages, criteria, write_tools):
    """Match each eligible call once, in recorded order within its tool and target."""
    replies = {m["id"]: (i, m) for i, m in enumerate(messages) if m["role"] == "tool"}
    attempts = []
    for i, m in enumerate(messages):
        for c in m.get("tool_calls") or []:
            if m["role"] != "assistant" or c["name"] not in write_tools:
                continue
            j, reply = replies.get(c["id"], (None, {}))
            if j is None:
                status = "missing_result"
            elif reply.get("execution_status") == "rejected":
                status = "rejected"
            elif reply.get("error"):
                status = "error"
            else:
                status = "executed"
            attempts.append(dict(turn=i, result_turn=j, tool=c["name"], arguments=c["arguments"], status=status))
    used = set()
    aligned = []
    for step in criteria["actions"]:
        tool, expected = step["name"], step["arguments"]
        if tool not in write_tools:
            continue
        key = action_key(tool, expected)
        pick = next(
            (
                i
                for i, a in enumerate(attempts)
                if i not in used and a["status"] == "executed" and action_key(a["tool"], a["arguments"]) == key
            ),
            None,
        )
        current = attempts[pick] if pick is not None else None
        observed = field_values(current["arguments"]) if current else {}
        checks = {k: k in observed and observed[k] == v for k, v in field_values(expected).items()}
        if current:
            used.add(pick)
        aligned.append(
            dict(
                reference_id=step["action_id"],
                tool=tool,
                target=key[1],
                expected=expected,
                observed_turn=current["turn"] if current else None,
                target_hit=bool(current),
                fields=checks,
                all_requested_fields=bool(current) and all(checks.values()),
            )
        )
    extras = [a["turn"] for i, a in enumerate(attempts) if a["status"] == "executed" and i not in used]
    return dict(attempts=attempts, aligned=aligned, extra_write_turns=extras)


def field_rates(cells):
    result = {}
    for arm in dict.fromkeys(c["arm"] for c in cells):
        counts = defaultdict(Counter)
        for c in cells:
            if c["arm"] != arm or c.get("mutations") is None:
                continue
            for step in c["mutations"]["aligned"]:
                for field, met in dict(
                    target=step["target_hit"], full_reference_write=step["all_requested_fields"], **step["fields"]
                ).items():
                    counts[field]["applicable"] += 1
                    counts[field]["matched"] += met
        result[arm] = dict(counts)
    return result


def tabulate_groups(cells, groups, arms):
    table = {}
    for label, tasks in groups.items():
        table[label] = {}
        for arm in arms:
            subset = [r for r in cells if r["arm"] == arm and r["task_id"] in tasks]
            counts = Counter(r["official_success"] for r in subset)
            if any(v is not None and type(v) is not bool for v in counts):
                raise ContractError("Invalid outcome denominator")
            table[label][arm] = dict(
                planned=len(subset),
                graded=counts[True] + counts[False],
                success=counts[True],
                failure=counts[False],
                missing=counts[None],
            )
    return table


def both_controls(cells, forms):
    """Report every paired block, preserving ungraded and empty-body assignments."""
    blocks = defaultdict(dict)
    for cell in cells:
        blocks[cell["task_id"], cell["repetition"]][cell["arm"]] = cell
    evidence = []
    for (task, rep), arms in blocks.items():
        no_memory, empty = arms["no_memory"], arms["empty_preload"]
        for form in forms:
            treated = arms[form]
            outcome, base, blank = (r["official_success"] for r in (treated, no_memory, empty))
            if None in (outcome, base, blank):
                label = "ungraded"
            elif base != blank:
                label = "controls_disagree"
            elif outcome == base:
                label = "all_success" if outcome else "all_failure"
            else:
                label = "gain_vs_both" if outcome else "loss_vs_both"
            evidence.append(
                dict(
                    task_id=task,
                    repetition=rep,
                    arm=form,
                    label=label,
                    job=treated["index"],
                    no_memory_job=no_memory["index"],
                    empty_job=empty["index"],
                    body_exposed=treated.get("body_exposed"),
                )
            )
    return evidence
