"""Audit fresh retail runs and quantify same-bank repeatability and paired customer-level effects."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from multiprocessing import get_context
from pathlib import Path
from random import Random
from statistics import mean, quantiles
from unittest.mock import patch

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path

if __package__:
    from . import run_retail_sample_expansion as expansion
    from .analyze_full_retail import audit_task, require
    from .analyze_recheck_rounds import status
    from .analyze_retail_full_factors import business_events, transfer_position
    from .replay_full_retail_fields import reference_state, state_hashes
else:
    import run_retail_sample_expansion as expansion
    from analyze_full_retail import audit_task, require
    from analyze_recheck_rounds import status
    from analyze_retail_full_factors import business_events, transfer_position
    from replay_full_retail_fields import reference_state, state_hashes

retail = expansion.retail
HISTORICAL_FACTORS = "51de543873535f09cdcb01ea6de93d4c8c0e8912ac68ed2a0867e7cd2ccc6063"
ROUNDS = ("solve1", "solve2", "solve3")


def collect_task(args):
    workspace, _, _, _, task, _, _ = args
    with (
        patch(
            "socket.socket.connect", side_effect=AssertionError("offline replay")
        ) as network,
        patch(
            "socket.socket.connect_ex", side_effect=AssertionError("offline replay")
        ) as connections,
    ):
        result = audit_task(args)
        from tau2.data_model.language import LanguageTaskInfo
        from tau2.multilingual.runtime import language_environment_kwargs
        from tau2.registry import registry
        from tau2.runner.presets import reload_recorded_call

        references = {}
        for cell in result["cells"]:
            if not cell.get("report"):
                continue
            report = expansion.verified(
                safe_path(workspace, cell["report"]), cell["report_sha256"]
            )
            updates, transfers = business_events(report["messages"])
            cell["transfer_position"] = transfer_position(updates, transfers)
            cell["first_transfer"] = transfers[0] if transfers else None
            ctx = report["language_context"]
            key = digest(canonical(ctx))
            if key not in references:
                require(task["initial_state"] is None, "Unexpected task initialization")
                opts = (
                    language_environment_kwargs(
                        reload_recorded_call(
                            LanguageTaskInfo.model_validate(ctx), "retail"
                        )
                    )
                    or {}
                )
                env = registry.get_env_constructor("retail")(**opts)
                initial = state_hashes(env)
                target, errors = reference_state(
                    env, task["evaluation_criteria"]["actions"]
                )
                references[key] = {
                    "initial": initial,
                    "target": target,
                    "changed": initial != target,
                    "errors": errors,
                }
            cell["task_context_sha256"] = key
            cell["target_changes_db"] = references[key]["changed"]
        require(
            network.call_count == connections.call_count == 0,
            "Offline replay attempted network access",
        )
    result["references"] = references
    return result


def reconcile(root, cfg, rows, record):
    registration = retail.read(root / "registration.json")
    completion = retail.read(root / "completion.json")
    require(
        registration["preflight"] == record
        and registration["endpoint_terms"] == cfg["expected_endpoint_terms"]
        and completion["registration_sha256"]
        == digest((root / "registration.json").read_bytes()),
        "Registration or endpoints differ from frozen plan",
    )
    grants = {}
    total = Decimal(0)
    size = cfg["max_concurrent"]
    for stage in cfg["stages"]:
        ordered = retail.stage_order(cfg, rows, stage)
        for start in range(0, len(ordered), size):
            wave = retail.read(root / f"{stage}-wave-{start // size + 1}.json")
            tasks = [r["task"] for r in ordered[start : start + size]]
            grant = Decimal(wave["per_shard_grant"])
            require(
                wave["tasks"] == tasks
                and Decimal(wave["prior_covered"]) == total
                and total + grant * len(tasks) <= Decimal(str(cfg["total_usd_limit"])),
                "Incorrect concurrent cost reservation",
            )
            for task in tasks:
                name = f"task-{task}/{stage}"
                entry = completion["stages"][name]
                _, ctl, _ = retail.sealed_rows(root / name)
                require(
                    Decimal(str(ctl["profile"]["guards"]["max_usd"])) == grant
                    and retail.accounting(root / name, grant) == entry,
                    "Stage cost or seal differs from schedule",
                )
                total += Decimal(entry["spent"])
                grants[name] = entry
    require(
        total == Decimal(completion["total_covered_usd"])
        and grants == completion["stages"],
        "Incomplete cost reconciliation",
    )
    return registration, completion


def bootstrap_customer_interval(values):
    """Percentile interval for the mean of equally weighted observed customers, conditional on banks."""
    if len(values) < 2:
        return None
    rng = Random(20261119)
    estimates = [mean(rng.choices(values, k=len(values))) for _ in range(10000)]
    percentiles = quantiles(estimates, n=40, method="inclusive")
    return [round(percentiles[0], 6), round(percentiles[-1], 6)]


def paired_effect(pairs, interval=False):
    totals = Counter(p["state"] for p in pairs)
    by_task = defaultdict(list)
    by_customer = defaultdict(list)
    for pair in pairs:
        if pair["state"] != "missing":
            by_task[(pair["customer"], pair["task"])].append(
                int(pair["state"] == "gain") - int(pair["state"] == "loss")
            )
    for (customer, _), observations in by_task.items():
        by_customer[customer].append(mean(observations))
    values = [mean(scores) for _, scores in sorted(by_customer.items())]
    return {
        "counts": dict(totals),
        "scored_tasks": len(by_task),
        "scored_customers": len(values),
        "task_difference": mean(mean(v) for v in by_task.values()) if by_task else None,
        "customer_difference": mean(values) if values else None,
        "customer_interval": bootstrap_customer_interval(values) if interval else None,
    }


def outcome_counts(cells):
    return dict(
        Counter(
            {True: "success", False: "failure", None: "missing"}[c["official"]]
            for c in cells
        )
    )


def repeated_outcomes(cells):
    values = [c["official"] for c in cells]
    if None in values:
        return "incomplete"
    if all(values):
        return "all_success"
    return "mixed" if any(values) else "all_failure"


def join_trials(rows, cells, cfg):
    index = {(c["task"], c["stage"], c["arm"]): c for c in cells}
    require(len(index) == len(cells), "Duplicate task, round and arm")
    forms = [f + "_original" for f in cfg["forms"]]
    pairs = []
    openings = Counter()
    for row in rows:
        tid = row["task"]
        first = index[tid, "source", "no_memory"]
        simulators = set()
        for arm in forms + cfg["controls"]:
            bank = index.get((tid, "write", arm))
            workers = set()
            for round_name in ROUNDS:
                c = index[tid, round_name, arm]
                hashes = c.get("opening", {})
                if hashes.get("simulator_request"):
                    simulators.add(hashes["simulator_request"])
                    openings["simulator_requests"] += 1
                if hashes.get("worker_input_without_opening"):
                    workers.add(hashes["worker_input_without_opening"])
                    openings["worker_requests"] += 1
                if c.get("snapshot") is not None or c.get("grading") == "graded":
                    require(
                        c["writer_run"] == (bank["run"] if bank else None)
                        and c["snapshot"] == (bank.get("snapshot") if bank else None),
                        "Repeated worker received different bank",
                    )
                if arm in forms:
                    for control in cfg["controls"]:
                        base = index[tid, round_name, control]
                        pairs.append(
                            {
                                "task": tid,
                                "customer": row["customer"],
                                "cohort": row["cohort"],
                                "stage": round_name,
                                "arm": arm,
                                "control": control,
                                "writer": bank["run"],
                                "source": first["run"],
                                "source_score": first["official"],
                                "memory_run": c["run"],
                                "control_run": base["run"],
                                "source_transition": status(
                                    c["official"], first["official"]
                                ),
                                "state": status(c["official"], base["official"]),
                            }
                        )
            require(
                len(workers) <= 1,
                "Initial solver settings, policy or snapshot differs between repeats",
            )
            if workers:
                openings["verified_task_arms"] += 1
        require(
            len(simulators) <= 1,
            "First simulator input differs within a task's second-pass conditions or repeats",
        )
        if simulators:
            openings["verified_tasks"] += 1
    return index, pairs, dict(openings)


def group_summary(rows, index, pairs, cfg, rounds, intervals):
    tids = {r["task"] for r in rows}
    sources = [index[t, "source", "no_memory"] for t in sorted(tids)]
    arms = {}
    for arm in [f + "_original" for f in cfg["forms"]] + cfg["controls"]:
        cases = [index[t, r, arm] for t in sorted(tids) for r in rounds]
        selected = [
            p
            for p in pairs
            if p["task"] in tids and p["arm"] == arm and p["stage"] in rounds
        ]
        cohorts = (
            {
                control: paired_effect(
                    [p for p in selected if p["control"] == control],
                    intervals and control == "no_memory",
                )
                for control in cfg["controls"]
            }
            if selected
            else {}
        )
        handoffs = Counter()
        handoff_scores = Counter()
        for c in cases:
            if c.get("target_changes_db") and c.get("transfer_position"):
                kind = c["transfer_position"]
                handoffs[kind] += 1
                handoff_scores[kind] += int(c["official"])
        arms[arm] = {
            "official": outcome_counts(cases),
            "first_to_second": dict(
                Counter(
                    status(
                        c["official"],
                        index[c["task"], "source", "no_memory"]["official"],
                    )
                    for c in cases
                )
            ),
            "vs_control": cohorts,
            "same_bank_or_control": dict(
                Counter(
                    repeated_outcomes([index[t, r, arm] for r in rounds])
                    for t in sorted(tids)
                )
            ),
            "handoffs_on_changed_targets": dict(handoffs),
            "handoff_successes_on_changed_targets": dict(handoff_scores),
        }
    return {
        "tasks": len(tids),
        "customers": len({r["customer"] for r in rows}),
        "source": outcome_counts(sources),
        "rounds": list(rounds),
        "arms": arms,
    }


def direction_repeats(pairs):
    groups = defaultdict(dict)
    for p in pairs:
        groups[p["cohort"], p["arm"], p["control"], p["task"]][p["stage"]] = p["state"]
    result = defaultdict(Counter)
    for (cohort, arm, control, _), rounds in groups.items():
        first = rounds["solve1"]
        if first in {"gain", "loss"}:
            later = [rounds[r] for r in ROUNDS[1:]]
            if "missing" in later:
                count = "incomplete"
            else:
                count = str(later.count(first)) + "_of_2"
            result[f"{cohort}/{arm}/{control}/{first}"][count] += 1
    return dict(result)


def billing(cells, completion):
    roles = defaultdict(
        lambda: {"http": 0, "tokens": 0, "unknown": 0, "reported": Decimal(0)}
    )
    stage_usd = defaultdict(Decimal)
    for cell in cells:
        for role, item in cell.get("costs", {}).items():
            entry = roles[role]
            entry["http"] += item["http_calls"]
            entry["tokens"] += item["total_tokens"]
            entry["unknown"] += item["unknown_count"]
            entry["reported"] += Decimal(item["reported_usd"])
            stage_usd[cell["stage"]] += Decimal(item["reported_usd"])
    total = sum(v["reported"] for v in roles.values())
    http = sum(v["http"] for v in roles.values())
    stages = completion["stages"].values()
    require(
        sum(Decimal(s["reported"]) for s in stages) == total
        and sum(s["http"] for s in stages) == http,
        "Requests or billing disagree with stage ledgers",
    )
    return {
        "reported": str(total),
        "held": str(Decimal(completion["total_covered_usd"]) - total),
        "http": http,
        "by_stage": {k: str(v) for k, v in stage_usd.items()},
        "by_role": {k: dict(v, reported=str(v["reported"])) for k, v in roles.items()},
    }


def historical_inputs(workspace):
    spec = retail.read(ROOT / expansion.PROFILE)
    prior = read_json(asset_text(ROOT, spec["parent_cells"]).encode())
    factors = expansion.verified(
        workspace / "artifacts/analyses/20261119-retail-full-decision-factors.json",
        HISTORICAL_FACTORS,
    )
    require(
        factors["input_sha256"]["cells"] == spec["parent_cells"]["sha256"],
        "Past openings and action sequences belong to another study",
    )
    extra = {e["run"]: e for e in factors["episodes"]}
    for c in prior["cells"]:
        if c["stage"] != "write":
            e = extra[c["run"]]
            require(
                e["report_sha256"] == c["report_sha256"],
                "Historical native trajectory mismatch",
            )
            c.update(
                opening=e["opening"],
                target_changes_db=e["target_changes_db"],
                transfer_position=e["transfer_position"],
                first_transfer=next(iter(e["transfers"]), None),
            )
    return prior, spec


def build_report(cfg, rows, historical, newly_audited):
    cells = historical["cells"] + newly_audited
    index, pairs, openings = join_trials(rows, cells, cfg)
    require(
        len(pairs)
        == len(rows) * len(ROUNDS) * len(cfg["forms"]) * len(cfg["controls"]),
        "Incomplete paired allocation",
    )
    groups = {}
    for cohort, subset in [
        ("repeat_test", [r for r in rows if r["cohort"] == "repeat_test"]),
        ("new_train", [r for r in rows if r["cohort"] == "new_train"]),
        ("combined", rows),
    ]:
        groups[cohort] = group_summary(
            subset, index, pairs, cfg, ROUNDS, cohort != "combined"
        )
        groups[cohort + "/rounds2and3"] = group_summary(
            subset, index, pairs, cfg, ROUNDS[1:], False
        )
        if cohort != "combined":
            for stage in ROUNDS:
                groups[cohort + "/" + stage] = group_summary(
                    subset, index, pairs, cfg, (stage,), False
                )
    source = [c for c in cells if c["stage"] == "source"]
    writers = [c for c in cells if c["stage"] == "write"]
    all_solvers = [c for c in cells if c["stage"] in ROUNDS]
    return {
        "source": outcome_counts(source),
        "distinct_source_failure_customers": len(
            {c["customer"] for c in source if c["official"] is False}
        ),
        "writers": dict(Counter(c["status"] for c in writers)),
        "second_pass": outcome_counts(all_solvers),
        "openings": openings,
        "groups": groups,
        "direction_repeats": direction_repeats(pairs),
    }, pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    require(
        not args.output.exists() and not args.summary.exists(),
        "Use new destinations for analysis",
    )
    workspace = args.workspace.resolve()
    cfg, base, _, rows, tasks = expansion.prepare(workspace)
    root = workspace / "runs" / cfg["run_directory"]
    record = retail.preflight(cfg, base, rows)
    require(
        record
        == retail.read(
            ROOT / "artifacts/analyses/20261119-retail-sample-expansion-preflight.json"
        ),
        "Registration is not the published allocation",
    )
    reg, end = reconcile(root, cfg, rows, record)
    prior, spec = historical_inputs(workspace)
    args_by_task = [
        (workspace, cfg, base, row, tasks[row["task"]], reg, end) for row in rows
    ]
    with ProcessPoolExecutor(max_workers=5, mp_context=get_context("spawn")) as pool:
        results = list(pool.map(collect_task, args_by_task))
    cells = [c for result in results for c in result["cells"]]
    ids = [i for result in results for i in result["generation_ids"]]
    sessions = [c["session"] for c in cells if c.get("session")]
    run_ids = [c["run"] for c in cells]
    for fresh, old in [
        (ids, prior["generation_ids"]),
        (sessions, [c["session"] for c in prior["cells"] if c.get("session")]),
        (run_ids, [c["run"] for c in prior["cells"]]),
    ]:
        require(
            len(fresh) == len(set(fresh)) and set(fresh).isdisjoint(old),
            "Duplicate execution, session or billed generation",
        )
    require(
        len(cells)
        == sum(len(jobs) for r in record["rows"] for jobs in r["stages"].values()),
        "Missing assigned cells",
    )
    summary, pairs = build_report(cfg, rows, prior, cells)
    costs = billing(cells, end)
    raw = {
        "study": cfg["name"],
        "directory": cfg["run_directory"],
        "implementation": reg["implementation"],
        "registration_sha256": end["registration_sha256"],
        "completion_sha256": digest((root / "completion.json").read_bytes()),
        "historical_sha256": spec["parent_cells"]["sha256"],
        "rows": rows,
        "reference_databases": {r["task"]: r["references"] for r in results},
        "new_cells": cells,
        "pairs": pairs,
        "new_generation_ids": ids,
    }
    write_json(args.output, raw)
    summary.update(
        study=cfg["name"],
        detail_sha256=digest(args.output.read_bytes()),
        fresh_allocations=len(cells),
        fresh_allocations_by_status={
            stage: dict(Counter(c["status"] for c in cells if c["stage"] == stage))
            for stage in cfg["stages"]
        },
        new_billing=costs,
        new_sessions=len(sessions),
        new_normal_responses=len(ids),
        fresh_regraded=sum("grade" in c for c in cells),
        replayed_judge_answers=sum(
            c.get("grade", {}).get("replayed_judge_answers", 0) for c in cells
        ),
        analysis_model_calls=0,
    )
    write_json(args.summary, summary)
    print(
        f"Audited {len(cells)} fresh allocations and {len(pairs)} matched comparisons across 66 tasks. HTTP {costs['http']}, reported USD {costs['reported']}, held USD {costs['held']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
