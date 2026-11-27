"""One scorecard for every benchmark: outcomes, no-memory baseline, source pairs, effort, exposure and cost."""

from collections import Counter, defaultdict
from decimal import Decimal

from agent_memory_eval.contracts.core import ContractError

FORMAT = "agent-memory-study-scorecard/v1"
NO_MEMORY = "no_memory"


def _check(row):
    if row["status"] not in {"graded", "missing"}:
        raise ContractError("Each outcome row must be graded or missing")
    if (row["status"] == "graded") != isinstance(row["success"], bool):
        raise ContractError("Only a graded outcome carries a success value")


def effective_slots(rows):
    """First graded attempt per (condition, source, repeat), in listed origin order then attempt order."""
    origins = {}
    for r in rows:
        _check(r)
        origins.setdefault(r["origin"], len(origins))
    grouped = defaultdict(list)
    for r in rows:
        grouped[r["condition"], r["source"], r.get("repeat", 1)].append(r)
    slots = {}
    for key, attempts in grouped.items():
        attempts.sort(key=lambda r: (origins[r["origin"]], r["attempt"]))
        graded = [r for r in attempts if r["status"] == "graded"]
        chosen = graded[0] if graded else attempts[-1]  # A slot still missing reports why its last attempt failed.
        slots[key] = dict(chosen, after_retry=chosen is not attempts[0])
    return slots


def _mean(values):
    values = [v for v in values if v is not None]
    return float(sum(values)) / len(values) if values else None


def _effort(pairs, field):
    both = [(c[field], s[field]) for c, s in pairs if c.get(field) is not None and s.get(field) is not None]
    if not both:
        return None
    cond, src = _mean([b[0] for b in both]), _mean([b[1] for b in both])
    return dict(condition=cond, source=src, difference=cond - src)


def scorecard(rows):
    slots = effective_slots(rows)
    sources = {s: r for (c, s, n), r in slots.items() if c == NO_MEMORY and n == 1}
    tasks = defaultdict(lambda: dict(first_pass_graded=0, first_pass_successes=0))
    for r in sources.values():
        if r["status"] == "graded":
            tasks[r["task"]]["first_pass_graded"] += 1
            tasks[r["task"]]["first_pass_successes"] += r["success"]
    baseline = {t: v["first_pass_successes"] / v["first_pass_graded"] for t, v in tasks.items() if v["first_pass_graded"]}
    cost = defaultdict(Decimal)
    for r in rows:
        if r.get("cost_usd") is not None:
            cost[r["condition"]] += Decimal(r["cost_usd"])
    conditions = {}
    for name in dict.fromkeys(c for c, _, _ in slots):
        own = [r for (c, _, _), r in slots.items() if c == name]
        graded = [r for r in own if r["status"] == "graded"]
        paired = [(r, sources[r["source"]]) for r in graded
                  if r["source"] in sources and sources[r["source"]]["status"] == "graded"]
        joint = [(r, s) for r, s in paired if r["success"] and s["success"]]
        decided = [(r, s) for r, s in paired if r.get("first_decision") is not None and s.get("first_decision") is not None]
        reuse = {key: dict(same=sum(r["first_decision"] == s["first_decision"] for r, s in decided if s["success"] is outcome),
                           different=sum(r["first_decision"] != s["first_decision"] for r, s in decided if s["success"] is outcome))
                 for key, outcome in (("source_success", True), ("source_failure", False))}
        conditions[name] = dict(
            assigned=len(own), graded=len(graded), successes=sum(r["success"] for r in graded),
            success_rate=sum(r["success"] for r in graded) / len(graded) if graded else None,
            expected_no_memory_successes=sum(baseline[r["task"]] for r in graded if r["task"] in baseline),
            slots_without_baseline=sum(r["task"] not in baseline for r in graded),
            graded_after_retry=sum(r["after_retry"] for r in graded),
            step_capped=sum(bool(r.get("step_cap")) for r in graded),
            missing=dict(Counter(r["missing_reason"] for r in own if r["status"] == "missing")),
            paired_with_source=len(paired),
            recoveries=sum(not s["success"] and r["success"] for r, s in paired),
            regressions=sum(s["success"] and not r["success"] for r, s in paired),
            joint_success_with_source=dict(pairs=len(joint), tokens=_effort(joint, "tokens"),
                                           business_calls=_effort(joint, "business_calls")),
            same_first_decision_as_source=reuse,
            reads=dict(list_calls=sum(r["reads_list"] for r in own), get_calls=sum(r["reads_get"] for r in own),
                       slots_with_any_read=sum(bool(r["reads_list"] or r["reads_get"]) for r in own)),
            preloaded=sum(bool(r["preloaded"]) for r in own),
            cost_usd=str(cost[name]) if name in cost else None,
        )
    return dict(format=FORMAT, sources=len(sources), tasks=dict(tasks), conditions=conditions)


def save_analysis(root, plan_id, rows, card, *, tasks):
    """Keep the rows and scorecard an analysis read, named by content, so later plans can pin them as evidence."""
    from agent_memory_eval.contracts.core import canonical, digest
    folder = root / "artifacts" / "studies" / plan_id
    folder.mkdir(parents=True, exist_ok=True)
    refs = {}
    scope = "all" if tasks is None else "tasks-" + digest(canonical(sorted(tasks)))[:12]
    for key, value in (("rows", dict(format="agent-memory-study-rows/v1", plan_id=plan_id, scored_tasks=tasks, rows=rows)),
                       ("scorecard", dict(card, plan_id=plan_id, scored_tasks=tasks))):
        data = canonical(value) + b"\n"
        path = folder / f"{key}-{scope}-{digest(data)[:12]}.json"
        if not path.exists():
            path.write_bytes(data)
        if path.read_bytes() != data:
            raise ContractError(f"Saved analysis {path.name} changed")
        refs[key] = dict(path=str(path.relative_to(root)), sha256=digest(data))
    return refs
