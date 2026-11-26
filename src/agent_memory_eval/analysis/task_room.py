"""Find where memory can still change official outcomes, and size the paired comparison that could show it."""

import argparse
from collections import Counter, defaultdict
from fractions import Fraction
from math import ceil, exp, lgamma, log
from pathlib import Path

from agent_memory_eval.adapters.tau_banking_results import task_customers
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
        require(isinstance(item.get("task"), str) and success in (True, False, None), "Unexpected banking result row")
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


def compare(spec, rows, rooms):
    """Pair two registered follow-up arms slot by slot and count where their official outcomes split."""

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
    by_task, unopened = defaultdict(Counter), Counter()
    for key in sorted(left, key=str):
        a, b = left[key], right[key]
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
    """Pairs needed when the two arms succeed independently at the stated rates."""
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


def task_room(workspace, plan, domain):
    require(plan.get("format") == PLAN, "Expected a task-room plan")
    ids = [s["id"] for s in plan["studies"]]
    require(ids and len(ids) == len(set(ids)), "Studies need distinct identifiers")
    rows = []
    for study in plan["studies"]:
        rows += result_rows(study["id"], read_json(asset_text(workspace, study["result"]).encode()))
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--domain", required=True, type=Path, help="tau2 banking_knowledge domain directory")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, help="Publish the result to a new file instead of printing it")
    args = parser.parse_args()
    try:
        value = task_room(args.workspace.resolve(), read_json(args.plan.read_bytes()), args.domain)
        if args.output:
            publish_json(args.output, value)
            return
    except (OSError, ValueError, KeyError, TypeError, ContractError) as exc:
        parser.error(str(exc))
    print(canonical(value).decode())


if __name__ == "__main__":
    main()
