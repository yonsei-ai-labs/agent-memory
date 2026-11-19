"""Reanalyse existing memory trials by customer, exposure, candidate reads and public disclosures."""

import argparse
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from unittest.mock import patch

from analyze_multi_workflow import transition

from agent_memory_eval.adapters.tau2 import native_modules, verify_checkout
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import FORMS, write_json
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.experiments.fresh_forms import scenario_customer
from agent_memory_eval.recording.assets import asset_text

ANALYSES = Path("artifacts/analyses")
RESULTS = {
    "multi-workflow-results": "623c1d88ac2116ca3cd65f56edb3769d1b91e28e5c7ad8c561850123bf9932eb",
    "multi-workflow-profile-results": "82ec0625237021cb9661274aedc74333641fadb2dea60c1a65309a673e5fa3c6",
    "multi-workflow-baggage-replay": "a770380d06ff19ce4a672830578d11083955d24a1e24581236dbc691adff1dc1",
    "fixed-bank-repeats-results": "6c5c05652a432fb54fc006f069510c79451a78fa4edc859cd290f61f4da28067",
    "frozen-bank-preflight": "898da02d03f118991c6f5fb2850d38f42fc90a20b5ee81bc17acc1830866fa26",
    "bank-transfer-results": "bb2fdf817b3aa48d3560340e2c930de70d16c142a41650e606fd46d23ff3c035",
    "scope-lessons-results": "be813aefb88eb3023a1ecd7e36d2cba963d62bd39d70634f1d399a844594034e",
}
SOURCES = {
    "protocols/pilots/tau2-new-customer-forms-v1.json": "0a73306f4ccb6f6a2e7a648c02b70e4fef274f2a7574e9a136dac06132c2062e",
    "artifacts/pilots/20261115-tau2-baseline-screening-results.json": "38239cc8896f53bc805eb4287d8e1be9561b3bb0bd857c9d89674620c9693487",
    "runs/tau2-new-customer-source-01/completion.json": "bf8dde82280789623855a81d5ed860e7924996365037852912cc3bcba80a53ec",
    "runs/tau2-multi-workflow-native-01/completion.json": "3e25a62daab301ad103ba1fabe4fa3276b91e6ad9601b5fd2789292df453ef1d",
    "protocols/pilots/tau2-multi-workflow-profile-step-v1.json": "ff82c9050a01178339b12cc4aba29c145c7b9e3698238a35bb082d8cbc4fcd55",
}
TASKS = ("3", "14", "42")
CONTROLS = ("no_memory", "empty_preload")
# Code the customer's express statement before the first booking proposal. Generic assent is not an express number.
CATEGORIES = (
    "zero_checked",
    "extra_or_allowance_only",
    "explicit_positive",
    "unresolved_conflict",
    "no_quantity_statement",
)


def pinned_json(workspace, path, checksum=None):
    return read_json(asset_text(workspace, {"path": str(path), "sha256": checksum or SOURCES[str(path)]}).encode())


def result_file(workspace, key):
    filename = ANALYSES / ("20261119-" + key + ".json")
    return pinned_json(workspace, filename, RESULTS[key])


def customer_prefix_and_proposals(messages):
    """End the masked customer transcript at the first booking attempt, including a rejected attempt."""
    proposals = [
        (turn, call["arguments"]["total_baggages"])
        for turn, message in enumerate(messages)
        if message["role"] == "assistant"
        for call in message.get("tool_calls") or []
        if call["name"] == "book_reservation"
    ]
    first_turn = proposals[0][0] if proposals else None
    words = [m["content"] for m in messages[:first_turn] if m["role"] == "user" and m.get("content")]
    return words, first_turn, [quantity for _, quantity in proposals]


def booking_wording(workspace, native):
    packet, cases = {}, []
    for e in native:
        if e["task_id"] != "14":
            continue
        graded = "grade" in e
        path = e["report_path"] if graded else e["partial_path"]
        sha = e["grade"]["report_sha256"] if graded else Path(path).name
        report = pinned_json(workspace, path, sha)
        messages = report["messages"] if isinstance(report, dict) else report
        words, first_turn, quantities = customer_prefix_and_proposals(messages)
        key = digest(canonical(words))
        packet[key] = words
        cases.append(
            {
                "index": e["index"],
                "arm": e["arm"],
                "review_id": key,
                "report_sha256": sha,
                "graded": graded,
                "first_booking_turn": first_turn,
                "proposed_quantities": quantities,
            }
        )
    if len(cases) != 40 or len({r["index"] for r in cases}) != len(cases):
        raise ContractError("The public wording review must include all 40 booking assignments")
    return dict(sorted(packet.items())), cases


def join_wording(packet, cases, labels, interventions):
    if labels.keys() != packet.keys():
        raise ContractError("Missing or extra masked customer transcript")
    original = {r["index"]: r for r in interventions["rows"]}
    if len(original) != len(interventions["rows"]) or original.keys() != {r["index"] for r in cases}:
        raise ContractError("Original DB replay population changed")
    counts = defaultdict(Counter)
    for row in cases:
        label = labels[row["review_id"]]
        category, quotes = label["category"], label["evidence"]
        if (
            category not in CATEGORIES
            or not isinstance(quotes, list)
            or bool(quotes) != (category != "no_quantity_statement")
        ):
            raise ContractError("Unknown wording code or missing supporting customer quotation")
        words = packet[row["review_id"]]
        for q in quotes:
            if (
                type(q["utterance"]) is not int
                or not 0 <= q["utterance"] < len(words)
                or not q["quote"]
                or q["quote"] not in words[q["utterance"]]
            ):
                raise ContractError("Wording label does not cite the blinded public prefix")
        values = row["proposed_quantities"]
        if values:
            outcome = "first_proposed_" + str(values[0])
        else:
            outcome = "no_proposal_before_end" if row["graded"] else "partial_no_proposal"
        row.update(wording=category, first_proposal_outcome=outcome)
        counterfactual = original[row["index"]]
        if row["graded"] and counterfactual["native_report_sha256"] != row["report_sha256"]:
            raise ContractError("Wording and DB counterfactual use different transcripts")
        if row["arm"] != counterfactual["arm"]:
            raise ContractError("The intervention arm does not match the wording case")
        if counterfactual["status"] == "replayed":
            row.update(
                executed_quantity=counterfactual["old"],
                db_before=counterfactual["recorded_db_match"],
                db_after_quantity_change=counterfactual["edited_db_match"],
            )
        counts[category][outcome] += 1
    replayed = [r for r in cases if "executed_quantity" in r]
    return {
        "wording_by_first_proposal": dict(counts),
        "cases": cases,
        "labels_sha256": digest(canonical(labels)),
        "customer_utterances": sum(len(packet[r["review_id"]]) for r in cases),
        "proposed_with_execution": sum(bool(r["proposed_quantities"]) for r in replayed),
        "proposals_all_match_execution": sum(
            bool(r["proposed_quantities"])
            and all(quantity == r["executed_quantity"] for quantity in r["proposed_quantities"])
            for r in replayed
        ),
        "db_repairs_by_wording": dict(
            Counter(r["wording"] for r in replayed if not r["db_before"] and r["db_after_quantity_change"])
        ),
    }


def condition_rows(episodes, *, suffix):
    cells = {}
    for e in episodes:
        task, arm = e["task_id"], e["arm"]
        if task not in TASKS or (suffix and arm not in CONTROLS and not arm.endswith(suffix)):
            continue
        form = arm.removesuffix(suffix) if suffix else arm
        if form not in (*FORMS, *CONTROLS):
            raise ContractError("Unexpected recorded condition")
        key = (task, e["writer_block"], e["solver_seed"], form)
        if key in cells:
            raise ContractError("Duplicated task, writer, solver and condition")
        cells[key] = e
    return cells


def summarize_blocks(blocks):
    summary = {}
    scores = [block["scores"] for block in blocks]
    for form in (*FORMS, *CONTROLS):
        paired = {
            control: dict(Counter(transition(row[form], row[control]) for row in scores)) for control in CONTROLS
        }
        both_controls = Counter(
            transition(row[form], row["no_memory"])
            for row in scores
            if row["no_memory"] is not None and row["no_memory"] == row["empty_preload"]
        )
        summary[form] = {
            "success": sum(row[form] is True for row in scores),
            "graded": sum(row[form] is not None for row in scores),
            "paired": paired,
            "versus_concordant_controls": dict(both_controls),
        }
    return summary


def study_instruction(workspace, study_path, seal):
    instructions = {}
    for task in TASKS:
        for wave in (1, 2):
            shard = f"task-{int(task):02d}-wave-{wave}"
            control = pinned_json(
                workspace, study_path / shard / "control.json", seal["shards"][shard]["control_sha256"]
            )
            ref = control["design"]["second_pass"]["solve_instruction_ref"]
            asset_text(workspace, ref)
            instructions[ref["sha256"]] = ref
    if len(instructions) != 1:
        raise ContractError("Worker guidance is not constant within the historical comparison")
    return next(iter(instructions.values()))


def repeat_comparison(workspace, prior, latest, inventory):
    old = condition_rows(prior["episodes"], suffix="")
    new = condition_rows(latest["native_evidence"], suffix="__preload")
    banks = {(b["task"], b["writer"], b["arm"]): b for b in inventory["banks"] if b["task"] in TASKS}
    links = {(b["task"], b["writer"], b["form"]): b for b in latest["bank_links"]}
    if banks.keys() != links.keys() or len(banks) != 24:
        raise ContractError("The two cohorts do not contain the same 24 original banks")
    for key, link in links.items():
        b = banks[key]
        if (b["run"], b["items_sha256"]) != (link["writer_run"], link["items_sha256"]):
            raise ContractError("The matched saved content differs")
    results = {}
    source_outcomes = latest["native"]["first_pass_success"]
    if source_outcomes != {
        r["task_id"]: r["first_pass_success"] for r in inventory["tasks"] if r["task_id"] in TASKS
    }:
        raise ContractError("The matched source runs disagree about first-pass outcomes")
    for name, cells, seeds, source, seal_hash in [
        ("earlier", old, (4, 5), prior["study"], prior["completion_sha256"]),
        ("latest", new, (6, 7), latest["native"]["study"], None),
    ]:
        study_path = Path("runs") / source
        seal = pinned_json(workspace, study_path / "completion.json", seal_hash)
        block_keys = [(t, w, s) for t in TASKS for w in (1, 2) for s in seeds]
        if set(cells) != {(*k, f) for k in block_keys for f in (*FORMS, *CONTROLS)}:
            raise ContractError("Unequal assignment when restricting the two studies to common tasks")
        blocks = []
        for key in block_keys:
            t, w, s = key
            scores = {f: cells[*key, f]["official_success"] for f in (*FORMS, *CONTROLS)}
            for f in FORMS:
                if cells[*key, f]["source_writer_run_id"] != banks[t, w, f]["run"]:
                    raise ContractError("Episode points to a different source bank")
            blocks.append({"task": t, "writer": w, "seed": s, "scores": scores})
        strata = {
            "all": blocks,
            **{t: [r for r in blocks if r["task"] == t] for t in TASKS},
            "source_passed": [r for r in blocks if source_outcomes[r["task"]]],
            "source_failed": [r for r in blocks if not source_outcomes[r["task"]]],
        }
        results[name] = {
            "study": source,
            "seeds": list(seeds),
            "instruction": study_instruction(workspace, study_path, seal),
            "first_pass_success": source_outcomes,
            "cells": blocks,
            "summary": {stratum: summarize_blocks(subset) for stratum, subset in strata.items()},
        }
    return results


def unread_variation(episodes):
    groups = defaultdict(list)
    for e in episodes:
        if e["mode"] == "on_demand":
            if any(
                e[k]
                for k in ("list_requests", "get_requests", "list_returns", "get_returns", "title_spans", "body_spans")
            ):
                raise ContractError("A supposedly unread arm actually requested or received memory")
            groups[e["task_id"], e["writer_block"], e["solver_seed"]].append(e)
    counts, blocks = Counter(), []
    for (task, writer, seed), block in sorted(groups.items()):
        if sorted(e["form"] for e in block) != sorted(FORMS):
            raise ContractError("Incomplete allocation of four voluntarily unread banks")
        scores = {e["form"]: e["official_success"] for e in block}
        pairs = Counter(
            "missing" if None in (a, b) else "agree" if a == b else "discordant"
            for a, b in combinations(scores.values(), 2)
        )
        counts.update(pairs)
        blocks.append(
            {
                "task": task,
                "writer": writer,
                "seed": seed,
                "scores": scores,
                "mixed": True in scores.values() and False in scores.values(),
                "pairs": dict(pairs),
            }
        )
    return {"blocks": blocks, "mixed_blocks": sum(b["mixed"] for b in blocks), "shared_pair_counts": dict(counts)}


def profile_choices(workspace, raw):
    rule = pinned_json(workspace, "protocols/pilots/tau2-multi-workflow-profile-step-v1.json")["cases"][
        "after_profile"
    ]
    target = rule["expected_id"]
    summaries = defaultdict(Counter)
    banks = []
    for arm, b in sorted(raw["first_pass_evidence"]["banks"].items()):
        rows = [r for r in raw["rows"] if r["arm"] == arm]
        if len(rows) != 2 or any(r["score"] is None for r in rows):
            raise ContractError("Expected two observed decisions for each masked writer block")
        f = rows[0]["form"]
        q = summaries[f]
        q["writer_blocks"] += 1
        q["saved_nonempty"] += bool(b["items"])
        results = []
        for r in rows:
            wire_hash = r["wire_sha256"]
            repeat_path = Path("runs") / raw["study"] / f"repeat-{r['repeat']:02d}"
            wire = pinned_json(workspace, repeat_path / "blobs/sha256" / wire_hash, wire_hash)
            memory_message = wire["messages"][1]
            payload = read_json(memory_message["content"].encode())
            if memory_message["role"] != "user" or payload != {"items": b["items"]}:
                raise ContractError("The saved bank differs from the recorded memory input")
            score = r["score"]
            ids = score["requested_ids"]
            if len(ids) != len(score["calls"]) or len(ids) != len(set(ids)) or set(ids) - set(rule["candidates"]):
                raise ContractError("First-reply candidate metrics need distinct, in-profile lookups")
            q.update(
                replies=1,
                direct=int(score["strict"]),
                target_in_batch=int(target in ids),
                business_reads_proposed=len(ids),
                non_target_reads_proposed=sum(i != target for i in ids),
            )
            results.append(
                {
                    "slot": r["slot"],
                    "wire_sha256": wire_hash,
                    "direct": score["strict"],
                    "target_in_batch": target in ids,
                    "requested_count": len(ids),
                }
            )
        banks.append(
            {
                "arm": arm,
                "form": f,
                "writer": rows[0]["writer"],
                "writer_run": b["writer_run"],
                "items_sha256": b["items_sha256"],
                "target_in_title": any(target in i["title"] for i in b["items"]),
                "target_in_body": any(target in i["body"] for i in b["items"]),
                "results": results,
            }
        )
    return {
        "summary": dict(summaries),
        "banks": banks,
        "candidates": len(rule["candidates"]),
        "target_position": rule["candidates"].index(target) + 1,
    }


def coverage(workspace, banks):
    """Count past assignments by customer without consulting any score or target answer."""
    protocol = pinned_json(workspace, "protocols/pilots/tau2-new-customer-forms-v1.json")
    airline = workspace / "data/tau2-bench/data/tau2/domains/airline"
    assets = {name: pinned_json(airline, name, checksum) for name, checksum in protocol["native_assets"].items()}
    splits = assets["split_tasks.json"]
    customers = {task["id"]: scenario_customer(task, assets["db.json"]) for task in assets["tasks.json"]}
    if set(splits["train"]) & set(splits["test"]) or set(splits["train"] + splits["test"]) != customers.keys():
        raise ContractError("The published division of customer tasks is incomplete")

    baseline = pinned_json(workspace, "artifacts/pilots/20261115-tau2-baseline-screening-results.json")
    source_path = Path("runs/tau2-new-customer-source-01")
    seal = pinned_json(workspace, source_path / "completion.json")
    source = pinned_json(workspace, source_path / "allocations.json", seal["allocation_sha256"])
    cohorts = {
        "baseline_screening": {r["task_id"] for r in baseline["tasks"] if r["started"] > 0},
        "new_customer_source": {r["task_id"] for r in source if r["result"]["http_calls"] > 0},
    }
    for key in ("bank-transfer-results", "scope-lessons-results"):
        episodes = result_file(workspace, key)["episodes"]
        cohorts[key] = {e["task_id"] for e in episodes if e["http_calls"] > 0}
    observed = set.union(*cohorts.values())
    seen = {customers[t] for t in observed}
    remaining = sorted(set(splits["test"]) - observed, key=int)
    writer_slots = defaultdict(set)
    for bank in banks:
        writer_slots[bank["task"]].add((bank["arm"], bank["writer"]))
    eight_writers = {(form, writer) for form in FORMS for writer in (1, 2)}
    candidates = []
    for task in remaining:
        sources = sorted((t for t in observed if customers[t] == customers[task]), key=int)
        candidates.append(
            {
                "task": task,
                "customer_group": "customer-" + digest(customers[task].encode())[:12],
                "customer_seen": bool(sources),
                "observed_same_customer_tasks": sources,
                "same_customer_sources_in_fixed_bank_inventory": [
                    t for t in sources if writer_slots[t] == eight_writers
                ],
            }
        )
    return {
        "official_tasks": len(customers),
        "split_sizes": {k: len(splits[k]) for k in ("train", "test")},
        "official_customers": len(set(customers.values())),
        "cohorts": {k: sorted(v, key=int) for k, v in cohorts.items()},
        "observed_distinct_tasks": len(observed),
        "observed_distinct_customers": len(seen),
        "test_tasks_outside_four_cohorts": candidates,
    }


def no_service_grade(task, request):
    from tau2.data_model.message import AssistantMessage, UserMessage
    from tau2.data_model.semantic_communication import CommunicateScoring
    from tau2.data_model.simulation import SimulationRun, TerminationReason
    from tau2.data_model.tasks import Task
    from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation

    reply = "Goodbye."
    sim = SimulationRun(
        id="synthetic-grader-control-" + task["id"],
        task_id=task["id"],
        duration=0,
        start_time="2026-11-19T00:00:00Z",
        end_time="2026-11-19T00:00:00Z",
        termination_reason=TerminationReason.AGENT_STOP,
        messages=[UserMessage(role="user", content=request), AssistantMessage(role="assistant", content=reply)],
    )
    grade = evaluate_simulation(
        simulation=sim,
        task=Task.model_validate(task),
        evaluation_type=EvaluationType.ALL,
        solo_mode=False,
        domain="airline",
        strict_replay=True,
        communicate_scoring=CommunicateScoring(scorer="substring"),
    )
    return {
        "request": request,
        "reply": reply,
        "termination": sim.termination_reason.value,
        "tool_calls": 0,
        "official_reward": grade.reward,
        "db_match": grade.db_check.db_match,
        "reward_breakdown": {k.value: v for k, v in grade.reward_breakdown.items()},
    }


def source_writer_mentions(parent, writers, banks, source, previous_id):
    mentions = []
    for bank in banks:
        if bank["task"] != source:
            continue
        assignment = writers[bank["job"]]
        saved = read_form_bank(
            parent / assignment["directory"], expected_log_sha256=assignment["result"]["log_sha256"]
        )
        if digest(canonical(saved)) != bank["bank_sha256"]:
            raise ContractError("Candidate writer content differs from the original inventory")
        mentions.append(
            {
                "writer_run": saved["run_id"],
                "form": bank["arm"],
                "writer": bank["writer"],
                "past_id_in_title_or_body": any(
                    previous_id in item[key] for item in saved["items"] for key in ("title", "body")
                ),
            }
        )
    return mentions


def candidate_checks(workspace, inventory, census):
    """Inspect grading only after selecting same-customer pairs from the independent assignment census."""
    requests = {
        ("17", "22"): "Please change the passenger to me, upgrade my trip to economy, and add three checked bags.",
        ("3", "48"): "Please cancel my reservation and give me a full refund.",
    }
    selected = {
        (source, r["task"])
        for r in census["test_tasks_outside_four_cohorts"]
        for source in r["same_customer_sources_in_fixed_bank_inventory"]
    }
    if selected != requests.keys():
        raise ContractError(
            "The grader controls must match the candidates selected from the bank and customer inventories"
        )

    parent = Path("runs") / inventory["parent"]
    seal = pinned_json(workspace, parent / "completion.json", inventory["parent_completion_sha256"])
    writers = {r["index"]: r for r in pinned_json(workspace, parent / "allocations.json", seal["allocation_sha256"])}
    protocol = pinned_json(workspace, "protocols/pilots/tau2-new-customer-forms-v1.json")
    checkout = verify_checkout(workspace / "data/tau2-bench")
    assets = checkout / "data/tau2/domains/airline"
    tasks = {t["id"]: t for t in pinned_json(assets, "tasks.json", protocol["native_assets"]["tasks.json"])}
    rows = []
    with patch(
        "socket.socket.connect", side_effect=AssertionError("Unexpected network access in an offline grader control")
    ):
        native_modules(checkout)
        for (source, target), request in requests.items():
            original = tasks[source]["evaluation_criteria"]["actions"]
            criteria = tasks[target]["evaluation_criteria"]
            if criteria["reward_basis"] != ["DB", "COMMUNICATE"]:
                raise ContractError("Unexpected official scoring basis for the two diagnostic targets")
            targets_by_stage = [
                {a["arguments"]["reservation_id"] for a in actions if "reservation_id" in a["arguments"]}
                for actions in (original, criteria["actions"])
            ]
            if any(len(ids) != 1 for ids in targets_by_stage):
                raise ContractError("Ambiguous target in the candidate reference actions")
            previous_id = next(iter(targets_by_stage[0]))
            action_sequences = [
                [(a["name"], a["arguments"]) for a in actions] for actions in (original, criteria["actions"])
            ]
            rows.append(
                {
                    "source_task": source,
                    "target_task": target,
                    "source_writers": source_writer_mentions(
                        workspace / parent, writers, inventory["banks"], source, previous_id
                    ),
                    "source_success": next(
                        t["first_pass_success"] for t in inventory["tasks"] if t["task_id"] == source
                    ),
                    "same_reference_reservation": targets_by_stage[0] == targets_by_stage[1],
                    "same_reference_calls_and_arguments": action_sequences[0] == action_sequences[1],
                    "target_reference_writes": [
                        a["name"] for a in criteria["actions"] if a["name"] in protocol["write_tools"]
                    ],
                    "target_required_communicate_count": len(criteria["communicate_info"]),
                    "synthetic_no_service": no_service_grade(tasks[target], request),
                }
            )
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["packet", "review"])
    p.add_argument("--workspace", type=Path, default=ROOT)
    p.add_argument("--labels", type=Path)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists() or (args.mode == "review") != bool(args.labels):
        p.error("Choose a new output and supply masked labels only for the review")
    workspace = args.workspace.resolve()
    latest = result_file(workspace, "multi-workflow-results")
    packet, cases = booking_wording(workspace, latest["native_evidence"])
    result = packet
    if args.mode == "review":
        inventory = result_file(workspace, "frozen-bank-preflight")
        census = coverage(workspace, inventory["banks"])
        result = {
            "inputs": {**{str(ANALYSES / ("20261119-" + k + ".json")): v for k, v in RESULTS.items()}, **SOURCES},
            "common_banks": repeat_comparison(
                workspace,
                result_file(workspace, "fixed-bank-repeats-results"),
                latest,
                inventory,
            ),
            "unread": unread_variation(latest["native_evidence"]),
            "profile": profile_choices(workspace, result_file(workspace, "multi-workflow-profile-results")),
            "baggage": join_wording(
                packet,
                cases,
                read_json(args.labels.read_bytes()),
                result_file(workspace, "multi-workflow-baggage-replay"),
            ),
            "benchmark_coverage": census,
            "candidate_diagnostics": candidate_checks(workspace, inventory, census),
        }
    write_json(args.output, result)


if __name__ == "__main__":
    main()
