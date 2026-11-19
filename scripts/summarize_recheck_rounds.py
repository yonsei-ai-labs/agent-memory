"""Compare the two sealed rounds without treating repeated customer sessions as independent."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json

if __package__:
    from .analyze_recheck_rounds import status
    from .run_recheck_rounds import PROFILE, read, sealed_rows
else:
    from analyze_recheck_rounds import status
    from run_recheck_rounds import PROFILE, read, sealed_rows


def round_table(result, cfg):
    rows = result["cells"]
    by_slot = {(r["task"], r["stage"], r["arm"]): r for r in rows}
    writers = defaultdict(Counter)
    for w in rows:
        if w["stage"] == "write":
            g = writers[w["arm"]]
            g["allocated"] += 1
            if w["status"] in {"completed", "empty"}:
                g["available"] += 1
                g["empty"] += w["status"] == "empty"
                g["explicit_recheck"] += w["quote"] is not None
    tasks = sorted({r["task"] for r in rows}, key=int)
    arms = list(result["arms"])
    stability = defaultdict(Counter)
    for task in tasks:
        for arm in arms:
            first, second = (by_slot[task, w, arm] for w in ("solve1", "solve2"))
            if first.get("writer_run") != second.get("writer_run") and all(
                x.get("grading") == "graded" for x in [first, second]
            ):
                raise ContractError("Replicated solves used different writer banks")
            stability[arm][status(second["official"], first["official"])] += 1
    support = defaultdict(Counter)
    by_case = defaultdict(Counter)
    discordant = []
    for p in result["pairs"]:
        if not p["control"].endswith("_original"):
            continue
        by_case[p["task"]][p["outcome"]] += 1
        if p["outcome"] not in {"gain", "loss"}:
            continue
        ctrl1, ctrl2 = (by_slot[p["task"], p["wave"], arm] for arm in cfg["controls"])
        as_recheck = p["outcome"] == "gain"
        flags = [c["official"] for c in (ctrl1, ctrl2)]
        agreement = (
            "ungraded"
            if None in flags
            else "controls_split"
            if flags[0] != flags[1]
            else "agree_recheck"
            if flags[0] == as_recheck
            else "agree_original"
        )
        support[p["outcome"]][agreement] += 1
        discordant.append(
            dict(
                task=p["task"],
                family=p["family"],
                form=p["form"],
                wave=p["wave"],
                outcome=p["outcome"],
                writing_change=p["writing_change"],
                controls=agreement,
                original_run=p["control_run"],
                recheck_run=p["treatment_run"],
                no_memory_run=ctrl1["run"],
                empty_run=ctrl2["run"],
            )
        )
    actors = defaultdict(lambda: dict(http_calls=0, usd=Decimal(0)))
    for e in rows:
        for name, ledger in e.get("costs", {}).items():
            actors[name]["http_calls"] += ledger["http_calls"]
            actors[name]["usd"] += Decimal(ledger["reported_usd"])
    money = sum(Decimal(s["usd"]) for s in result["stages"].values())
    if money != sum(a["usd"] for a in actors.values()):
        raise ContractError("Stage and role totals disagree")
    for a in actors.values():
        a["usd"] = str(a["usd"])
    return dict(
        writing=dict(writers),
        writing_pairs=result["writer_gate"],
        official=result["arms"],
        paired=result["primary_counts"],
        by_task=dict(by_case),
        paired_discordant=discordant,
        comparison_with_controls=dict(support),
        repeats=dict(stability),
        empty_vs_no_memory=dict(Counter(x["outcome"] for x in result["empty_vs_no_memory"])),
        versus_first_pass=result["versus_first_pass"],
        sources=[
            dict(task=x["task"], family=x["family"], official=x["official"], run=x["run"])
            for x in rows
            if x["stage"] == "source"
        ],
        stages=result["stages"],
        by_role=dict(actors),
        usd=str(money),
        reserved_usd=str(sum(Decimal(s["reserved_usd"]) for s in result["stages"].values())),
        unpriced=sum(e.get("unpriced", 0) for e in rows),
        calls=sum(a["http_calls"] for a in actors.values()),
        distinct_replies=result["distinct_generation_ids"],
    )


def join(workspace, paths):
    cfg = read(ROOT / PROFILE)
    root = workspace / "runs" / cfg["name"]
    reg = read(root / "registration.json")
    combined = {}
    seen = {"customer": set(), "id": set(), "run": set(), "session": set()}
    timing = {}
    refs = {}
    for name, path in zip(("development", "confirmation"), paths, strict=True):
        data = read(path)
        if (
            data["round"] != name
            or data["protocol_sha256"] != reg["protocol_sha256"]
            or data["implementation"] != reg["implementation"]
            or data["round_completion_sha256"] != digest((root / name / "round-completion.json").read_bytes())
        ):
            raise ContractError("Round report or its registration differs")
        sets = dict(
            customer={r["customer"] for r in data["cells"]},
            id=set(data["generation_ids"]),
            run={r["run"] for r in data["cells"]},
            session={r["session"] for r in data["cells"] if "session" in r},
        )
        if len(sets["customer"]) != 6:
            raise ContractError("Expected six customers per registered round")
        for key, values in sets.items():
            if seen[key] & values:
                raise ContractError("Shared cross-round " + key)
            seen[key].update(values)
        combined[name] = round_table(data, cfg)
        refs[name] = dict(
            path=str(path.relative_to(workspace)) if path.is_relative_to(workspace) else path.name,
            sha256=digest(path.read_bytes()),
        )
        times = []
        for sub in (root / name).glob("task-*/*/completion.json"):
            seal, ctl, _ = sealed_rows(sub.parent)
            times.append((datetime.fromisoformat(ctl["started_at"]), datetime.fromisoformat(seal["ended_at"])))
        timing[name] = dict(start=min(t[0] for t in times).isoformat(), end=max(t[1] for t in times).isoformat())
    if timing["development"]["end"] >= timing["confirmation"]["start"]:
        raise ContractError("A confirmation session began before development was completed")
    money = sum(Decimal(r["usd"]) for r in combined.values())
    reserve = sum(Decimal(r["reserved_usd"]) for r in combined.values())
    if money + reserve > Decimal(str(cfg["total_usd_limit"])):
        raise ContractError("Combined spend exceeds the approved ceiling")
    return dict(
        study=cfg["name"],
        protocol_sha256=reg["protocol_sha256"],
        inputs=refs,
        chronology=timing,
        distinct_customers=len(seen["customer"]),
        distinct_sessions=len(seen["session"]),
        http_calls=sum(v["calls"] for v in combined.values()),
        distinct_generation_ids=len(seen["id"]),
        total_reported_usd=str(money),
        total_reserved_usd=str(reserve),
        rounds=combined,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", type=Path, default=ROOT)
    p.add_argument("--development", type=Path, required=True)
    p.add_argument("--confirmation", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        p.error("Use a new output outside the original runs")
    result = join(args.workspace.resolve(), [args.development.resolve(), args.confirmation.resolve()])
    write_json(args.output, result)
    print(result["http_calls"], result["total_reported_usd"])
