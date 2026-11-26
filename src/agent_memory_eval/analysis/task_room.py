"""Find where memory can still change official outcomes, and size the paired comparison that could show it."""

import argparse
from collections import Counter, defaultdict
from fractions import Fraction
from math import ceil, exp, fsum, lgamma, log
from pathlib import Path

from agent_memory_eval.adapters.tau_banking_results import task_customers
from agent_memory_eval.analysis.clustered_outcomes import clustered_contrast
from agent_memory_eval.analysis.paired_trials import sign_test
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.recording.assets import asset_text, publish_json


PLAN = "agent-memory-task-room-plan/v1"
FORMAT = "agent-memory-task-room/v1"
GRADES = {True: "success", False: "failure", None: "ungraded"}
ROOMS = ("mixed", "floor", "ceiling", "unobserved", "not_run")
CELLS = {
    (True, True): "both_success",
    (True, False): "reference_only",
    (False, True): "candidate_only",
    (False, False): "both_failure",
}


def result_rows(study, result):
    """Read the first-pass and follow-up rows of one registered banking result without regrading them."""

    def row(phase, item, condition):
        success = item.get("success")
        require(isinstance(item.get("task"), str) and (success is None or type(success) is bool), "Unexpected banking result row")
        require((item.get("status") == "graded") == (success is not None), "Grade and grading status disagree")
        return dict(
            study=study,
            phase=phase,
            condition=condition,
            task=item["task"],
            slot=item.get("arm") or item.get("form"),
            writer_draw=item.get("writer_draw"),
            reader_repeat=item.get("reader_repeat", item.get("repeat")),
            run=item.get("run"),
            status=item["status"],
            success=success,
            read_calls=item.get("read_calls"),
        )

    sources = result.get("sources")
    require(isinstance(sources, (dict, list)) and sources, "Result lacks its first-pass sources")
    rows = [row("source", s, "no_memory") for s in (sources.values() if isinstance(sources, dict) else sources)]
    trials = result["trials"] if "trials" in result else result.get("readers")
    if isinstance(trials, dict):
        rows += [row("solve", t, condition) for condition, items in sorted(trials.items()) for t in items]
    else:
        require(isinstance(trials, list) and trials, "Result lacks its follow-up rows")
        rows += [row("solve", t, "registered") for t in trials]
    return rows


def distinct_runs(rows):
    """Count a recorded run once when several studies reuse it, for example as a shared first pass."""
    seen, kept = {}, []
    for r in rows:
        if r["run"] is not None:
            first = seen.setdefault(r["run"], r)
            if first is not r:
                require(
                    (first["task"], first["phase"], first["success"]) == (r["task"], r["phase"], r["success"]),
                    "A reused run has a different task, phase or grade",
                )
                continue
        kept.append(r)
    return kept


def room(counts):
    if not counts["success"] + counts["failure"]:
        return "unobserved"
    if counts["success"] and counts["failure"]:
        return "mixed"
    return "ceiling" if counts["success"] else "floor"


def task_table(rows, identities):
    """One row per benchmark task: its independent unit, graded outcomes by phase and whether memory has room."""
    seen = defaultdict(lambda: dict(source=Counter(), solve=Counter(), studies=set()))
    for r in rows:
        entry = seen[r["task"]]
        entry[r["phase"]][GRADES[r["success"]]] += 1
        entry["studies"].add(r["study"])
    table = []
    for task in sorted(identities):
        found = sorted(identities[task])
        # A task without one registered customer is its own unit. Personas outside the database are not merged.
        record = dict(task=task, unit=found[0] if len(found) == 1 else "task:" + task, identified=len(found) == 1)
        entry = seen.get(task)
        if entry is None:
            record.update(room="not_run", source=None, solve=None, studies=[])
        else:
            record.update(
                room=room(entry["source"] + entry["solve"]),
                source={g: entry["source"][g] for g in GRADES.values()},
                solve={g: entry["solve"][g] for g in GRADES.values()},
                studies=sorted(entry["studies"]),
            )
        table.append(record)
    return table


def pair_key(r):
    return r["task"], r["slot"], r["writer_draw"], r["reader_repeat"]


def unopened_on_demand(*rows):
    """Neither on-demand reader opened its memory, so the arms differed only in content that nobody read."""
    return all(r["read_calls"] == 0 and str(r["slot"]).endswith("on_demand") for r in rows)


def paired_slots(spec, rows):
    def side(selector):
        chosen = [
            r for r in rows
            if r["phase"] == "solve" and r["study"] == selector["study"] and r["condition"] == selector["condition"]
        ]
        require(chosen, "Comparison side has no rows: " + canonical(selector).decode())
        indexed = {pair_key(r): r for r in chosen}
        require(len(indexed) == len(chosen), "Ambiguous comparison pairing")
        return indexed

    left, right = side(spec["reference"]), side(spec["candidate"])
    require(left.keys() == right.keys(), "Comparison sides cover different slots")
    return [(key, left[key], right[key]) for key in sorted(left, key=str)]


def compare(spec, rows, rooms):
    """Pair two registered follow-up arms slot by slot and count where their official outcomes split."""
    by_task, unopened = defaultdict(Counter), Counter()
    for key, a, b in paired_slots(spec, rows):
        if a["success"] is None or b["success"] is None:
            by_task[key[0]]["ungraded"] += 1
            continue
        cell = CELLS[a["success"], b["success"]]
        by_task[key[0]][cell] += 1
        if rooms[key[0]] == "mixed" and unopened_on_demand(a, b):
            unopened[cell] += 1
    totals = sum(by_task.values(), Counter())
    mixed = sum((c for t, c in by_task.items() if rooms[t] == "mixed"), Counter())
    return dict(
        id=spec["id"],
        reference=spec["reference"],
        candidate=spec["candidate"],
        by_task={t: {k: c[k] for k in (*CELLS.values(), "ungraded")} for t, c in sorted(by_task.items())},
        totals={k: totals[k] for k in (*CELLS.values(), "ungraded")},
        mixed_tasks={k: mixed[k] for k in CELLS.values()},
        sign_test_p=sign_test(totals["candidate_only"], totals["reference_only"]),
        unopened_on_demand_in_mixed_tasks={k: unopened[k] for k in CELLS.values()},
    )


def _pmf(n, k, p):
    return exp(lgamma(n + 1) - lgamma(k + 1) - lgamma(n - k + 1) + k * log(p) + (n - k) * log(1 - p))


def _power(n, critical, share):
    # Reject when either arm wins at most `critical` discordant pairs; the two tails never overlap.
    return sum(_pmf(n, k, share) + _pmf(n, n - k, share) for k in range(critical + 1)) if critical >= 0 else 0.0


def _critical(n, alpha, start=-1):
    critical = start
    while 2 * (critical + 1) < n and sign_test(critical + 1, n - critical - 1) <= alpha:
        critical += 1
    return critical


def sign_test_power(n, share, alpha=0.05):
    """Exact power of the two-sided sign test when the candidate wins this share of n discordant pairs."""
    require(type(n) is int and n >= 1 and 0 < share < 1 and 0 < alpha < 1, "Invalid sizing input")
    return _power(n, _critical(n, alpha), share)


def discordant_needed(share, alpha=0.05, power=0.8, limit=2000):
    """Fewest discordant pairs at which the exact sign test reaches the power, or None within the limit."""
    require(0 < share < 1 and 0 < alpha < 1 and 0 < power < 1, "Invalid sizing input")
    critical = -1
    for n in range(1, limit + 1):
        critical = _critical(n, alpha, critical)  # The critical count never decreases as n grows.
        if _power(n, critical, share) >= power:
            return n
    return None


def scenario(reference_rate, candidate_rate, alpha, power):
    """Historical v1 estimate using the expected yield of discordant pairs."""
    p0, p1 = Fraction(str(reference_rate)), Fraction(str(candidate_rate))
    require(0 < p0 < 1 and 0 < p1 < 1 and p0 != p1, "Scenario rates must differ and lie strictly inside (0, 1)")
    discordance = p0 * (1 - p1) + p1 * (1 - p0)
    share = p1 * (1 - p0) / discordance
    needed = discordant_needed(float(share), alpha, power)
    return dict(
        reference_rate=reference_rate,
        candidate_rate=candidate_rate,
        discordance=float(discordance),
        candidate_share=float(share),
        discordant_pairs=needed,
        pairs=ceil(needed / discordance) if needed else None,
    )


def domain_digest(domain, tasks):
    files = {t: digest((domain / "tasks" / (t + ".json")).read_bytes()) for t in tasks}
    return digest(canonical(dict(db=digest((domain / "db.json").read_bytes()), tasks=files)))


def study_rows(workspace, plan):
    require(plan.get("format") == PLAN, "Expected a task-room plan")
    ids = [s["id"] for s in plan["studies"]]
    require(ids and len(ids) == len(set(ids)), "Studies need distinct identifiers")
    return [r for study in plan["studies"]
            for r in result_rows(study["id"], read_json(asset_text(workspace, study["result"]).encode()))]


def task_room(workspace, plan, domain):
    rows = study_rows(workspace, plan)
    tasks = sorted(p.stem for p in (domain / "tasks").glob("*.json"))
    require({r["task"] for r in rows} <= set(tasks), "A result names a task outside the benchmark")
    kept = distinct_runs(rows)
    table = task_table(kept, task_customers(domain, tasks))
    rooms = {t["task"]: t["room"] for t in table}
    comparisons = [compare(spec, rows, rooms) for spec in plan.get("comparisons", [])]
    unopened = sum((Counter(c["unopened_on_demand_in_mixed_tasks"]) for c in comparisons), Counter())
    opened_pairs = sum(unopened.values())
    split = unopened["reference_only"] + unopened["candidate_only"]
    run_units = {t["unit"] for t in table if t["room"] != "not_run"}
    idle_units = {t["unit"] for t in table if t["room"] == "not_run"}
    sizing = plan["sizing"]
    return dict(
        format=FORMAT,
        plan_sha256=digest(canonical(plan)),
        inputs=[dict(study=s["id"], result=s["result"]) for s in plan["studies"]],
        domain=dict(tasks=len(tasks), sha256=domain_digest(domain, tasks)),
        summary=dict(
            rows=len(rows),
            distinct_rows=len(kept),
            reused_runs=len(rows) - len(kept),
            by_room={k: sum(t["room"] == k for t in table) for k in ROOMS},
            mixed_tasks=[t["task"] for t in table if t["room"] == "mixed"],
            mixed_units=sorted({t["unit"] for t in table if t["room"] == "mixed"}),
            identified_customers=len({t["unit"] for t in table if t["identified"]}),
            unresolved_tasks=sum(not t["identified"] for t in table),
            units_only_in_tasks_not_run=len(idle_units - run_units),
        ),
        tasks=table,
        comparisons=comparisons,
        noise=dict(
            definition="Pairs in mixed tasks where neither on-demand reader opened its memory",
            pairs=opened_pairs,
            discordant=split,
            rate=split / opened_pairs if opened_pairs else None,
        ),
        sizing=dict(
            test="exact two-sided sign test on discordant pairs; pairs assumed independent",
            alpha=sizing["alpha"],
            power=sizing["power"],
            scenarios=[scenario(p0, p1, sizing["alpha"], sizing["power"]) for p0, p1 in sizing["scenarios"]],
        ),
    )


def fixed_pairs(discordance, share, alpha, target, limit=2000):
    """Find the first fixed enrolment reaching target power, averaging over the random discordant count."""
    require(
        all(0 < v < 1 for v in (discordance, share, alpha, target)) and type(limit) is int and limit > 0,
        "Invalid fixed-pair sizing inputs",
    )
    weights, conditional, critical = [1.0], [0.0], -1
    for n in range(1, limit + 1):
        critical = _critical(n, alpha, critical)
        conditional.append(_power(n, critical, share))
        weights = [
            (1 - discordance) * same + discordance * one_more
            for same, one_more in zip([*weights, 0.0], [0.0, *weights])
        ]
        power = fsum(w * c for w, c in zip(weights, conditional))
        if power >= target:
            return dict(pairs=n, achieved_power=power)
    return dict(pairs=None, achieved_power=None)


def reviewed_counts(workspace, plan, domain, archive):
    """Audit a frozen result using all eligible pairs and grouped outcome contrasts."""
    archived_bytes = archive.read_bytes()
    original = read_json(archived_bytes)
    require(
        original == task_room(workspace, plan, domain),
        "The published result, selected inputs or domain changed",
    )
    rows = study_rows(workspace, plan)
    sources = {(r["study"], r["task"]): r["run"] for r in rows if r["phase"] == "source"}
    tasks = {t["task"]: t for t in original["tasks"]}
    used_customers = {t["unit"] for t in tasks.values() if t["identified"] and t["room"] != "not_run"}
    unused = [t for t in tasks.values() if t["room"] == "not_run"]
    all_unopened, mixed_unopened = {}, {}
    comparisons = []
    for spec, previously in zip(plan["comparisons"], original["comparisons"], strict=True):
        contrasts = []
        for (task, arm, draw, repeat), a, b in paired_slots(spec, rows):
            if a["study"] != b["study"]:
                source = sources.get((a["study"], task))
                require(
                    source and source == sources.get((b["study"], task)),
                    "Compared studies must share the first pass",
                )
            ref, candidate = a["success"], b["success"]
            contrasts.append((tasks[task]["unit"], task, arm, (draw, repeat), ref, candidate))
            if ref is None or candidate is None or not unopened_on_demand(a, b):
                continue
            require(
                a["run"] and b["run"] and a["run"] != b["run"], "Unopened comparison needs two observed runs"
            )
            key = tuple(sorted((a["run"], b["run"])))
            all_unopened[key] = ref != candidate
            if tasks[task]["room"] == "mixed":
                mixed_unopened[key] = ref != candidate
        cluster = clustered_contrast(contrasts, cluster_unit="identified_customer_or_unresolved_task")
        comparisons.append(
            dict(
                id=spec["id"],
                assigned=cluster["assigned_pairs"],
                graded=cluster["observed_pairs"],
                allocation_sign_p=previously["sign_test_p"],
                observed_units=cluster["observed_clusters"],
                positive_units=cluster["positive_clusters"],
                negative_units=cluster["negative_clusters"],
                tied_units=cluster["tied_clusters"],
                unit_sign_p=cluster["two_sided_cluster_sign_p"],
            )
        )
    sizing = original["sizing"]
    scenarios = []
    for case in sizing["scenarios"]:
        scenarios.append(
            dict(
                reference_rate=case["reference_rate"],
                candidate_rate=case["candidate_rate"],
                discordance=case["discordance"],
                **fixed_pairs(case["discordance"], case["candidate_share"], sizing["alpha"], sizing["power"]),
            )
        )
    return dict(
        format="agent-memory-task-room-review/v1",
        report_sha256=digest(archived_bytes),
        plan_sha256=original["plan_sha256"],
        domain_sha256=original["domain"]["sha256"],
        unused=dict(
            tasks=len(unused),
            identified_new_customers=len({t["unit"] for t in unused if t["identified"]} - used_customers),
            unresolved_tasks=sum(not t["identified"] for t in unused),
        ),
        unopened={
            scope: dict(pairs=len(vals), discordant=sum(vals.values()))
            for scope, vals in (("all_graded", all_unopened), ("outcome_selected_mixed", mixed_unopened))
        },
        comparisons=comparisons,
        alpha=sizing["alpha"],
        target_power=sizing["power"],
        scenarios=scenarios,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--domain", required=True, type=Path, help="tau2 banking_knowledge domain directory")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, help="Publish the result to a new file instead of printing it")
    parser.add_argument(
        "--review-of",
        type=Path,
        help="Verify a published task-room result and report corrected denominators and fixed-pair power",
    )
    args = parser.parse_args()
    try:
        inputs = args.workspace.resolve(), read_json(args.plan.read_bytes()), args.domain
        value = reviewed_counts(*inputs, args.review_of) if args.review_of else task_room(*inputs)
        if args.output:
            publish_json(args.output, value)
            return
    except (OSError, ValueError, KeyError, TypeError, ContractError) as exc:
        parser.error(str(exc))
    print(canonical(value).decode())


if __name__ == "__main__":
    main()
