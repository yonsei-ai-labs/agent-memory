"""Regrade retail retrieval episodes and match outcomes to voluntary reads and costs."""

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from multiprocessing import get_context
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.contracts.core import MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_solve import resolve_retrieval_pool
from agent_memory_eval.memory.context import memory_system_suffix
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore

if __package__:
    from . import run_retail_candidate_pools as study
    from .analyze_full_retail import (
        opening_hashes,
        read_request_data,
        require,
        verify_grade,
    )
    from .analyze_recheck_rounds import inspect_episode, status
    from .analyze_retail_sample_expansion import paired_effect
else:
    import run_retail_candidate_pools as study
    from analyze_full_retail import (
        opening_hashes,
        read_request_data,
        require,
        verify_grade,
    )
    from analyze_recheck_rounds import inspect_episode, status
    from analyze_retail_sample_expansion import paired_effect


def reads(events, records, origin):
    requested = Counter()
    for actor, _, raw, outcome in records:
        if actor == "worker" and outcome == "response":
            for call in raw["choices"][0]["message"].get("tool_calls") or []:
                name = call["function"]["name"]
                if name.startswith("memory_"):
                    requested[name] += 1
    finishes = {
        e["payload"]["request_id"]: e["payload"]["status"]
        for e in events
        if e["type"] == "model.request_finished"
    }
    actors = {
        e["payload"]["request_id"]: e["payload"]["actor_role"]
        for e in events
        if e["type"] == "model.request_started"
    }
    returns, exposed = [], {}
    for event in events:
        p = event["payload"]
        if event["type"] == "memory.returned":
            require(p["operation"] in {"list", "get"}, "Unexpected memory operation")
            returns.append(
                {
                    "operation": p["operation"],
                    "status": p["status"],
                    "seq": event["event_seq"],
                    "items": [
                        dict(source_task=origin[i["item_id"], i["version_id"]], **i) for i in p["items"]
                    ],
                }
            )
        if event["type"] == "memory.input_included":
            require(
                p["source"]["kind"] == "memory_return" and actors[p["request_id"]] == "worker",
                "Unexpected memory exposure in worker, simulator or grading input",
            )
            if finishes[p["request_id"]] == "response":
                identity = p["item_id"], p["version_id"], p["field"]
                exposed.setdefault(
                    identity,
                    {
                        "item": p["item_id"],
                        "version": p["version_id"],
                        "field": p["field"],
                        "source_task": origin[identity[:2]],
                        "first_request": p["request_id"],
                    },
                )
    return {"requested": dict(requested), "returned": returns, "exposed": list(exposed.values())}


def audit_shard(args):
    workspace, root, cfg, base, row, task_data, reg, seal = args
    study.retail.modules_for(workspace)
    from tau2.data_model.tasks import Task

    task = Task.model_validate(task_data)
    tid = row["task"]
    folder = root / ("task-" + tid) / "retrieval"
    source = study.retail.recorded_source(workspace / row["source_folder"], tid)
    design, jobs, pools = study.design_for(cfg, base, row, source)
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
            "Unexpected evidence in an unfunded shard",
        )
        return [
            {
                "task": tid,
                "customer": row["customer"],
                "arm": j["arm"],
                "run": None,
                "status": "not_started",
                "reason": "spend_limit",
                "first_pass": source.official_success,
                "official": None,
            }
            for j in jobs
        ], []
    _, ctl, slots = study.retail.sealed_rows(folder)
    require(
        ctl["design"] == design
        and ctl["implementation"] == reg["implementation"]
        and ctl["profile"]["config_sha256"] == reg["preflight"]["protocol_sha256"]
        and study.retail.accounting(folder, str(ctl["profile"]["guards"]["max_usd"])) == seal,
        "Shard mismatch",
    )
    models = ctl["model_catalog_entries"]
    require(
        study.endpoint_terms(models) == {k: v for k, v in reg["endpoint_terms"].items() if k != "grader"},
        "Unexpected worker or simulator endpoint",
    )
    cells, generations = [], []
    for slot, job in zip(slots, jobs, strict=True):
        arm, run, sub = slot["arm"], slot["run_id"], folder / slot["directory"]
        require(
            all(slot[k] == v for k, v in job.items()) and slot["directory"] == f"jobs/{job['index']:04d}",
            "Wrong slot",
        )
        cell = {
            "task": tid,
            "customer": row["customer"],
            "arm": arm,
            "run": run,
            "status": slot["status"],
            "first_pass": source.official_success,
            "official": None,
        }
        cells.append(cell)
        if "result" not in slot:
            require(slot["status"] == "not_started" and not sub.exists(), "Unsealed attempted allocation")
            cell["reason"] = slot.get("reason")
            continue
        result = slot["result"]
        require(result["run_id"] == run and result["status"] == slot["status"], "Run/slot mismatch")
        blob = BlobStore(sub)
        assignment = blob.get(read_json((sub / "log.jsonl").read_bytes().splitlines()[-1])["state_ref"])[
            "assignments"
        ][run]
        grader = assignment["native_grade_limits"]
        judge, judge_cfg = blob.get(grader["model_ref"]), blob.get(grader["config_ref"])
        require(
            study.endpoint_terms({"grader": judge})["grader"] == reg["endpoint_terms"]["grader"]
            and judge_cfg == dict(cfg["judge"], timeout_seconds=cfg["http_timeout_seconds"]),
            "Grader mismatch",
        )
        info, ids, state, scan, blobs, records = read_request_data(
            sub, run, result, workspace, models, judge
        )
        generations.extend(ids)
        cell.update(
            info,
            official=result["official_success"],
            native_steps=result["native_steps"],
            opening=opening_hashes(records),
        )
        starts = scan["starts"][run]["payload"]
        native = blobs.get(assignment["native_config_ref"])
        policy = MemoryPolicy(**assignment["policy"])
        require(
            starts["fresh_session"]
            and (native["seed"], native["task_ids"], native["task_split_name"], native["domain"])
            == (cfg["pool_spec"]["seed"], [tid], row["split"], "retail"),
            "Wrong task, seed or session",
        )
        system = blobs.get(starts["task_ref"])["content"]
        origin = {}
        if arm != "no_memory":
            pool = pools[arm]
            items, lineage = resolve_retrieval_pool(
                design["delivery_arms"][arm],
                pool,
                tid,
                1,
                row["public_context_sha256"],
                cfg["pool_spec"]["forms"],
            )
            require(
                policy.read_mode == "on_demand"
                and policy.write_timing == "none"
                and blobs.get(assignment["ordered_writer_sources_ref"]) == lineage
                and starts["source_run_ids"] == [b["run_id"] for b in pool]
                and snapshot_items(blobs, state, starts["start_snapshot_id"]) == items
                and snapshot_items(blobs, state, result["snapshot_id"]) == items
                and assignment["input_items_sha256"] == digest(canonical(items)),
                "Wrong frozen memory",
            )
            instruction = blobs.get(assignment["instruction_ref"])
            require(
                digest(instruction.encode()) == reg["preflight"]["instruction_sha256"], "Wrong read prompt"
            )
            system += memory_system_suffix(policy, instruction)
            origin = {(i["id"], i["version"]): b["task_id"] for b in pool for i in b["items"]}
        else:
            require(
                not policy.enabled
                and not starts["source_run_ids"]
                and not starts["bank_id"]
                and not state["banks"],
                "Control inherited memory",
            )
        for actor, wire, _, _ in records:
            memory_names = {
                t["function"]["name"]
                for t in wire.get("tools", [])
                if t["function"]["name"].startswith("memory_")
            }
            require(
                memory_names
                == ({"memory_get", "memory_list"} if actor == "worker" and policy.enabled else set()),
                "Wrong role tools",
            )
            if actor == "worker":
                require(
                    wire["messages"][0] == {"role": "system", "content": system}
                    and wire["seed"] == native["seed"],
                    "Worker policy or seed mismatch",
                )
        events = [e for e in scan["events"].values() if e["run_id"] == run]
        require(
            not any(e["type"] in {"memory.stored", "memory.preloaded"} for e in events),
            "Unexpected preload/write",
        )
        cell["reads"] = reads(events, records, origin)
        if result.get("reached_state_assessment_ref"):
            cell["incomplete_state_assessment"] = blobs.get(result["reached_state_assessment_ref"])
        if cell["official"] is not None:
            report = blobs.get(result["report_ref"])
            grader_wires = [(wire, raw) for actor, wire, raw, _ in records if actor == "grader"]
            cell["grade"] = verify_grade(
                task,
                report,
                blobs.get(result["before_grade_ref"]),
                result["official_score"],
                grader_wires,
                judge_cfg,
                judge,
                native,
            )
            require((result["official_score"] == 1) is cell["official"], "Grade label differs")
            behavior = inspect_episode(report, task_data["evaluation_criteria"])
            cell["business_mutations"] = behavior["writes"]
            cell["exact_mutations"] = behavior["exact_mutations"]
    for key in ("simulator_request", "worker_input_without_opening"):
        hashes = {
            c["opening"][key]
            for c in cells
            if c.get("opening", {}).get(key) and (key == "simulator_request" or c["arm"] != "no_memory")
        }
        require(len(hashes) <= 1, "Matched opening policies or simulator configuration differ")
    return cells, generations


def paired(cells, first, second):
    index = {(c["task"], c["arm"]): c for c in cells}
    pairs, minimum, maximum = [], 0, 0
    for c in cells:
        if c["arm"] != first:
            continue
        a, b = c["official"], index[c["task"], second]["official"]
        pairs.append({"task": c["task"], "customer": c["customer"], "state": status(a, b)})
        minimum += int(a or False) - int(True if b is None else b)
        maximum += int(True if a is None else a) - int(b or False)
    return dict(
        paired_effect(pairs, interval=True),
        assigned=len(pairs),
        all_task_bounds=[minimum / len(pairs), maximum / len(pairs)],
    )


def summarize(cells, forms):
    def saw_field(cell, field):
        return any(x["field"] == field for x in cell["reads"]["exposed"])

    counts, effects = {}, {}
    for arm in sorted({c["arm"] for c in cells}):
        group = [c for c in cells if c["arm"] == arm]
        attempted = [c for c in group if "reads" in c]
        counts[arm] = {
            "assigned": len(group),
            "attempted": len(attempted),
            "graded": sum(c["official"] is not None for c in group),
            "successes": sum(c["official"] is True for c in group),
            "list_episodes": sum(bool(c["reads"]["requested"].get("memory_list")) for c in attempted),
            "get_episodes": sum(bool(c["reads"]["requested"].get("memory_get")) for c in attempted),
            "title_exposed": sum(saw_field(c, "title") for c in attempted),
            "body_exposed": sum(saw_field(c, "body") for c in attempted),
            "other_body_exposed": sum(
                any(x["field"] == "body" and x["source_task"] != c["task"] for x in c["reads"]["exposed"])
                for c in attempted
            ),
            "db_matched": sum(c.get("grade", {}).get("db_match") is True for c in group),
            "first_pass_pairs": dict(Counter(status(c["official"], c["first_pass"]) for c in group)),
            "reported_usd": str(
                sum(
                    (Decimal(r["reported_usd"]) for c in attempted for r in c["costs"].values()), Decimal(0)
                )
            ),
        }
    for form in forms:
        own, mixed = form + "__own", form + "__mixed"
        effects[form] = {
            "mixed_minus_own": paired(cells, mixed, own),
            "own_minus_no_memory": paired(cells, own, "no_memory"),
            "mixed_minus_no_memory": paired(cells, mixed, "no_memory"),
        }
    return {"by_arm": counts, "paired_by_form": effects}


def offline_shard(args):
    with patch("socket.socket.connect", side_effect=AssertionError("Offline grade verification")):
        return audit_shard(args)


def historical_costs(spec):
    groups = {}
    seen = set()
    for ref in spec["sources"]:
        for cell in read_json(asset_text(ROOT, ref).encode())[ref["field"]]:
            if cell["stage"] not in {"source", "write"}:
                continue
            require(cell["run"] not in seen, "Duplicate historical acquisition cost")
            seen.add(cell["run"])
            key = "first_pass" if cell["stage"] == "source" else cell["arm"]
            summary = groups.setdefault(key, {"runs": 0, "reported_usd": Decimal(0), "unknown_requests": 0})
            summary["runs"] += 1
            for role in cell["costs"].values():
                summary["reported_usd"] += Decimal(role["reported_usd"])
                summary["unknown_requests"] += role["unknown_count"]
    for group in groups.values():
        group["reported_usd"] = str(group["reported_usd"])
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    cfg, base, _, rows, tasks, preflight = study.prepare(workspace)
    root = workspace / "runs" / cfg["name"]
    end = study.retail.read(root / "completion.json")
    reg = study.expansion.verified(root / "registration.json", end["registration_sha256"])
    registered = dict(reg["preflight"])
    cap = Decimal(registered.pop("authorized_usd"))
    require(
        cap.is_finite()
        and cap > 0
        and registered == preflight == study.retail.read(ROOT / study.PREFLIGHT),
        "Preflight or registered budget differs",
    )
    work = [
        (
            workspace,
            root,
            cfg,
            base,
            r,
            tasks[r["task"]],
            reg,
            end["stages"]["task-" + r["task"] + "/retrieval"],
        )
        for r in rows
    ]
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=get_context("spawn")) as pool:
        shards = list(pool.map(offline_shard, work))
    cells = [c for group, _ in shards for c in group]
    ids = [i for _, batch in shards for i in batch]
    runs = [c["run"] for c in cells if c["run"] is not None]
    require(
        len(ids) == len(set(ids))
        and len(runs) == len(set(runs))
        and len({(c["task"], c["arm"]) for c in cells}) == len(cells) == preflight["allocations"],
        "Generation or assignment identity reused",
    )
    sessions = [c["session"] for c in cells if "session" in c]
    require(len(sessions) == len(set(sessions)), "Session identity reused")
    budget = {
        k: str(sum(Decimal(v[k]) for v in end["stages"].values())) for k in ("reported", "held", "spent")
    }
    require(
        Decimal(budget["spent"]) == Decimal(end["total_covered_usd"]) <= cap,
        "Cost exceeds the authorized total",
    )
    summary = summarize(cells, cfg["pool_spec"]["forms"])
    study.retail.fixed(
        args.output,
        dict(
            study=cfg["name"],
            implementation=reg["implementation"],
            registration_sha256=end["registration_sha256"],
            completion_sha256=digest((root / "completion.json").read_bytes()),
            cost=dict(budget, authorized_usd=str(cap)),
            shared_historical_costs=historical_costs(cfg["pool_spec"]),
            analysis_model_calls=0,
            generations=len(ids),
            **summary,
            cells=cells,
        ),
    )
    print({k: (v["successes"], v["graded"], v["body_exposed"]) for k, v in summary["by_arm"].items()})


if __name__ == "__main__":
    main()
