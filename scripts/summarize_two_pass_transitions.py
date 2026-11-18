"""Tabulate direct first-to-second-pass outcomes from the verified full assignment CSV."""
import argparse
from collections import Counter
import csv
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.experiments.forms_batch import FORMS, write_json


def outcome(cell):
    value = cell["official_success"]
    if cell["grading_status"] == "graded":
        if value not in {"True", "False"} or cell["solve_status"] != "completed":
            raise ContractError("A graded transition lacks a binary official result")
        return "success" if value == "True" else "failure"
    if value:
        raise ContractError("An ungraded transition has an official result")
    return "not_started" if cell["solve_status"] == "not_started" else "ungraded"


def summarize(path):
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    cells = {(r["task_id"], int(r["repetition"]), r["condition"]): r for r in rows}
    if len(cells) != len(rows) or len(cells) != 352:
        raise ContractError("The transition table requires all distinct second-pass assignments")
    tasks = sorted({k[0] for k in cells}, key=int)
    origins = {}
    for task in tasks:
        source = {r["first_pass_success"] for r in rows if r["task_id"] == task}
        if len(source) != 1 or not source <= {"True", "False"}:
            raise ContractError("A task has inconsistent first-pass results")
        origins[task] = "success" if source == {"True"} else "failure"
        for arm in (*FORMS, "empty_preload", "no_memory"):
            if any((task, rep, arm) not in cells for rep in (1, 2)):
                raise ContractError("A task is missing its preassigned primary comparison")
    if Counter(origins.values()) != {"success": 24, "failure": 4}:
        raise ContractError("The archived first-pass cohort has changed")
    transitions, recovery = {}, {}
    for arm in ("no_memory", *FORMS, "empty_preload", "summary", "public_record"):
        arm_rows = [r for r in rows if r["condition"] == arm]
        counts = {}
        for prior in ("success", "failure"):
            subset = [r for r in arm_rows if origins[r["task_id"]] == prior]
            dist = Counter(outcome(r) for r in subset)
            counts[prior] = dict(source_tasks=len({r["task_id"] for r in subset}), assignments=len(subset),
                **{state: dist[state] for state in ("success", "failure", "ungraded", "not_started")})
        transitions[arm] = counts
    for task in tasks:
        if origins[task] != "failure":
            continue
        recovery[task] = {arm: [dict(repetition=rep, job=int(cells[task,rep,arm]["job"]),
                    outcome=outcome(cells[task,rep,arm])) for rep in (1, 2) if (task, rep, arm) in cells]
                    for arm in transitions}
    baseline = {t: [outcome(cells[t,rep,"no_memory"]) for rep in (1, 2)] for t in tasks}
    flipped = [t for t, pair in baseline.items() if set(pair) == {"success", "failure"}]
    return dict(format="agent-memory-two-pass-source-transitions/v1", cell_csv_sha256=digest(path.read_bytes()),
        source_task_counts=dict(Counter(origins.values())), within_task_no_memory_repeats=baseline,
        flipped_no_memory_tasks=flipped, by_condition_and_source=transitions, first_pass_failure_tasks=recovery,
        interpretation="Each trace in the first pass is reused in both second-pass blocks. This table is descriptive; the planned memory comparison uses the paired contemporaneous no-memory condition.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cells", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ContractError("Use a new output file")
    write_json(args.output, summarize(args.cells))


if __name__ == "__main__":
    main()
