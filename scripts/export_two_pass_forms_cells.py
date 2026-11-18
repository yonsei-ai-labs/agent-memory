"""Export every preassigned second-pass cell and the matched empty-bank diagnostic."""
import argparse
import csv
from collections import Counter
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.experiments.forms_batch import ordered_jobs, write_json


def cells(run, writing_review):
    ledger = run / "allocations.json"
    control_file = run / "control.json"
    completion = read_json((run / "completion.json").read_bytes())
    if digest(ledger.read_bytes()) != completion["allocation_sha256"] or digest(control_file.read_bytes()) != completion["control_sha256"]:
        raise ContractError("Evidence differs from the completion receipt")
    rows = read_json(ledger.read_bytes())
    control = read_json(control_file.read_bytes())
    expected = ordered_jobs(control["design"])
    if len(rows) != len(expected) or any(any(r[k] != v for k, v in e.items()) for r, e in zip(rows, expected)):
        raise ContractError("Output rows differ from the preassigned cohort")
    cohort = {s["task_id"]: s for s in control["design"]["source"]["cohort"]}
    notes = {x["index"]: x for x in writing_review["annotations"]}
    solves, writers = {}, {}
    for row in rows:
        table = solves if row["phase"] == "solve" else writers
        table[row["task_id"], row["repetition"], row["arm"]] = row

    table = []
    empty_comparisons = Counter()
    empty_pairs = []
    for (task, rep, arm), row in solves.items():
        result = row.get("result", {})
        own_writer = writers.get((task, rep, arm))
        note = notes.get(own_writer["index"], {}) if own_writer else {}
        if own_writer and own_writer.get("result") and own_writer["run_id"] != note.get("run_id"):
            raise ContractError("Writing annotations refer to a different assignment")
        base = solves[task, rep, "no_memory"].get("result", {})
        empty = solves[task, rep, "empty_preload"].get("result", {})
        costs = result.get("billing_by_role", {})
        success = result.get("official_success")
        bs = base.get("official_success")
        difference = int(success) - int(bs) if type(success) is bool and type(bs) is bool else None
        cell = dict(task_id=task, source_group=cohort[task]["source_group"], first_pass_success=control["source_outcomes"][task],
            reference_db_write=cohort[task]["reference_has_db_write"], repetition=rep, condition=arm, job=row["index"],
            writer_status=own_writer["status"] if own_writer else None, writer_items=note.get("item_count"),
            writer_title_body_utf8_bytes=note.get("title_body_utf8_bytes"), reviewed_grounding=note.get("grounding"),
            solve_status=row["status"], skip_reason=row.get("reason"), grading_status=result.get("grading_status"),
            official_score=result.get("official_score"), official_success=success, control_success=bs,
            difference_from_no_memory=difference, termination_reason=result.get("termination_reason"),
            error_code=result.get("error_code"), native_steps=result.get("native_steps"),
            normal_response_memory_spans=result.get("normal_response_memory_spans"), worker_requests=(result.get("requests_by_role") or {}).get("worker", 0),
            simulator_requests=(result.get("requests_by_role") or {}).get("user_simulator", 0),
            http_calls=sum(v["http_calls"] for v in costs.values()), reported_tokens=sum(v["total_tokens"] for v in costs.values()),
            known_cost_usd=str(sum((Decimal(v["reported_usd"]) for v in costs.values()), Decimal(0))),
            unknown_cost_requests=sum(v["unknown_count"] for v in costs.values()), first_worker_wire_sha256=result.get("first_worker_request_sha256"))
        table.append(cell)
        if own_writer and own_writer["status"] == "empty":
            empty_comparisons["assigned"] += 1
            own_hash = result.get("first_worker_request_sha256")
            other_hash = empty.get("first_worker_request_sha256")
            es = empty.get("official_success")
            same = own_hash == other_hash if own_hash and other_hash else None
            if same is not None:
                empty_comparisons["both_first_worker_wires_available"] += 1
                empty_comparisons["identical_first_worker_wire"] += same
            if type(success) is bool and type(es) is bool:
                empty_comparisons["both_graded"] += 1
                empty_comparisons["different_official_success"] += success != es
            empty_pairs.append(dict(task_id=task, repetition=rep, assigned_arm=arm, empty_authored_job=row["index"],
                                    empty_control_job=solves[task, rep, "empty_preload"]["index"], identical_first_wire=same,
                                    authored_success=success, matched_empty_success=es))
    return table, dict(counts=dict(empty_comparisons), pairs=empty_pairs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("writing_review", type=Path)
    parser.add_argument("output", type=Path, help="New directory for the CSV and the paired empty-bank evidence")
    args = parser.parse_args()
    table, empty = cells(args.run, read_json(args.writing_review.read_bytes()))
    if args.output.exists():
        raise ContractError("Export requires a new directory")
    with TemporaryDirectory(dir=args.output.parent) as tmp:
        staged = Path(tmp) / "cells"
        staged.mkdir()
        with (staged / "second-pass.csv").open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows(table)
        write_json(staged / "empty-forms.json", empty)
        staged.rename(args.output)


if __name__ == "__main__":
    main()
