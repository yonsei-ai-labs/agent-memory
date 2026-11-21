"""Regrade the retail retest and compare writing, notice and combined effects on matched tasks."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from decimal import ROUND_DOWN, Decimal
from multiprocessing import get_context
from pathlib import Path

from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.recording.journal import safe_path
from scripts import run_retail_transfer_revision as study
from scripts.analyze_full_retail import read_request_data, require
from scripts.analyze_recheck_rounds import status
from scripts.analyze_retail_related_transfer import audit_target
from scripts.analyze_retail_sample_expansion import paired_effect

LEGACY, DOMAIN = "legacy-id-v1", "domain-auth-v2"


def terminal_disposition(items, final_text):
    marker = (final_text or "").strip()
    if items:
        return "stored_with_marker" if marker == "STORED" else "stored_other_reply"
    return "explicit_skip" if marker == "SKIP" else "other_empty_reply"


def audited_writers(workspace, root, cfg, base, row, reg, end):
    sid = row["task"]
    folder = root / ("task-" + sid) / "write"
    source = study.retail.recorded_source(safe_path(workspace, cfg["donors"][sid]["source_folder"]), sid)
    design, jobs = study.retail.full_design(cfg, base, row, "write", source)
    seal = end["stages"][f"task-{sid}/write"]
    banks = study.new_banks(workspace, root, cfg, sid)
    slots = [None] * len(jobs)
    if seal.get("status") == "not_started":
        require(not folder.exists() and Decimal(seal["spent"]) == 0, "Unexpected unfunded writer shard")
    else:
        _, ctl, slots = study.retail.sealed_rows(folder)
        models = ctl["model_catalog_entries"]
        require(
            ctl["design"] == design
            and ctl["implementation"] == reg["implementation"]
            and study.retail.accounting(folder, ctl["profile"]["guards"]["max_usd"]) == seal
            and study.retail.endpoint_terms(models)
            == {k: v for k, v in reg["endpoint_terms"].items() if k != "grader"},
            "Writer seal or provider changed",
        )
    cells, ids = [], []
    for job, slot in zip(jobs, slots, strict=True):
        arm = job["arm"]
        c = {
            "source": sid,
            "customer": row["customer"],
            "arm": arm,
            "run": None,
            "stored": None,
            "disposition": "not_started",
        }
        cells.append(c)
        if slot is None:
            continue
        require(all(slot[k] == v for k, v in job.items()), "Writer allocation differs")
        c.update(run=slot["run_id"], status=slot["status"])
        path = safe_path(folder, slot["directory"])
        if "result" not in slot:
            require(c["status"] == "not_started" and not path.exists(), "Unfinished writer")
            c["reason"] = slot["reason"]
            continue
        res = slot["result"]
        evidence, gen, state, scan, blobs, records = read_request_data(
            path, c["run"], res, workspace, models, None
        )
        ids.extend(gen)
        c.update(evidence)
        writing = state["memory_writes"][c["run"]]
        source_ref = blobs.get(blobs.get(writing["experience_ref"])["source_ref"])
        require(
            scan["starts"][c["run"]]["payload"]["source_run_ids"] == [source.source_run_id]
            and source_ref["source_log_sha256"] == source.source_log_sha256
            and source_ref["public_context_sha256"] == source.public_context_sha256
            and digest(writing["instruction"].encode()) == reg["preflight"]["writer_instruction_sha256"][arm],
            "Wrong writer source or instruction",
        )
        prefix = [
            {"role": "system", "content": writing["instruction"]},
            {"role": "user", "content": source.public_context},
            {"role": "user", "content": canonical({"kind": "evaluation_feedback", "level": "none"}).decode()},
        ]
        for i, (actor, wire, _, _) in enumerate(records):
            require(
                actor == "writer"
                and wire["messages"][:3] == prefix
                and (i > 0 or wire["messages"] == prefix)
                and wire["seed"] == cfg["writer_seed"]
                and wire["tool_choice"] == "auto"
                and [t["function"]["name"] for t in wire["tools"]] == ["memory_store"],
                "Wrong writer wire",
            )
        if c["status"] not in ("completed", "empty"):
            c.update(disposition="incomplete", error=res.get("error_code"))
            continue
        bank = read_form_bank(path, expected_log_sha256=banks[arm]["journal_sha256"])
        require(
            bank["public_context_sha256"] == source.public_context_sha256
            and bank["arm"] == arm
            and bank["run_id"] == c["run"]
            and bank["snapshot_id"] == banks[arm]["snapshot_id"]
            and bank["task_id"] == sid
            and records[-1][3] == "response",
            "Writer version differs",
        )
        final = records[-1][2]["choices"][0]["message"]
        require(not final.get("tool_calls"), "Writer did not terminate normally")
        text = final.get("content")
        c.update(
            disposition=terminal_disposition(bank["items"], text),
            terminal_reply=text,
            stored=bool(bank["items"]),
            items=bank["items"],
            snapshot=bank["snapshot_id"],
        )
    return cells, ids, banks


def writer_summary(rows):
    subsets = defaultdict(list)
    for r in rows:
        variant = r["arm"].rsplit("_", 1)[1]
        subsets[variant].append(r)
        subsets[r["arm"]].append(r)
    by_arm = {
        k: {
            "assigned": len(subset),
            "normal": sum(r["stored"] is not None for r in subset),
            "stored": sum(r["stored"] is True for r in subset),
            "dispositions": dict(Counter(r["disposition"] for r in subset)),
        }
        for k, subset in subsets.items()
    }
    lookup = {(r["source"], r["arm"]): r for r in rows}
    pairs = []
    for r in subsets["explicit"]:
        old = lookup[r["source"], r["arm"].removesuffix("_explicit") + "_original"]
        pairs.append(
            {
                "task": r["source"],
                "customer": r["customer"],
                "arm": r["arm"],
                "state": status(r["stored"], old["stored"]),
            }
        )
    return {"by_arm": by_arm, "paired_storage": paired_effect(pairs, interval=True), "pairs": pairs}


def solve_summary(rows, plans):
    index = {(c["task"], c["source"], c["arm"], c["stage"]): c for c in rows}
    require(len(index) == len(rows), "Duplicated solve allocation")
    groups, paired, by_arm = {}, defaultdict(list), defaultdict(Counter)

    def compare(label, treatment, control):
        row = {
            "task": treatment["task"],
            "customer": treatment["customer"],
            "source": treatment["source"],
            "arm": treatment["arm"],
            "stage": treatment["stage"],
            "stratum": treatment["stratum"],
            "run": treatment["run"],
            "control_run": control["run"],
            "state": status(treatment["official"], control["official"]),
            "low": int(treatment["official"] is True) - int(control["official"] is not False),
            "high": int(treatment["official"] is not False) - int(control["official"] is True),
        }
        paired[label].append(row)

    for c in rows:
        tid, sid, arm, stage = (c[k] for k in ("task", "source", "arm", "stage"))
        notice = plans[tid]["notices"][stage]
        other = next(s for s in study.SOLVES if s != stage)
        grp = by_arm[notice + "/" + arm]
        grp["assigned"] += 1
        grp["graded"] += c["official"] is not None
        grp["success"] += c["official"] is True
        grp["exposed"] += bool(c.get("body_exposed"))
        if not sid:
            if notice == DOMAIN:
                compare(arm + "/notice_order", c, index[tid, None, arm, other])
            continue
        form, variant = arm.rsplit("_", 1)
        if variant == "explicit":
            compare("writer/" + notice, c, index[tid, sid, form + "_original", stage])
        if notice == DOMAIN:
            compare("notice/" + variant, c, index[tid, sid, arm, other])
            if variant == "explicit":
                for control in ("empty_preload", "no_memory"):
                    compare("revised/" + control, c, index[tid, None, control, stage])
    for label, pairs in paired.items():
        groups[label] = paired_effect(pairs, interval=True)
        groups[label]["all_pair_bounds"] = [sum(p[k] for p in pairs) / len(pairs) for k in ("low", "high")]
    primary = paired["revised/no_memory"]
    return {
        "by_notice_and_arm": by_arm,
        "contrasts": groups,
        "pairs": paired,
        "primary_by_stratum": {
            k: paired_effect([p for p in primary if p["stratum"] == k], interval=True)
            for k in sorted({p["stratum"] for p in primary})
        },
    }


def analyze(workspace, workers, *, writing_only=False):
    cfg, base, _, rows, tasks, pre = study.prepare(workspace)
    root = workspace / "runs" / cfg["name"]
    registration = root / "registration.json"
    reg = study.retail.read(registration)
    cap = reg["preflight"]["authorized_usd"]
    require(
        pre == study.retail.read(ROOT / study.PREFLIGHT)
        and reg["preflight"] == dict(pre, authorized_usd=cap)
        and 0 < Decimal(cap) <= Decimal(cfg["total_usd_limit"]),
        "Recorded protocol, cohort or budget changed",
    )
    sources = [r for r in rows if r["scheduled_stages"] == ["write"]]
    if writing_only:
        # Reconstruct the same sealed costs from recorded grants before the consultations finish.
        stages = {}
        total = Decimal(0)
        width = cfg["concurrency_by_stage"]["write"]
        order = pre["stage_order"]["write"]
        for start in range(0, len(order), width):
            wave = study.retail.read(root / f"write-wave-{start // width + 1}.json")
            grant = Decimal(wave["per_shard_grant"])
            require(
                wave["tasks"] == order[start : start + width]
                and Decimal(wave["prior_covered"]) == total
                and grant
                == ((Decimal(cap) - total) / len(wave["tasks"])).quantize(
                    Decimal("0.000001"), rounding=ROUND_DOWN
                ),
                "Writer grant differs from its wave or exceeds budget",
            )
            for sid in wave["tasks"]:
                name = f"task-{sid}/write"
                if grant:
                    seal = study.retail.accounting(root / name, str(grant))
                else:
                    seal = {
                        "status": "not_started",
                        "reason": "spend_limit",
                        "reported": "0",
                        "held": "0",
                        "spent": "0",
                        "http": 0,
                    }
                stages[name] = seal
                total += Decimal(seal["spent"])
        end = {"stages": stages}
    else:
        end = study.retail.read(root / "completion.json")
        require(
            digest(registration.read_bytes()) == end["registration_sha256"]
            and set(end["stages"]) == {f"task-{r['task']}/{s}" for r in rows for s in r["scheduled_stages"]},
            "Recorded registration or phase changed",
        )
    writers, ids = [], []
    for row in sources:
        batch, generation_ids, banks = audited_writers(workspace, root, cfg, base, row, reg, end)
        writers.extend(batch)
        ids.extend(generation_ids)
        cfg["donors"][row["task"]]["banks"] = banks
    if writing_only:
        started = [w for w in writers if w.get("session")]
        require(
            len(writers) == pre["writer_allocations"]
            and len(ids) == len(set(ids))
            and len({w["session"] for w in started}) == len(started)
            and len({w["run"] for w in started}) == len(started),
            "Incomplete or reused writer allocations",
        )
        return {
            "study": cfg["name"],
            "phase": "write",
            "implementation": reg["implementation"],
            "registration_sha256": digest(registration.read_bytes()),
            "stage_seals": stages,
            "total_covered_usd": str(total),
            "authorized_study_usd": cap,
            "generations": len(ids),
            "analysis_model_calls": 0,
            "writer_summary": writer_summary(writers),
            "writers": writers,
        }
    cfg["stages"] = list(study.SOLVES)
    targets = [r for r in rows if "pairs" in r]
    with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
        batches = list(
            pool.map(audit_target, [(workspace, cfg, base, r, tasks[r["task"]], reg, end) for r in targets])
        )
    solves = [c for batch, _ in batches for c in batch]
    ids += [i for _, group in batches for i in group]
    sessions = [c["session"] for c in writers + solves if "session" in c]
    runs = [c["run"] for c in writers + solves if c["run"]]
    require(
        len(writers) == pre["writer_allocations"]
        and len(solves) == pre["solve_allocations"]
        and len(ids) == len(set(ids))
        and len(sessions) == len(set(sessions))
        and len(runs) == len(set(runs)),
        "Missing or reused sessions",
    )
    budget = {
        k: str(sum(Decimal(s[k]) for s in end["stages"].values())) for k in ("reported", "held", "spent")
    }
    require(
        Decimal(budget["spent"])
        == Decimal(end["total_covered_usd"])
        <= Decimal(cap)
        <= Decimal(cfg["total_usd_limit"]),
        "Cost differs from the authorized total",
    )
    return {
        "study": cfg["name"],
        "implementation": reg["implementation"],
        "registration_sha256": end["registration_sha256"],
        "completion_sha256": digest((root / "completion.json").read_bytes()),
        "cost": dict(budget, authorized_usd=cap),
        "generations": len(ids),
        "analysis_model_calls": 0,
        "writer_summary": writer_summary(writers),
        "solve_summary": solve_summary(solves, pre["targets"]),
        "writers": writers,
        "solves": solves,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--writing-only", action="store_true", help="Audit the sealed writing phase alone.")
    args = parser.parse_args()
    result = analyze(args.workspace.resolve(), args.workers, writing_only=args.writing_only)
    study.retail.fixed(args.output, result)
    print(result["writer_summary"]["paired_storage"])
    if not args.writing_only:
        print(result["solve_summary"]["contrasts"]["revised/no_memory"])


if __name__ == "__main__":
    main()
