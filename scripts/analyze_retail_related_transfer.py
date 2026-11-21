"""Strictly replay related retail tasks and compare transferred notes with fresh target controls."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from multiprocessing import get_context
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.contracts.core import MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.memory.context import (
    memory_system_suffix,
    past_memory_instruction,
    snapshot_message,
)
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.journal import BlobStore, safe_path
from scripts import run_retail_related_transfer as study
from scripts.analyze_full_retail import (
    opening_hashes,
    read_request_data,
    require,
    verify_grade,
)
from scripts.analyze_recheck_rounds import inspect_episode, mutation_key, status
from scripts.analyze_retail_sample_expansion import paired_effect


def audited_episode(workspace, folder, res, cell, cfg, task, reg, models):
    sid, arm, run = cell["source"], cell["arm"], cell["run"]
    blob = BlobStore(folder)
    tail = read_json((folder / "log.jsonl").read_bytes().splitlines()[-1])
    assignment = blob.get(tail["state_ref"])["assignments"][run]
    grader = assignment["native_grade_limits"]
    judge, judge_config = blob.get(grader["model_ref"]), blob.get(grader["config_ref"])
    require(
        study.retail.endpoint_terms({"grader": judge})["grader"] == reg["endpoint_terms"]["grader"]
        and judge_config == dict(cfg["judge"], timeout_seconds=cfg["http_timeout_seconds"]),
        "Grader mismatch",
    )
    evidence, ids, state, scan, blobs, records = read_request_data(folder, run, res, workspace, models, judge)
    native = blobs.get(assignment["native_config_ref"])
    policy = MemoryPolicy(**assignment["policy"])
    start = scan["starts"][run]["payload"]
    desc = cfg["donors"][sid]["banks"][arm] if sid else None
    bank = (
        read_form_bank(
            safe_path(workspace, desc["journal"]).parent, expected_log_sha256=desc["journal_sha256"]
        )
        if desc
        else None
    )
    items = bank["items"] if bank else []
    require(
        start["fresh_session"]
        and start["source_run_ids"] == ([bank["run_id"]] if bank else [])
        and assignment.get("source_task_id") == sid
        and assignment["task_id"] == task.id
        and assignment["source_writer_snapshot_id"] == (desc["snapshot_id"] if bank else None)
        and assignment["source_writer_bank_sha256"] == (digest(canonical(bank)) if bank else None)
        and assignment["public_context_sha256"] == (desc["public_context_sha256"] if bank else None)
        and native["seed"] == 12
        and native["task_ids"] == [task.id]
        and native["task_split_name"] == "train"
        and native["domain"] == "retail",
        "Wrong source or target",
    )
    require(
        policy.enabled == (arm != "no_memory")
        and policy.write_timing == "none"
        and policy.read_mode == ("preload" if policy.enabled else "none"),
        "Wrong delivery policy",
    )
    system = blobs.get(start["task_ref"])["content"]
    instruction = blobs.get(assignment["instruction_ref"])
    version = cell.get("preload_notice_version") if policy.enabled else None
    require(assignment.get("preload_notice_version") == version, "Wrong registered notice")
    if policy.enabled:
        require(
            snapshot_items(blobs, state, start["start_snapshot_id"]) == items
            and snapshot_items(blobs, state, res["snapshot_id"]) == items
            and assignment["input_items_sha256"] == digest(canonical(items))
            and digest(instruction.encode()) == reg["preflight"]["instruction_sha256"],
            "Snapshot or prompt changed",
        )
        rendered = past_memory_instruction(instruction, notice_version=version)
        if version is not None:
            require(digest(rendered.encode()) == reg["preflight"]["notice_instruction_sha256"][version],
                    "Notice wording differs from registration")
        system += memory_system_suffix(policy, rendered)
    else:
        require(not state["banks"] and not start["bank_id"] and instruction == "", "Control inherited memory")
    grader_wires = []
    worker_responses = 0
    for actor, wire, raw, result in records:
        require(
            not any(t["function"]["name"].startswith("memory_") for t in wire.get("tools", [])),
            "Unexpected memory tool",
        )
        if actor == "worker":
            require(
                wire["seed"] == native["seed"]
                and wire["messages"][0] == {"role": "system", "content": system}
                and (
                    wire["messages"][1] == snapshot_message(items)
                    if policy.enabled
                    else snapshot_message([]) not in wire["messages"]
                ),
                "Wrong worker exposure",
            )
            worker_responses += result == "response"
        elif actor == "user_simulator":
            require(
                "seed" not in wire and snapshot_message(items) not in wire["messages"], "Simulator got notes"
            )
        else:
            require(
                actor == "grader" and snapshot_message(items) not in wire["messages"],
                "Unexpected grading input",
            )
            grader_wires.append((wire, raw))
    require(
        bool(evidence["input_spans"]) == bool(items and worker_responses), "Actual memory exposure differs"
    )
    cell.update(
        evidence,
        official=res["official_success"],
        item_count=len(items),
        worker_responses=worker_responses,
        body_exposed=bool(items and worker_responses),
        opening=opening_hashes(records),
        steps=res["native_steps"],
    )
    if res.get("reached_state_assessment_ref"):
        cell["incomplete_state_assessment"] = blobs.get(res["reached_state_assessment_ref"])
    if cell["official"] is not None:
        report = blobs.get(res["report_ref"])
        cell["grade"] = verify_grade(
            task,
            report,
            blobs.get(res["before_grade_ref"]),
            res["official_score"],
            grader_wires,
            judge_config,
            judge,
            native,
        )
        require((res["official_score"] == 1) is cell["official"], "Official label differs from grade")
        cell["writes"] = inspect_episode(report, task.evaluation_criteria.model_dump(mode="json"))["writes"]
    return ids


def audit_target(args):
    workspace, cfg, base, row, task_data, reg, end = args
    study.retail.modules_for(workspace)
    from tau2.data_model.tasks import Task

    task = Task.model_validate(task_data)
    cells, ids = [], []
    with patch(
        "socket.socket.connect", side_effect=AssertionError("Transfer analysis has no model transport")
    ):
        for stage in cfg["stages"]:
            name = f"task-{task.id}/{stage}"
            folder = workspace / "runs" / cfg["run_directory"] / name
            seal = end["stages"][name]
            design, jobs = study.design_for(cfg, base, row, stage)
            slots = [None] * len(jobs)
            if seal.get("status") == "not_started":
                require(
                    not folder.exists()
                    and seal
                    == {
                        "status": "not_started",
                        "reason": "spend_limit",
                        "reported": "0",
                        "held": "0",
                        "spent": "0",
                        "http": 0,
                    },
                    "Unfunded shard was attempted",
                )
            else:
                _, ctl, slots = study.retail.sealed_rows(folder)
                models = ctl["model_catalog_entries"]
                require(
                    ctl["design"] == design
                    and ctl["implementation"] == reg["implementation"]
                    and ctl["profile"]["config_sha256"] == reg["preflight"]["protocol_sha256"]
                    and study.retail.accounting(folder, str(ctl["profile"]["guards"]["max_usd"])) == seal
                    and study.retail.endpoint_terms(models)
                    == {k: v for k, v in reg["endpoint_terms"].items() if k != "grader"},
                    "Shard seal or policy differs",
                )
            for slot, job in zip(slots, jobs, strict=True):
                sid = job.get("source_task_id")
                cell = {
                    "task": task.id,
                    "customer": row["customer"],
                    "source": sid,
                    "arm": job["arm"],
                    "stage": stage,
                    "stratum": row["pairs"][sid]["stratum"] if sid else None,
                    "run": None,
                    "official": None,
                }
                cells.append(cell)
                if "notice_by_stage" in row:
                    cell["preload_notice_version"] = row["notice_by_stage"][stage] if job["arm"] != "no_memory" else None
                if slot is None:
                    cell.update(status="not_started", reason=seal["reason"])
                    continue
                require(
                    all(slot[k] == v for k, v in job.items())
                    and slot.get("source_task_id") == sid
                    and slot["directory"] == f"jobs/{job['index']:04d}",
                    "Wrong source or scheduled slot",
                )
                path = safe_path(folder, slot["directory"])
                cell.update(status=slot["status"], run=slot["run_id"])
                if "result" not in slot:
                    require(
                        slot["status"] == "not_started" and not path.exists(), "Unexpected unfinished slot"
                    )
                    cell["reason"] = slot["reason"]
                    continue
                res = slot["result"]
                require(
                    (res["run_id"], res["status"], res["arm"], res["task_id"])
                    == (cell["run"], slot["status"], cell["arm"], task.id),
                    "Episode differs from assignment",
                )
                ids.extend(audited_episode(workspace, path, res, cell, cfg, task, reg, models))
    openings = {
        c["opening"]["simulator_request"] for c in cells if c.get("opening", {}).get("simulator_request")
    }
    require(len(openings) <= 1, "Different initial simulator inputs within this target")
    return cells, ids


def compare_actions(cell, required, old_reference, old_observed):
    if "writes" not in cell:
        return None
    done = Counter(mutation_key(w["name"], w["args"]) for w in cell["writes"] if w["success"])
    extra = done - required
    return {
        "required_matched": (done & required).total(),
        "required_total": required.total(),
        "extra": extra.total(),
        "old_reference_only": (extra & (old_reference - required)).total(),
        "old_observed_only": (extra & (old_observed - required)).total(),
    }


def summarize(cells, reg, tasks, forms, controls):
    def mutations(actions):
        return Counter(
            mutation_key(a["name"], a["arguments"]) for a in actions if a["name"] in study.MUTATIONS
        )

    expected = {tid: mutations(t["evaluation_criteria"]["actions"] or []) for tid, t in tasks.items()}
    historic = {tid: mutations(row["executed"]) for tid, row in reg["database_replay"]["observed"].items()}
    baseline = {(c["task"], c["stage"], c["arm"]): c for c in cells if not c["source"]}
    pairs, groups = [], defaultdict(list)
    for c in cells:
        sid, tid = c["source"], c["task"]
        if sid is None:
            continue
        action_sets = expected[tid], expected[sid], historic[sid]
        pair = {
            "task": tid,
            "customer": c["customer"],
            "source": sid,
            "arm": c["arm"],
            "stage": c["stage"],
            "stratum": c["stratum"],
            "run": c["run"],
            "actions": compare_actions(c, *action_sets),
        }
        pairs.append(pair)
        for control in controls:
            ref = baseline[tid, c["stage"], control]
            state = status(c["official"], ref["official"])
            pair[control] = {"run": ref["run"], "state": state, "actions": compare_actions(ref, *action_sets)}
            for stratum in ("all", c["stratum"]):
                for form in ("all", c["arm"]):
                    groups[stratum, form, control].append(
                        {
                            "task": tid,
                            "customer": c["customer"],
                            "state": state,
                            "low": int(c["official"] or False) - int(ref["official"] is not False),
                            "high": int(c["official"] is not False) - int(ref["official"] or False),
                        }
                    )
    effects = {}
    for (stratum, form, control), entries in groups.items():
        result = paired_effect(entries, interval=True)
        result.update(
            assigned=len(entries),
            all_pair_bounds=[sum(x[k] for x in entries) / len(entries) for k in ("low", "high")],
        )
        effects.setdefault(stratum, {}).setdefault(form, {})[control] = result
    arms = {}
    for arm in [*forms, *controls]:
        group = [c for c in cells if c["arm"] == arm]
        arms[arm] = {
            "assigned": len(group),
            "attempted": sum("costs" in c for c in group),
            "successes": sum(c["official"] is True for c in group),
            "graded": sum(c["official"] is not None for c in group),
            "notes_exposed": sum(bool(c.get("body_exposed")) for c in group),
            "db_matches": sum(c.get("grade", {}).get("db_match") is True for c in group),
        }
    return {"by_arm": arms, "paired_effects": effects, "pairs": pairs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    cfg, base, _, rows, targets, pre = study.prepare(workspace)
    root = workspace / "runs" / cfg["run_directory"]
    end = study.retail.read(root / "completion.json")
    reg = study.expansion.verified(root / "registration.json", end["registration_sha256"])
    cap = reg["preflight"]["authorized_usd"]
    require(
        dict(pre, authorized_usd=cap) == reg["preflight"]
        and pre == study.retail.read(ROOT / study.PREFLIGHT),
        "Registered cohort or budget differs",
    )
    require(
        set(end["stages"]) == {f"task-{r['task']}/{s}" for r in rows for s in cfg["stages"]},
        "Wrong stage set",
    )
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=get_context("spawn")) as pool:
        batches = list(
            pool.map(audit_target, [(workspace, cfg, base, r, targets[r["task"]], reg, end) for r in rows])
        )
    cells = [c for batch, _ in batches for c in batch]
    ids = [id_ for _, subset in batches for id_ in subset]
    sessions = [c["session"] for c in cells if "session" in c]
    runs = [c["run"] for c in cells if c["run"]]
    require(
        len(set(ids)) == len(ids)
        and len(set(sessions)) == len(sessions)
        and len(set(runs)) == len(runs)
        and len(cells)
        == pre["allocations"]
        == len({(c["task"], c["source"], c["arm"], c["stage"]) for c in cells}),
        "Reused identity or missing assignment",
    )
    budget = {
        k: str(sum(Decimal(v[k]) for v in end["stages"].values())) for k in ("reported", "held", "spent")
    }
    require(Decimal(budget["spent"]) == Decimal(end["total_covered_usd"]) <= Decimal(cap), "Cost differs")
    raw = {
        t["id"]: t
        for t in study.retail.read(workspace / "data/tau2-bench/data/tau2/domains/retail/tasks.json")
    }
    summary = summarize(cells, pre, raw, cfg["transfer_spec"]["forms"], cfg["controls"])
    study.retail.fixed(
        args.output,
        dict(
            study=cfg["name"],
            implementation=reg["implementation"],
            registration_sha256=end["registration_sha256"],
            completion_sha256=digest((root / "completion.json").read_bytes()),
            cost=dict(budget, authorized_usd=cap),
            generations=len(ids),
            analysis_model_calls=0,
            **summary,
            cells=cells,
        ),
    )
    print(summary["by_arm"])


if __name__ == "__main__":
    main()
