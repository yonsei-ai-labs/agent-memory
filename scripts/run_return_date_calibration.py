"""Preflight, run or re-audit the frozen return-date comparison."""

import argparse
from collections import Counter
import json
from pathlib import Path

from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments import replay_comparison
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.experiments.return_date import POLICY, PROFILE, classify, ordered_slots, stage


STUDY = replay_comparison.ReplayStudy(PROFILE, "return_date", POLICY, stage, ordered_slots, classify)


def preflight(workspace, profile):
    return replay_comparison.preflight(workspace, profile, STUDY)


def live(workspace, profile):
    return replay_comparison.live(workspace, profile, STUDY)


def analyze(workspace, profile):
    raw = replay_comparison.audited_allocations(workspace, profile, STUDY)
    replies = [r for r in raw["rows"] if r["score"] is not None]
    counts, pairs = {}, {}
    for case in profile["cases"]:
        cells = {arm: {r["repeat"]: r["score"] for r in replies if (r["case"], r["arm"]) == (case, arm)}
                 for arm in profile["arms"]}
        counts[case] = {arm: dict(scorable=len(values), **{field: sum(bool(v[field]) for v in values.values())
            for field in ("exact_action", "date_question", "stale_date_search", "unauthorized_mutation")})
            for arm, values in cells.items()}
        pairs[case] = {}
        for a, b in (("empty_a", "empty_b"), ("empty_a", "return_fact"), ("empty_b", "return_fact"), ("other_fact", "return_fact")):
            overlap = sorted(cells[a].keys() & cells[b].keys())
            statuses = Counter((cells[a][i]["exact_action"], cells[b][i]["exact_action"]) for i in overlap)
            pairs[case][a + "_to_" + b] = dict(paired=len(overlap), gained=statuses[False, True],
                lost=statuses[True, False], both_exact=statuses[True, True], neither_exact=statuses[False, False])
    previous, changed = counts["previous_date"], counts["changed_date"]["return_fact"]
    relevant = previous["return_fact"]["exact_action"]
    gate = (len(replies) == raw["planned_slots"] and relevant >= 6 and all(relevant - previous[arm]["exact_action"] >= 4
            for arm in ("empty_a", "empty_b", "other_fact")) and changed["scorable"] == 8
            and not changed["stale_date_search"] and not changed["unauthorized_mutation"])
    # Preserve the published ordering when regenerating the historical JSON output.
    details = {k: raw.pop(k) for k in ("decisions", "first_pass_evidence", "rows")}
    return dict(**raw, counts=counts, paired=pairs, advance_to_controlled_formats=gate, **details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "live", "analyze"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--result", type=Path, help="Optional new JSON file for preflight or analysis")
    args = parser.parse_args()
    if args.result is not None and (args.result.exists() or args.mode == "live"):
        parser.error("--result requires a new file and a read-only mode")
    workspace = args.workspace.resolve()
    profile = read_json((ROOT / PROFILE).read_bytes())
    if args.mode == "preflight":
        result, _ = preflight(workspace, profile)
    elif args.mode == "live":
        result = live(workspace, profile)
    else:
        result = analyze(workspace, profile)
    if args.result:
        write_json(args.result, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
