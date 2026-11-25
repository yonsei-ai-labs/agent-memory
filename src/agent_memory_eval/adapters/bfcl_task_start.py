"""First decisions for BFCL V4 simple_python, a stateless function-selection task."""

import subprocess

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.runner.task_start import TaskKey, TaskStartMismatch, open_task_start


REVISION = "6ea57973c7a6097fd7c5915698c54c17c5b1b6c8"
DATA = "berkeley-function-call-leaderboard/bfcl_eval/data/BFCL_v4_simple_python.json"


def simple_python_tasks(checkout):
    # Read only the public task file from the pinned Git object. Sparse checkouts also work.
    data = subprocess.check_output(["git", "-C", str(checkout), "show", f"{REVISION}:{DATA}"])
    rows = [read_json(line) for line in data.splitlines() if line]
    tasks = {row["id"]: row for row in rows}
    if not tasks or len(tasks) != len(rows):
        raise TaskStartMismatch("Empty or duplicate IDs in the BFCL task file")
    return tasks


def open_simple_python(row, run_controls, *, reference=None):
    """Build a fresh first input in BFCL's public question/function representation."""
    if (set(row) != {"id", "question", "function"} or not row["id"].startswith("simple_python_")
            or len(row["question"]) != 1 or len(row["question"][0]) != 1
            or set(row["question"][0][0]) != {"role", "content"} or row["question"][0][0]["role"] != "user"
            or not isinstance(row["question"][0][0]["content"], str) or not row["question"][0][0]["content"].strip()
            or not row["function"] or any(set(f) != {"name", "description", "parameters"} for f in row["function"])
            or not isinstance(run_controls, dict) or not run_controls):
        raise TaskStartMismatch("Expected a BFCL simple_python public task and explicit run controls")
    task = TaskKey("bfcl", REVISION, row["id"])
    controls = dict(category="simple_python", task_sha256=digest(canonical(row)), run=run_controls)
    return open_task_start(task, controls, lambda _: dict(messages=row["question"][0], functions=row["function"]),
                           reference=reference)


def open_relevance(row, category, run_controls, *, reference=None):
    """A public, single-turn decision about whether to call a function."""
    if (category not in ("live_relevance", "irrelevance") or not row["id"].startswith(category + "_")
            or len(row["question"]) != 1 or len(row["question"][0]) != 1
            or set(row["question"][0][0]) != {"role", "content"} or row["question"][0][0]["role"] != "user"
            or not isinstance(row["question"][0][0]["content"], str) or not row["function"]
            or not isinstance(run_controls, dict) or not run_controls):
        raise TaskStartMismatch("Expected a single BFCL relevance question and explicit run controls")
    task = TaskKey("bfcl", REVISION, row["id"])
    controls = dict(category=category, task_sha256=digest(canonical(row)), run=run_controls)
    return open_task_start(task, controls, lambda _: dict(messages=row["question"][0], functions=row["function"]),
                           reference=reference)
