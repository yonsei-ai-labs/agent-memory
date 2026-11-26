"""A paired contrast with equal weights for strata, tasks and independent clusters."""

from collections import defaultdict
from fractions import Fraction
from statistics import mean

from agent_memory_eval.analysis.paired_trials import sign_test
from agent_memory_eval.contracts.core import ContractError


def clustered_contrast(pairs, *, cluster_unit):
    """Each tuple is (cluster, task, stratum, slot, reference, candidate). None denotes a missing grade."""
    if not isinstance(cluster_unit, str) or not cluster_unit.strip():
        raise ContractError("Specify the independent unit of the comparison")
    strata = defaultdict(list)
    identities = {}
    seen = set()
    for cluster, task, stratum, slot, reference, candidate in pairs:
        key = task, stratum, slot
        if key in seen or identities.setdefault(task, cluster) != cluster:
            raise ContractError("Duplicate comparison slot or inconsistent cluster")
        if any(value is not None and type(value) is not bool for value in (reference, candidate)):
            raise ContractError("Expected binary paired observations")
        seen.add(key)
        strata[cluster, task, stratum].append((reference, candidate))
    if not seen:
        raise ContractError("Register at least one comparison slot")

    def combine(entries):
        points = [point for point, _, _ in entries]
        observed = mean(points) if None not in points else None
        return observed, mean([low for _, low, _ in entries]), mean([high for _, _, high in entries])

    tasks = defaultdict(list)
    observed_pairs = 0
    for (cluster, task, _), rows in strata.items():
        observed = [int(b) - int(a) for a, b in rows if a is not None and b is not None]
        lower = sum(int(b or False) - int(True if a is None else a) for a, b in rows)
        upper = sum(int(True if b is None else b) - int(a or False) for a, b in rows)
        observed_pairs += len(observed)
        tasks[cluster, task].append((Fraction(sum(observed), len(observed)) if observed else None,
                                      Fraction(lower, len(rows)), Fraction(upper, len(rows))))
    clusters = defaultdict(list)
    for (cluster, _), values in tasks.items():
        clusters[cluster].append(combine(values))
    by_cluster, points, lower, upper = {}, [], [], []
    for cluster, values in sorted(clusters.items()):
        point, low, high = combine(values)
        if point is not None:
            points.append(point)
        lower.append(low)
        upper.append(high)
        by_cluster[cluster] = dict(tasks=len(values), difference=float(point) if point is not None else None,
                                     missing_bounds=[float(low), float(high)])
    positive, negative = sum(p > 0 for p in points), sum(p < 0 for p in points)
    return dict(cluster_unit=cluster_unit, assigned_pairs=len(seen), observed_pairs=observed_pairs, tasks=len(tasks), clusters=len(clusters),
                observed_clusters=len(points), difference=float(mean(points)) if points else None,
                missing_bounds=[float(mean(lower)), float(mean(upper))], positive_clusters=positive,
                negative_clusters=negative, tied_clusters=len(points) - positive - negative,
                two_sided_cluster_sign_p=sign_test(positive, negative), by_cluster=by_cluster)
