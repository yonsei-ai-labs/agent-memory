"""Within-target comparisons of a frozen donor memory and an identically presented empty bank."""

from collections import Counter, defaultdict
from decimal import Decimal

from agent_memory_eval.contracts.core import ContractError


def reader_arms(condition):
    return condition, condition + "__empty"


def decision_evidence(rule, steps, *, completed):
    """Use only answered worker requests. An absent action is conclusive only after normal termination."""
    exposed = False
    for step in steps:
        exposed |= step["body"]
        for call in step["calls"]:
            if call["name"] not in rule["tools"]:
                continue
            correct = call["name"] == rule["expected"] and all(
                call["arguments"].get(k) == v for k, v in rule.get("arguments", {}).items()
            )
            return dict(correct=correct, call=call, request=step["request"], body_seen=exposed)
    return dict(correct=rule["expected"] is None if completed else None, call=None, request=None, body_seen=exposed)


def allocation_rows(plan, attempts, *, written_memories=None):
    """Materialize every registered target slot, including blocked, crashed and never-started sessions."""
    from agent_memory_eval.study.plan import arm_name, conditions

    expected = {}
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


def transfer_scorecard(rows):
    from agent_memory_eval.study.scorecard import effective_slots

    groups = defaultdict(lambda: defaultdict(dict))
    for row in effective_slots(rows).values():
        key = row["source"], row["task"], row["draw"], row["repeat"]
        groups[row["comparison"]][key][row["bank"]] = row
    comparisons = {}
    for name, pairs in groups.items():
        if any(set(arms) != {"real", "empty"} for arms in pairs.values()):
            raise ContractError("A transfer target is missing its paired allocation")
        exposure = {}
        reader_cost = {}
        for bank in ("real", "empty"):
            decisions = [a[bank].get("decision") for a in pairs.values()]
            measured = [d for d in decisions if d is not None and d["correct"] is not None]
            reads = [a[bank].get("reads", {}) for a in pairs.values()]
            exposure[bank] = dict(
                decisions=len(measured),
                body_seen=sum(d["body_seen"] for d in measured),
                slots_listing=sum(bool(r.get("memory_list")) for r in reads),
                slots_getting=sum(bool(r.get("memory_get")) for r in reads),
            )
            usage = [
                role
                for row in rows
                if row["comparison"] == name and row["bank"] == bank
                for role in row.get("billing", {}).values()
            ]
            reader_cost[bank] = dict(
                http_calls=sum(u["http_calls"] for u in usage),
                tokens=sum(u["total_tokens"] for u in usage),
                reported_usd=str(sum((Decimal(u["reported_usd"]) for u in usage), Decimal(0))),
                unknown=sum(u["unknown_count"] for u in usage),
            )
        comparisons[name] = dict(
            official_success=_contrast(pairs, "success"),
            registered_decision=_contrast(pairs, "decision"),
            exposure=exposure,
            reader_usage_all_attempts=reader_cost,
        )
    return dict(format="agent-memory-transfer-scorecard/v1", comparisons=comparisons)
