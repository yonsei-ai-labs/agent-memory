"""Read the interrupted episode and connect the three original retail ledgers offline."""

from collections import defaultdict
from copy import deepcopy
from decimal import Decimal

from agent_memory_eval.contracts.core import MemoryPolicy, digest
from agent_memory_eval.contracts.validation import ROOT, validate_manifest
from agent_memory_eval.memory.context import (
    memory_system_suffix,
    past_memory_instruction,
    snapshot_message,
)
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.journal import BlobStore, read_experiment

if __package__:
    from . import run_retail_sample_recovery as recovery
    from .analyze_full_retail import opening_hashes, require
else:
    import run_retail_sample_recovery as recovery
    from analyze_full_retail import opening_hashes, require

retail = recovery.retail
supplement = recovery.supplement


def interrupted_episode(workspace, folder, slot, frozen, cfg, base, row):
    job = folder / slot["directory"]
    run = slot["run_id"]
    scan, state, _ = read_experiment(job)
    require(
        frozen["run"] == run
        and frozen["status"] == "open"
        and slot["arm"] == "empty_preload"
        and scan["pending_requests"] == [frozen["request"]]
        and scan["incomplete_runs"] == [run]
        and not scan["tail_bytes"]
        and digest((job / "log.jsonl").read_bytes()) == frozen["journal_sha256"],
        "Open run does not match the registered interruption",
    )
    blobs = BlobStore(job)
    start = scan["starts"][run]
    assignment = state["assignments"][run]
    native = blobs.get(assignment["native_config_ref"])
    policy = MemoryPolicy(**assignment["policy"])
    require(
        start["payload"]["fresh_session"]
        and not start["payload"]["source_run_ids"]
        and policy.read_mode == "preload"
        and not assignment["source_writer_snapshot_id"]
        and not assignment["public_context_sha256"]
        and snapshot_items(blobs, state, start["payload"]["start_snapshot_id"]) == []
        and native["seed"] == cfg["solver_seeds"][1]
        and native["domain"] == "retail"
        and native["task_ids"] == [row["task"]]
        and native["task_split_name"] == row["split"],
        "Open episode received different treatment, task or seed",
    )
    instruction = blobs.get(assignment["instruction_ref"])
    require(
        digest(instruction.encode())
        == base["second_pass"]["solve_instruction_ref"]["sha256"],
        "Open episode instruction changed",
    )
    system = blobs.get(start["payload"]["task_ref"])["content"] + memory_system_suffix(
        policy, past_memory_instruction(instruction)
    )
    ends = {
        e["payload"]["request_id"]: e["payload"]
        for e in scan["events"].values()
        if e["type"] == "model.request_finished"
    }
    models = retail.read(folder / "control.json")["model_catalog_entries"]
    ids, records = [], []
    bills = defaultdict(
        lambda: {
            "requests": 0,
            "http_calls": 0,
            "total_tokens": 0,
            "unknown_count": 0,
            "reported_usd": Decimal(0),
        }
    )
    for commit in scan["records"]:
        for event in commit["events"]:
            if event["type"] != "model.request_started":
                continue
            req = event["payload"]
            rid, role = req["request_id"], req["actor_role"]
            origin = blobs.get(commit["state_ref"])["provenance"][rid]
            require(
                not validate_manifest(
                    job,
                    blobs.get(req["input_manifest_ref"]),
                    event,
                    blobs.get(origin["sources_ref"]),
                    scan["events"],
                    origin["approved_templates"],
                ),
                "Empty control exposed a memory field",
            )
            wire = blobs.get(req["request_ref"])
            model = models[role]
            require(
                wire["model"] == model["id"]
                and wire["provider"]
                == {
                    "only": [model["selected_endpoint"]["tag"]],
                    "allow_fallbacks": False,
                    "require_parameters": True,
                },
                "Interrupted request has a different provider",
            )
            if role == "worker":
                require(
                    wire["seed"] == native["seed"]
                    and wire["messages"][0] == {"role": "system", "content": system}
                    and wire["messages"][1] == snapshot_message([])
                    and not any(
                        t["function"]["name"].startswith("memory_")
                        for t in wire["tools"]
                    ),
                    "Interrupted worker received a different input",
                )
            else:
                require(
                    role == "user_simulator"
                    and "seed" not in wire
                    and snapshot_message([]) not in wire["messages"],
                    "Interrupted simulator received a treatment",
                )
            bill = bills[role]
            bill["requests"] += 1
            if rid not in ends:
                require(rid == frozen["request"], "Additional open request")
                bill["unknown_count"] += 1
                records.append((role, wire, None, "prepared"))
                continue
            end = ends[rid]
            raw = blobs.get(end["response_ref"])
            require(
                end["delivery_status"] == "response_received"
                and end["status"] == "response"
                and raw["model"] in (model["id"], model["canonical_slug"])
                and raw["provider"] == model["selected_endpoint"]["provider_name"]
                and end["cost_status"] == "observed"
                and end["usage"] == raw["usage"]
                and end["cost_usd"] == raw["usage"]["cost"],
                "Changed interrupted response or billing",
            )
            bill["http_calls"] += 1
            bill["total_tokens"] += end["usage"]["total_tokens"]
            bill["reported_usd"] += Decimal(str(end["cost_usd"]))
            ids.append(raw["id"])
            records.append((role, wire, raw, "response"))
    require(
        len(ids) == len(set(ids)) == 29
        and len(records) == 30
        and sum(b["reported_usd"] for b in bills.values())
        == Decimal(frozen["reported_usd"])
        and not any(
            e["type"] in {"memory.input_included", "outcome.scored", "run.ended"}
            for e in scan["events"].values()
        ),
        "Open episode has conflicting status, exposure or billing",
    )
    costs = {
        role: dict(v, reported_usd=str(v["reported_usd"])) for role, v in bills.items()
    }
    return {
        "generation_ids": ids,
        "cell": {
            "run": run,
            "session": start["session_id"],
            "status": "interrupted",
            "grading": "incomplete",
            "official": None,
            "termination": "host_restart",
            "pending_request": frozen["request"],
            "journal": str((job / "log.jsonl").relative_to(workspace)),
            "journal_sha256": frozen["journal_sha256"],
            "writer_run": None,
            "snapshot": None,
            "steps": None,
            "native_reward": None,
            "responses": len(ids),
            "costs": costs,
            "input_spans": 0,
            "opening": opening_hashes(records),
        },
    }


def checkpoint_inputs(workspace, cfg, base, rows, tasks):
    c2, _, _, rows2, _, p2, receipt1 = supplement.prepare(workspace)
    c3, _, _, rows3, _, p3, receipt2 = recovery.prepare(workspace)
    require(
        receipt2 == retail.read(ROOT / recovery.RECEIPT)
        and p3 == retail.read(ROOT / recovery.ALLOCATION),
        "Recovery differs from frozen original disposition or eligible schedule",
    )
    root1, root2, root3 = [
        workspace / "runs" / c["run_directory"] for c in (cfg, c2, c3)
    ]
    reg1, reg2 = [retail.read(d / "registration.json") for d in (root1, root2)]
    require(reg2["preflight"] == p2, "Previous supplement registration changed")
    end2 = {"stages": deepcopy(receipt2["stages"])}
    spec = retail.read(ROOT / recovery.PROFILE)
    partial = root2 / spec["open_stage"]
    open_entry = end2["stages"][spec["open_stage"]]
    slots = retail.read(partial / "allocations.json")
    (open_slot,) = [s for s in slots if s["status"] == "started"]
    (price,) = [x for x in receipt2["uncertain"] if x["status"] == "open"]
    source_row = next(r for r in rows2 if r["task"] == open_slot["task_id"])
    opened = interrupted_episode(
        workspace, partial, open_slot, price, c2, base, source_row
    )
    ended = [s for s in slots if "result" in s]
    open_entry.update(
        interrupted_episode=opened,
        reported=str(
            sum(recovery.recorded_cost(s) for s in ended)
            + Decimal(price["reported_usd"])
        ),
        http=sum(s["result"]["http_calls"] for s in ended)
        + sum(x["http_calls"] for x in opened["cell"]["costs"].values()),
    )
    passes = []
    for r in rows:
        if r["cohort"] == "new_train":
            passes.append(
                (
                    workspace,
                    cfg,
                    base,
                    dict(r, scheduled_stages=["source", "write", "solve1"]),
                    tasks[r["task"]],
                    reg1,
                    receipt1,
                )
            )
    for r in rows2:
        stages = [
            s
            for s in r["scheduled_stages"]
            if f"task-{r['task']}/{s}" in end2["stages"]
        ]
        if stages:
            passes.append(
                (
                    workspace,
                    c2,
                    base,
                    dict(r, scheduled_stages=stages),
                    tasks[r["task"]],
                    reg2,
                    end2,
                )
            )
    replaced = {
        s["unstarted_run"]: {"task": tid, "stage": "solve1", "arm": s["arm"]}
        for tid, slots in receipt1["unstarted"].items()
        for s in slots
    }
    for x in receipt2["untouched"]:
        if x["previous_run"]:
            require(x["previous_run"] not in replaced, "Duplicate superseded run")
            replaced[x["previous_run"]] = {k: x[k] for k in ("task", "stage", "arm")}
    stages = {
        f"{prefix}/{name}": entry
        for prefix, receipt in [("r1", receipt1), ("r2", end2)]
        for name, entry in receipt["stages"].items()
    }
    sources = {
        "prior_covered_usd": receipt2["covered_usd"],
        "initial_directory": root1.name,
        "interrupted_directory": root2.name,
        "open_request": price,
        "unclosed_request_count": 1,
        "initial_receipt_sha256": receipt2["initial_receipt_sha256"],
        "recovery_receipt_sha256": digest((ROOT / recovery.RECEIPT).read_bytes()),
        "recovery_schedule_sha256": digest((ROOT / recovery.ALLOCATION).read_bytes()),
    }
    return passes, replaced, stages, sources, (root3, c3, rows3, p3)


def inputs(workspace, cfg, base, rows, tasks, reconcile_closed):
    passes, replaced, stages, sources, final = checkpoint_inputs(
        workspace, cfg, base, rows, tasks
    )
    root3, c3, rows3, p3 = final
    reg3, end3 = reconcile_closed(root3, c3, rows3, p3)
    for r in rows3:
        passes.append((workspace, c3, base, r, tasks[r["task"]], reg3, end3))
    stages.update({f"r3/{name}": entry for name, entry in end3["stages"].items()})
    sources["admission"] = retail.read(root3 / "admission.json")
    return root3, reg3, dict(end3, stages=stages), passes, replaced, sources
