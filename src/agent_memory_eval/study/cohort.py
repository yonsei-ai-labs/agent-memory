"""Bank membership for paired own and mixed memory on the same task."""

from collections import defaultdict

from agent_memory_eval.contracts.core import ContractError, canonical, digest


def is_cohort(plan):
    return plan.get("transfer", {}).get("bank_comparison") == "own_vs_mixed"


def check_through_stage(plan, stage):
    if stage is not None and (not is_cohort(plan) or stage not in {"source", "write"}):
        raise ContractError("A source or writing checkpoint requires an own/mixed cohort")


def pool_sources(plan, target, draw=1):
    """Freeze the same set and order for all writing and reading policies of this target."""
    pair, = [p for p in plan["transfer"]["pairs"] if p["target"] == target]
    others = pair.get("other_sources")
    if others is None:
        others = [task for task in plan["benchmark"]["tasks"] if task != target]
    return sorted([target, *others], key=lambda task: digest(canonical([plan["id"], target, draw, task])))


def condition_banks(plan, name):
    if is_cohort(plan):
        return [("own", name + "__own"), ("mixed", name + "__mixed")]
    return [("real", name), ("empty", name + "__empty")]


def source_components(pools):
    """Group all tasks linked directly or indirectly through a registered memory."""
    neighbors = defaultdict(set)
    for target, sources in pools:
        for source in sources:
            neighbors[target].add(source)
            neighbors[source].add(target)
    remaining = set(neighbors)
    groups = []
    while remaining:
        members = set()
        pending = [remaining.pop()]
        while pending:
            task = pending.pop()
            members.add(task)
            linked = remaining.intersection(neighbors[task])
            remaining.difference_update(linked)
            pending.extend(linked)
        groups.append(sorted(members))
    return sorted(groups)
