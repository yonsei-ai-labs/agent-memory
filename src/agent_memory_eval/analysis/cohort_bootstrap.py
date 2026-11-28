"""Resample the registered memory-sharing groups together for every policy in a benchmark."""

from functools import cached_property
from random import Random


class CohortBootstrap:
    seed = 20261118
    repetitions = 5000

    def __init__(self, groups):
        self.groups = sorted(set(groups))

    @cached_property
    def samples(self):
        rng = Random(self.seed)
        indices = range(len(self.groups))
        return [rng.choices(indices, k=len(indices)) for _ in range(self.repetitions)]

    def estimates(self, grouped):
        """Each sampled group contributes all its observed target means, including its multiplicity."""
        if not grouped:
            return [None] * self.repetitions
        if len(grouped) == 1:
            group, targets = next(iter(grouped.items()))
            index = self.groups.index(group)
            mean = sum(targets) / len(targets)
            return [mean if index in sample else None for sample in self.samples]
        sums = [sum(grouped.get(group, ())) for group in self.groups]
        sizes = [len(grouped.get(group, ())) for group in self.groups]
        values = []
        for sample in self.samples:
            count = sum(map(sizes.__getitem__, sample))
            values.append(sum(map(sums.__getitem__, sample)) / count if count else None)
        return values

    def design(self):
        return dict(unit="registered_shared_memory_group", groups=self.groups, seed=self.seed,
                    resamples=self.repetitions, percentiles=[2.5, 97.5], interpolation="linear",
                    minimum_observed_groups=2, draws_without_estimate="excluded")


def interval(values, observed_groups):
    """A draw with no estimable contrast cannot contribute to its percentile distribution."""
    ordered = sorted(v for v in values if v is not None)
    limits = None
    if observed_groups >= 2 and ordered:
        limits = []
        for p in (0.025, 0.975):
            index = (len(ordered) - 1) * p
            left = int(index)
            right = min(left + 1, len(ordered) - 1)
            limits.append(ordered[left] + (index - left) * (ordered[right] - ordered[left]))
    return dict(resamples_with_estimate=len(ordered), percentile_interval=limits)
