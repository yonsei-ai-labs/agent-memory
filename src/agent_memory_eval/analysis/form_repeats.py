"""Compare repeated readers of one sealed memory per form without counting repeats as sources."""

from collections import Counter

from agent_memory_eval.analysis.efficiency import paired_effort
from agent_memory_eval.analysis.paired_trials import Trial, compare_trials, sign_test
from agent_memory_eval.contracts.core import ContractError


def summarize_form_repeats(sources, readers, forms, repetitions, *, metric, cohort):
    """Rows include all scheduled reader slots, including ineligible and ungraded ones."""
    repeats = range(1, repetitions + 1)
    index = {(r["task"], r["form"], r["repeat"]): r for r in readers}
    expected = {(t, f, n) for t in sources for f in forms for n in repeats}
    if len(index) != len(readers) or set(index) != expected:
        raise ContractError("Missing or duplicate registered form repetition")

    def observation(task, rep, row):
        return Trial(task, str(rep), sources[task].get("run"), row["started"], row["success"],
                     row.get("effort", {}), metric=metric, cohort=cohort)

    # Keep the same task and repeat mix for each form's descriptive resource comparison.
    common = [(task, n) for task in sorted(sources) for n in repeats
              if sources[task]["success"] is not None and all(index[task, f, n]["success"] is not None for f in forms)]
    summary = {}
    for form in forms:
        reference, candidate, pairs, receipt_totals = [], [], [], Counter()
        for task, source in sources.items():
            rounds = [index[task, form, n] for n in repeats]
            for n, row in zip(repeats, rounds):
                reference.append(observation(task, n, source))
                candidate.append(observation(task, n, row))
                if row["success"] is not None:
                    receipt_totals.update(row.get("effects", {}))
            if repetitions == 2 and all(row["success"] is not None for row in rounds):
                pairs.append(rounds[0]["success"] != rounds[1]["success"])
        result = compare_trials(reference, candidate, test_signs=False)
        clusters = result["sources"]
        clusters["two_sided_sign_p"] = sign_test(clusters["gain"], clusters["loss"])
        common_effort = paired_effort(
            (index[t, form, n]["success"], sources[t]["success"], index[t, form, n].get("effort", {}), sources[t].get("effort", {}))
            for t, n in common)
        summary[form] = dict(common_graded_slots=[dict(task=t, repeat=n) for t, n in common],
                             common_graded_effort=common_effort["jointly_graded"],
                             same_bank_complete_pairs=len(pairs), same_bank_discordant_pairs=sum(pairs),
                             effects_in_graded=dict(receipt_totals), comparison=result)
    return summary
