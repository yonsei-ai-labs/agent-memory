"""Report policy outcomes, effort and uncertainty with distinct trial and source denominators."""

from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields
from itertools import chain

from agent_memory_eval.analysis.efficiency import METRICS, paired_effort
from agent_memory_eval.contracts.core import ContractError


@dataclass(frozen=True)
class OutcomeMetric:
    """The definition and eligible population of a binary outcome, declared by its scorer."""

    name: str
    version: int
    definition: str
    stage: str
    unit: str
    denominator: str
    cluster_unit: str
    cluster_eligibility: str

    def __post_init__(self):
        if type(self.version) is not int or self.version < 1:
            raise ContractError("An outcome metric needs a positive integer version")
        if self.cluster_eligibility not in ("all_pairs_graded", "any_pair_graded"):
            raise ContractError("Unknown outcome metric cluster_eligibility")
        for key in ("name", "definition", "stage", "unit", "denominator", "cluster_unit"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"Missing outcome metric {key}")


@dataclass(frozen=True)
class Trial:
    task: str
    slot: str
    source: str | None
    started: bool
    success: bool | None
    effort: dict = field(default_factory=dict)
    metric: OutcomeMetric = field(kw_only=True)
    cohort: str = field(kw_only=True)
    evidence_pointer: str | None = field(default=None, kw_only=True)

    def __post_init__(self):
        if not isinstance(self.metric, OutcomeMetric):
            raise ContractError("Each observation needs its scorer's outcome metric")
        if not all(isinstance(k, str) and k.strip() for k in (self.task, self.slot, self.cohort)):
            raise ContractError("Each assigned trial needs task, slot and comparison cohort identifiers")
        if self.source is not None and not (isinstance(self.source, str) and self.source):
            raise ContractError("An identified source needs a non-empty identifier")
        if type(self.started) is not bool or (self.success is not None and type(self.success) is not bool):
            raise ContractError("Trial started and observed success must be binary")
        if not self.started and (self.success is not None or any(v is not None for v in self.effort.values())):
            raise ContractError("An unstarted trial cannot have a grade or measured effort")


def _context(trials):
    rows = iter(trials)
    first = next(rows, None)
    if first is None:
        return None, None
    for trial in rows:
        if trial.metric != first.metric:
            differences = [f.name for f in fields(OutcomeMetric)
                           if getattr(first.metric, f.name) != getattr(trial.metric, f.name)]
            raise ContractError(f"Outcome metric mismatch ({', '.join(differences)}): "
                                f"{first.metric.name} v{first.metric.version} vs {trial.metric.name} v{trial.metric.version}")
        if trial.cohort != first.cohort:
            raise ContractError(f"Different comparison cohorts: {first.cohort} vs {trial.cohort}")
    return first.metric, first.cohort


def _index(trials):
    result = {}
    for trial in trials:
        key = (trial.task, trial.slot)
        if key in result:
            raise ContractError("Duplicate policy allocation")
        result[key] = trial
    return result


def outcome_coverage(trials):
    trials = list(trials)
    _context(trials)
    graded = sum(t.success is not None for t in trials)
    successes = sum(t.success is True for t in trials)
    return dict(assigned=len(trials), started=sum(t.started for t in trials), graded=graded,
                successes=successes, success_rate=successes / graded if graded else None)


def sign_test(gains, losses):
    """Two-sided exact sign test, conditional on discordances. No informative units gives no test."""
    if any(type(value) is not int or value < 0 for value in (gains, losses)):
        raise ContractError("Sign-test counts must be nonnegative integers")
    n = gains + losses
    if not n:
        return None
    k = min(gains, losses)
    if 2 * k >= n - 1:
        return 1.0
    total = term = 1
    for i in range(1, k + 1):
        term = term * (n - i + 1) // i
        total += term
    return (2 * total) / (1 << n)


def compare_trials(reference, candidate, *, metrics=METRICS, test_signs=True):
    left, right = _index(reference), _index(candidate)
    metric, cohort = _context(chain(left.values(), right.values()))
    if left.keys() != right.keys():
        raise ContractError("Every registered slot needs an explicit observation or missing row in each policy")
    pairs, by_source, joint_success = [], defaultdict(list), []
    for key in sorted(left):
        a, b = left[key], right[key]
        if a.source != b.source:
            raise ContractError("Paired trials came from different source experiences")
        pairs.append((b.success, a.success, b.effort, a.effort))
        observed = a.success is not None and b.success is not None
        if a.source is not None:
            by_source[a.source].append(int(b.success) - int(a.success) if observed else None)
        if a.success is True and b.success is True:
            joint_success.append(dict(task=a.task, slot=a.slot, source=a.source))
    effort = paired_effort(pairs, metrics=metrics)
    counts = effort["outcomes"]
    complete = 0
    included = []
    for differences in by_source.values():
        observed = [change for change in differences if change is not None]
        all_graded = len(observed) == len(differences)
        complete += all_graded
        if all_graded or (observed and metric.cluster_eligibility == "any_pair_graded"):
            included.append(sum(observed))
    gains, losses = sum(x > 0 for x in included), sum(x < 0 for x in included)
    paired = counts["assigned"] - counts["missing"]
    return dict(metric=asdict(metric) if metric is not None else None, cohort=cohort,
                reference=outcome_coverage(left.values()), candidate=outcome_coverage(right.values()),
                pairs={**counts, "jointly_graded": paired,
                       "discordance_rate": (counts["gain"] + counts["loss"]) / paired if paired else None,
                       "sign_p_if_independent": sign_test(counts["gain"], counts["loss"]) if test_signs else None},
                sources=dict(assigned=len(by_source), unknown_source_pairs=sum(v.source is None for v in left.values()),
                             complete=complete, included=len(included), gain=gains, loss=losses,
                             tie=included.count(0), sign_p_if_independent=sign_test(gains, losses) if test_signs else None),
                joint_success_members=joint_success, joint_success_effort=effort["both_success"],
                jointly_graded_effort=effort["jointly_graded"])


def target_effects(expected, attempts):
    """Match accepted side effects to intended targets, keeping duplicate and unconfirmed attempts."""
    required = set(expected)
    seen = set()
    result = dict(expected=len(required), attempted=0, accepted_target=0, accepted_other=0,
                  accepted_duplicate=0, rejected=0, unconfirmed=0)
    for target, accepted in attempts:
        result["attempted"] += 1
        if accepted is None:
            result["unconfirmed"] += 1
        elif type(accepted) is not bool:
            raise ContractError("An effect needs a binary receipt or an unconfirmed outcome")
        elif not accepted:
            result["rejected"] += 1
        elif target not in required:
            result["accepted_other"] += 1
        elif target in seen:
            result["accepted_duplicate"] += 1
        else:
            result["accepted_target"] += 1
            seen.add(target)
    result["targets_without_confirmed_effect"] = len(required - seen)
    return result


def component_coverage(expected, confirmed):
    """Count distinct required documents or action checks that were observed in a graded episode."""
    required, seen = set(expected), set(confirmed)
    if not seen <= required:
        raise ContractError("An observed component is outside the declared reference set")
    return dict(required=len(required), confirmed=len(seen), missing=len(required - seen))
