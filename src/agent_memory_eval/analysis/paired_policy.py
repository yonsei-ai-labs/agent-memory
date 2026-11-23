"""Equal-customer contrasts for policies applied to the same registered cases."""

from collections import defaultdict
from math import ceil
from random import Random
from statistics import fmean

from agent_memory_eval.contracts.core import ContractError


def customer_contrast(cases, coefficients, *, uncertainty=None):
    """Each (customer, case) maps every required arm to a binary outcome or None."""
    observed, lower, upper = defaultdict(list), defaultdict(list), defaultdict(list)
    for (customer, _), arms in cases.items():
        if set(arms) != set(coefficients) or any(value not in (None, 0, 1) for value in arms.values()):
            raise ContractError("Incomplete allocation or nonbinary policy outcome")
        known = sum(coefficients[k] * value for k, value in arms.items() if value is not None)
        missing = [coefficients[k] for k, value in arms.items() if value is None]
        lower[customer].append(known + sum(min(0, c) for c in missing))
        upper[customer].append(known + sum(max(0, c) for c in missing))
        if not missing:
            observed[customer].append(known)
    values = [fmean(observed[u]) for u in sorted(observed)]
    interval = None
    if uncertainty and len(values) >= 2:
        rng = Random(uncertainty["seed"])
        n = uncertainty["replicates"]
        draws = sorted(fmean(rng.choices(values, k=len(values))) for _ in range(n))
        interval = [draws[ceil(n * p) - 1] for p in (0.025, 0.975)]
    return {
        "registered_cases": len(cases), "complete_cases": sum(map(len, observed.values())),
        "registered_customers": len(lower), "observed_customers": len(values),
        "difference": fmean(values) if values else None, "interval": interval,
        "gain": sum(v > 0 for v in values), "tie": values.count(0), "loss": sum(v < 0 for v in values),
        "bounds": [fmean(fmean(group[u]) for u in sorted(group)) for group in (lower, upper)] if lower else None,
    }
