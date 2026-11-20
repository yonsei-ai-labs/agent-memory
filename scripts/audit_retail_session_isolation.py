"""Rebuild retail conversations from a clean benchmark and the current run's archived replies."""

import argparse
import json
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from itertools import combinations
from multiprocessing import get_context
from pathlib import Path
from statistics import mean
from unittest.mock import patch

from agent_memory_eval.contracts.connection import provider_route
from agent_memory_eval.contracts.core import MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.memory.context import request_with_past_memory
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, safe_path
from scripts.analyze_full_retail import require
from scripts.analyze_recheck_rounds import status
from scripts.analyze_retail_full_factors import verified
from scripts.analyze_retail_retry_gains import CONTROLS, DETAIL, INPUTS, PARENT
from scripts.analyze_retail_sample_expansion import (
    ROUNDS,
    bootstrap_customer_interval,
    paired_effect,
)
from scripts.replay_full_retail_fields import state_hashes
from scripts.run_retail_sample_expansion import prepare


class ArchivedStop(Exception):
    """The last archived request did not receive a usable model reply."""


def public_turns(messages):
    keys = (
        "role",
        "content",
        "tool_calls",
        "id",
        "requestor",
        "error",
        "execution_status",
    )
    return [{k: m[k] for k in keys if k in m} for m in messages]


def replay_episode(workspace, cell, task, references, bank, settings):
    from litellm import ModelResponse
    from tau2.data_model.simulation import TextRunConfig
    from tau2.data_model.tasks import Task
    from tau2.runner.build import build_text_orchestrator
    from tau2.utils import llm_utils

    journal = safe_path(workspace, cell["journal"])
    blobs = BlobStore(journal.parent)
    run, arm = cell["run"], cell["arm"]
    starts, finishes, exposure_events = [], {}, Counter()
    last_boundary = None
    clock_reversals = []
    for line in verified(journal, cell["journal_sha256"]).splitlines():
        record = read_json(line)
        for event in record["events"]:
            kind, p = event["type"], event["payload"]
            if kind.startswith("model.request_"):
                require(
                    (event["run_id"], event["session_id"]) == (run, cell["session"]),
                    "Request has another run or session",
                )
                boundary = {
                    "event": event["event_seq"],
                    "type": kind,
                    "utc": event["timestamp_utc"],
                }
                if last_boundary and boundary["utc"] < last_boundary["utc"]:
                    clock_reversals.append([last_boundary, boundary])
                last_boundary = boundary
            if kind == "run.started":
                require(
                    event["run_id"] == run and event["session_id"] == cell["session"],
                    "Wrong episode",
                )
                state = blobs.get(record["state_ref"])
                assignment = state["assignments"][run]
                policy = MemoryPolicy(**assignment["policy"])
                items = (
                    snapshot_items(blobs, state, p["start_snapshot_id"])
                    if policy.enabled
                    else []
                )
                require(
                    p["fresh_session"]
                    and assignment["arm"] == arm
                    and assignment["task_id"] == cell["task"]
                    and policy.enabled == (arm != "no_memory")
                    and policy.read_mode == ("preload" if policy.enabled else "none")
                    and p["source_run_ids"] == ([bank["run"]] if bank else [])
                    and assignment["source_writer_snapshot_id"]
                    == (bank["snapshot"] if bank else None)
                    and [{k: i[k] for k in ("title", "body")} for i in items]
                    == (bank["notes"] if bank else [])
                    and (
                        policy.enabled
                        or (p["bank_id"] is None and p["start_snapshot_id"] is None)
                    ),
                    "Wrong memory source",
                )
            elif kind == "model.request_started" and p["actor_role"] in (
                "worker",
                "user_simulator",
            ):
                starts.append(p)
            elif kind == "model.request_finished":
                finishes[p["request_id"]] = p
            elif kind.startswith("memory."):
                exposure_events[kind] += 1
    require(arm != "no_memory" or not exposure_events, "Pure control has memory events")
    require(
        arm not in CONTROLS or not exposure_events["memory.input_included"],
        "Control exposed note content",
    )
    native = TextRunConfig.model_validate(blobs.get(assignment["native_config_ref"]))
    expected_seed = (
        settings["source_seed"]
        if cell["stage"] == "source"
        else settings["solver_seed"]
    )
    require(
        native.seed == expected_seed
        and native.task_ids == [task["id"]]
        and task["initial_state"] is None
        and native.domain == "retail"
        and native.agent == "llm_agent"
        and native.user == "user_simulator"
        and native.llm_agent == settings["models"]["worker"]
        and native.llm_user == settings["models"]["user_simulator"]
        and native.llm_args_agent
        == {**settings["parameters"]["worker"], "seed": expected_seed}
        and native.llm_args_user == settings["parameters"]["user_simulator"],
        "Unexpected task, preexisting state or seed",
    )
    orch = build_text_orchestrator(
        native, Task.model_validate(task), seed=expected_seed
    )
    initial = list(state_hashes(orch.environment))
    require(
        all(r["initial"] == initial for r in references.values()),
        "Replay did not start with the original DB",
    )
    cursor, counts, openings, generation_ids = 0, Counter(), {}, []
    instruction = settings["instruction"] if policy.enabled else ""
    require(
        blobs.get(assignment["instruction_ref"]) == instruction,
        "Wrong solve instruction",
    )

    def completion(**kwargs):
        nonlocal cursor
        require(cursor < len(starts), "Fresh episode requires an unrecorded call")
        start = starts[cursor]
        cursor += 1
        role = start["actor_role"]
        require(
            kwargs["model"] == settings["models"][role], "Call belongs to another role"
        )
        wire = {k: v for k, v in kwargs.items() if k != "num_retries" and v is not None}
        if role == "user_simulator":
            wire.pop("seed")
        wire.update(stream=False, provider=settings["routes"][role])
        if role == "worker" and policy.enabled:
            wire = request_with_past_memory(
                wire, policy, items, instruction=instruction
            )
        archived = blobs.get(start["request_ref"])
        require(
            canonical(wire) == canonical(archived),
            f"{run} {role} request {cursor} differs from clean construction",
        )
        counts[role + "_requests"] += 1
        first = role not in openings
        if first:
            openings[role] = {"request": start["request_ref"]["sha256"]}
        end = finishes.get(start["request_id"])
        if not end or end["status"] != "response":
            require(
                cursor == len(starts) and cell["official"] is None,
                "Response absent before end of scored dialogue",
            )
            raise ArchivedStop
        raw = blobs.get(end["response_ref"])
        generation_ids.append(raw["id"])
        if first:
            reply = raw["choices"][0]["message"]
            calls = [
                (c["function"]["name"], json.loads(c["function"]["arguments"]))
                for c in reply.get("tool_calls") or []
            ]
            openings[role]["reply"] = digest(canonical([reply.get("content"), calls]))
        counts[role + "_responses"] += 1
        cached = ((raw.get("usage") or {}).get("prompt_tokens_details") or {}).get(
            "cached_tokens"
        )
        if cached is not None:
            counts[role + "_reported_cache"] += 1
            counts[role + "_positive_prompt_cache"] += int(cached > 0)
        return ModelResponse(**raw)

    with (
        patch.object(llm_utils, "completion", completion),
        patch.object(llm_utils, "get_response_cost", return_value=0),
    ):
        try:
            replayed = orch.run()
        except ArchivedStop:
            replayed = None
    require(cursor == len(starts), f"{run} has unconsumed requests")
    order_differences = []
    if cell["official"] is not None:
        original = read_json(
            verified(safe_path(workspace, cell["report"]), cell["report_sha256"])
        )
        require(
            replayed is not None
            and original["provider_session_id"] is None
            and replayed.termination_reason.value == original["termination_reason"]
            and orch.call_context.language.model_dump(mode="json")
            == original["language_context"],
            f"{run} historical role context or termination differs",
        )
        turns = public_turns([m.model_dump(mode="json") for m in orch.trajectory])
        saved = public_turns(original["messages"])
        require(
            Counter(map(canonical, turns)) == Counter(map(canonical, saved)),
            f"{run} dialogue or tool output differs in content",
        )
        for i, (actual, published) in enumerate(zip(turns, saved, strict=True)):
            if actual != published:
                order_differences.append(
                    {
                        "position": i,
                        "executed_role": actual["role"],
                        "saved_role": published["role"],
                        "executed_message_sha256": digest(canonical(actual)),
                        "saved_message_sha256": digest(canonical(published)),
                        "saved_timestamp": original["messages"][i]["timestamp"],
                    }
                )
        db = list(state_hashes(orch.environment))
        require(
            all(
                (db == r["target"]) == cell["grade"]["db_match"]
                for r in references.values()
            ),
            "Final DB grade differs",
        )
        counts["matched_graded_trajectories"] += 1
        counts["saved_turn_order_differs"] += bool(order_differences)
        counts["matched_tool_replies"] += sum(
            m["role"] == "tool" for m in original["messages"]
        )
    return {
        "task": task["id"],
        "stage": cell["stage"],
        "arm": arm,
        "run": run,
        "session": cell["session"],
        "official": cell["official"],
        "counts": counts,
        "openings": openings,
        "saved_order_differences": order_differences,
        "clock_reversals": clock_reversals if order_differences else [],
        "native_nl_assertions": len(cell.get("grade", {}).get("nl", [])),
        "generations": generation_ids,
        "persona": digest(
            canonical(orch.call_context.language.model_dump(mode="json")["persona"])
        ),
    }


def replay_task(args):
    workspace, cells, task, references, settings = args
    banks = {c["arm"]: c for c in cells if c["stage"] == "write"}
    with (
        patch(
            "socket.socket.connect", side_effect=AssertionError("Offline audit")
        ) as connect,
        patch(
            "socket.socket.connect_ex", side_effect=AssertionError("Offline audit")
        ) as connect_ex,
    ):
        audited = [
            replay_episode(
                workspace, cell, task, references, banks.get(cell["arm"]), settings
            )
            for cell in cells
            if cell["stage"] != "write"
        ]
        require(
            connect.call_count == connect_ex.call_count == 0,
            "Audit attempted network access",
        )
    return audited


def load_tasks(workspace):
    cfg, base, conn, rows, tasks = prepare(workspace)
    parent, expansion = [
        read_json(verified(workspace / "artifacts/analyses" / n, INPUTS[n]))
        for n in (PARENT, DETAIL)
    ]
    require(expansion["rows"] == rows, "Expansion row mismatch")
    cells = parent["cells"] + expansion["new_cells"]
    require(
        len(cells)
        == len({c["session"] for c in cells})
        == len({c["run"] for c in cells})
        == 1518,
        "Reused run or session",
    )
    by_task = defaultdict(list)
    for c in cells:
        by_task[c["task"]].append(c)
    require(len(set(cfg["solver_seeds"])) == 1, "Unexpected solver seeds")
    roles = ["worker", "user_simulator"]
    settings = {
        "instruction": asset_text(ROOT, base["second_pass"]["solve_instruction_ref"]),
        "source_seed": cfg["source_seed"],
        "solver_seed": cfg["solver_seeds"][0],
        "models": {r: conn[r]["model_id"] for r in roles},
        "routes": {r: provider_route(conn, r) for r in roles},
        "parameters": {r: conn[r]["parameters"] for r in roles},
    }
    groups = [
        (workspace, by_task[k], t, expansion["reference_databases"][k], settings)
        for k, t in tasks.items()
    ]
    return rows, groups


def compare_groups(rows, episodes):
    index = {(e["task"], e["stage"], e["arm"]): e for e in episodes}
    require(
        len(index) == len(episodes) == 1254,
        "Unexpected source or retry allocation count",
    )
    arms = sorted({arm for _, _, arm in index} - set(CONTROLS))
    comparisons = defaultdict(list)
    openings, variability, by_persona, same_graded = (
        Counter(),
        Counter(),
        defaultdict(Counter),
        Counter(),
    )
    matched_scores, task_differences = Counter(), defaultdict(list)
    for row in rows:
        task, customer = row["task"], row["customer"]
        first = index[task, "source", "no_memory"]
        retries = [index[task, stage, "no_memory"] for stage in ROUNDS]
        scores = [r["official"] for r in retries]
        if None not in scores:
            variability["complete_tasks"] += 1
            variability["tasks_with_different_outcomes"] += len(set(scores)) > 1
        for left, right in combinations(retries, 2):
            a, b = (
                left["openings"].get("worker", {}),
                right["openings"].get("worker", {}),
            )
            if "reply" in a and "reply" in b and a["request"] == b["request"]:
                variability["pairs_with_identical_first_worker_request"] += 1
                variability["different_first_worker_reply"] += a["reply"] != b["reply"]
        retry_prompt_ids = set()
        for stage in ROUNDS:
            pure, empty = (index[task, stage, a] for a in CONTROLS)
            forms = [index[task, stage, a] for a in arms]
            block = [pure, empty, *forms]
            retry_prompt_ids.update(
                e["openings"]["user_simulator"]["request"] for e in block
            )
            opens = [e["openings"]["user_simulator"] for e in (pure, empty)]
            if all("reply" in o for o in opens):
                openings["paired_simulator_replies"] += 1
                openings["different_first_customer_reply"] += (
                    opens[0]["reply"] != opens[1]["reply"]
                )
            for e in [empty, *forms]:
                pair = {
                    "task": task,
                    "customer": customer,
                    "state": status(pure["official"], e["official"]),
                }
                comparisons[e["arm"]].append(pair)
            by_persona["same" if pure["persona"] == first["persona"] else "different"][
                status(pure["official"], first["official"])
            ] += 1
            if all(e["official"] is not None for e in block):
                same_graded["blocks"] += 1
                for e in block:
                    matched_scores[e["arm"]] += int(e["official"])
                matched_scores["source"] += int(first["official"])
                task_differences[customer, task].append(
                    int(pure["official"]) - mean(int(e["official"]) for e in forms)
                )
        require(
            len(retry_prompt_ids) == 1,
            "Different initial customer prompt within the same retry task",
        )
    customer_differences = defaultdict(list)
    for (customer, _), scores in task_differences.items():
        customer_differences[customer].append(mean(scores))
    clustered = [mean(v) for _, v in sorted(customer_differences.items())]
    return {
        "no_memory_minus_other": {
            arm: paired_effect(pairs, interval=True)
            for arm, pairs in comparisons.items()
        },
        "complete_six_condition_blocks": dict(
            **same_graded,
            successes=matched_scores,
            tasks=len(task_differences),
            customers=len(clustered),
            no_memory_minus_four_form_customer_average=mean(clustered),
            customer_interval=bootstrap_customer_interval(clustered),
        ),
        "no_memory_retries_by_source_persona": by_persona,
        "no_memory_repeat_variation": variability,
        "no_memory_and_empty_openings": openings,
    }


def summarize(rows, episodes):
    counts = defaultdict(Counter)
    for e in episodes:
        arm = "first_pass" if e["stage"] == "source" else e["arm"]
        counts[arm]["episodes"] += 1
        counts[arm].update(e["counts"])
    ids = [g for e in episodes for g in e["generations"]]
    require(len(ids) == len(set(ids)), "Duplicate response ID across archived calls")
    footprints = {
        row["task"]: digest(
            canonical(
                [
                    {k: v for k, v in e.items() if k != "generations"}
                    for e in episodes
                    if e["task"] == row["task"]
                ]
            )
        )
        for row in rows
    }
    return {
        "study": "Retail session isolation and paired control audit",
        "inputs": {n: INPUTS[n] for n in (PARENT, DETAIL)},
        "allocations_including_writers": 1518,
        "replayed_episodes": len(episodes),
        "unique_recorded_actor_responses": len(ids),
        "by_condition": counts,
        "saved_report_turn_order": [
            {
                k: e[k]
                for k in (
                    "run",
                    "task",
                    "stage",
                    "arm",
                    "official",
                    "saved_order_differences",
                    "clock_reversals",
                    "native_nl_assertions",
                )
            }
            for e in episodes
            if e["saved_order_differences"]
        ],
        "paired": compare_groups(rows, episodes),
        "task_replay_sha256": footprints,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output file")
    rows, groups = load_tasks(args.workspace.resolve())
    with ProcessPoolExecutor(
        max_workers=args.workers, mp_context=get_context("fork")
    ) as pool:
        episodes = [e for group in pool.map(replay_task, groups) for e in group]
    result = summarize(rows, episodes)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    print(
        f"Matched {len(episodes)} episodes and {result['unique_recorded_actor_responses']} recorded actor responses"
    )


if __name__ == "__main__":
    main()
