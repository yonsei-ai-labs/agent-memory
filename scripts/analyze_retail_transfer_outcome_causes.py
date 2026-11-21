"""Trace the retail retest's matched outcomes back to note exposure, transactions and handoffs."""

import argparse
import json
import re
from collections import Counter, defaultdict
from functools import cache
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.journal import BlobStore, safe_path
from scripts.analyze_full_retail import require
from scripts.analyze_recheck_rounds import MUTATIONS, mutation_key, status
from scripts.analyze_retail_full_factors import (
    business_events,
    first_difference,
    public_message,
    transfer_position,
    verified,
)
from scripts.analyze_retail_retry_gains import make_replayer
from scripts.audit_retail_transfer_writing import role_calls
from scripts.replay_full_retail_fields import state_hashes
from scripts.run_full_retail import modules_for
from scripts.run_recheck_rounds import fixed

FROZEN = {
    "results": (
        "artifacts/analyses/20261121-retail-transfer-revision-results.json",
        "e3a25bf8d0f9ce2392bdc50ba3cc776015e48e76bc71498b71a37b20fe1afa58",
    ),
    "previous": (
        "artifacts/analyses/20261121-retail-related-transfer-results.json",
        "faf315af21ff984d5760fd709bce9519a42c9fd9d23dd377e9dd83d9cd260c84",
    ),
    "sources": (
        "artifacts/analyses/20261121-retail-related-transfer-preflight.json",
        "d3e02d03621ae092d521791a7311b262c8b5c5cc4f7994ade5bc06efc4944aa6",
    ),
    "tasks": (
        "data/tau2-bench/data/tau2/domains/retail/tasks.json",
        "8e03ebce7901bd6218e7a7dc3105faa9324091a68058f7fe61c65262868812e8",
    ),
}
AMOUNT = re.compile(r"(?<![\d.])(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?!\d)")
HANDOFF_WORDS = re.compile(
    r"\b(?:escalat\w*|transfer(?:s|red|ring)?|hand[ -]?offs?|human (?:agents?|support|representatives?))\b",
    re.IGNORECASE,
)
# Selected after reading the discordant conversations. Locate the public claim in the
# precise worker response and the matching catalog entry already present in its input.
CATALOG_CASES = {
    ("29", "27", "episode_explicit"): ("shortest available bamboo option", "8176740019"),
    ("99", "53", "fact_explicit"): ("isn’t an available camera variant", "8363011723"),
    ("99", "51", "lesson_explicit"): ("option exists but is **unavailable**", "5546244844"),
    ("99", "51", "procedure_explicit"): ("no available lower-resolution camera", "8363011723"),
}


def spoken(message):
    text = message.get("content") or ""
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return text
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            return payload["message"]
    return text


def amounts(text):
    return {v.replace(",", "") for v in AMOUNT.findall(text)}


def native_reader(workspace, cells):
    @cache
    def read(run):
        cell = cells[run]
        path = safe_path(workspace, cell["journal"])
        (scored,) = (
            e["payload"]
            for line in verified(path, cell["journal_sha256"]).splitlines()
            for e in read_json(line)["events"]
            if e["type"] == "outcome.scored" and e["run_id"] == run
        )
        ref = scored["official_artifact_ref"]
        report = BlobStore(path.parent).get(ref)
        require(
            report["task_id"] == cell["task"]
            and scored["official_success"] is cell["official"]
            and digest(canonical(report["reward_info"])) == cell["grade"]["reward_sha256"],
            "The native report does not match the independently regraded episode",
        )
        updates, transfers = business_events(report["messages"])
        require(
            [{k: v for k, v in u.items() if k != "accepted"} for u in updates] == cell["writes"],
            "Recorded changes differ from the native turns",
        )
        return report, {
            "report_sha256": ref["sha256"],
            "handoff": transfer_position(updates, transfers),
            "handoff_turns": [t["turn"] for t in transfers],
        }

    return read


def handoffs(memory, pure):
    return {
        (False, False): "neither",
        (True, False): "memory_only",
        (False, True): "pure_only",
        (True, True): "both",
    }[memory["handoff"] != "no_transfer", pure["handoff"] != "no_transfer"]


def mutations(actions):
    return Counter(mutation_key(a["name"], a["arguments"]) for a in actions if a["name"] in MUTATIONS)


def repeated_source_actions(cell, source, target):
    observed = Counter(mutation_key(w["name"], w["args"]) for w in cell["writes"] if w["success"])
    return [read_json(key) for key in sorted(((observed - target) & (source - target)).elements())]


def grade_reason(cell):
    db = cell["grade"]["db_match"] is False
    language = any(not a["met"] for a in cell["grade"]["nl"])
    require(cell["official"] is False and (db or language), "Unexplained losing grade")
    return "db_and_language" if db and language else "db_only" if db else "language_only"


def amount_evidence(checks, messages):
    public = [
        (i, amounts(spoken(m)))
        for i, m in enumerate(messages)
        if m["role"] == "assistant" and not m.get("tool_calls")
    ]
    return [
        {
            "assertion": a["assertion"],
            "met": a["met"],
            "amount_turns": {
                n: [i for i, seen in public if n in seen] for n in sorted(amounts(a["assertion"]))
            },
        }
        for a in checks
    ]


def catalog_claim(workspace, cell, anchor, item, note):
    for _, wire, choice, response_hash in role_calls(workspace, cell, "worker"):
        if not choice:
            continue
        lines = [s.strip() for s in spoken(choice["message"]).splitlines() if anchor in s]
        if not lines:
            continue
        names = {
            c["id"]: c["function"]["name"]
            for m in wire["messages"]
            for c in (m.get("tool_calls") or [])
        }
        for m in wire["messages"]:
            if m["role"] == "tool" and names[m["tool_call_id"]] == "get_product_details":
                info = json.loads(m["content"])
                variant = info.get("variants", {}).get(item) if isinstance(info, dict) else None
                if variant and variant["available"] is True:
                    return {
                        "worker_reply": lines[0],
                        "variant_in_prior_tool_result": variant,
                        "variant_id_in_note": item in note,
                        "wire_sha256": digest(canonical(wire)),
                        "response_sha256": response_hash,
                    }
        raise ValueError(f"The reviewed variant was not available in the input of {cell['run']}")
    raise ValueError(f"The reviewed reply was not found in {cell['run']}")


def db_replay(workspace, tasks, references, native):
    modules_for(workspace)
    from tau2.data_model.language import LanguageTaskInfo
    from tau2.multilingual.runtime import language_environment_kwargs
    from tau2.registry import registry
    from tau2.runner.presets import reload_recorded_call

    @cache
    def bind(task, context):
        info = LanguageTaskInfo.model_validate_json(context)
        opts = language_environment_kwargs(reload_recorded_call(info, "retail")) or {}
        frozen = {
            "initial": state_hashes(registry.get_env_constructor("retail")(**opts)),
            "target": references[task]["end_db"],
            "errors": references[task]["errors"],
        }
        return make_replayer({task: tasks[task]}, {task: {digest(context): frozen}})

    def matches(run, calls):
        report, _ = native(run)
        task, context = report["task_id"], canonical(report["language_context"])
        outcome = bind(task, context)(task, context, canonical(calls))
        require(not outcome["failed_calls"], "An edited operation failed during DB replay")
        return outcome["match"]

    return matches


def action_stream(cell):
    require(all(w["success"] for w in cell["writes"]), "Unexpected failed action in replay case")
    return [(w["name"], "assistant", w["args"]) for w in cell["writes"]]


def replay_catalog(memory, pure, item, matches):
    """Change one replacement SKU, or add the missing single-item exchange, in both directions."""
    failed, passed = action_stream(memory), action_stream(pure)
    (index,) = [
        i
        for i, (name, _, a) in enumerate(passed)
        if name == "exchange_delivered_order_items" and item in a["new_item_ids"]
    ]
    name, _, args = passed[index]
    original_item = args["item_ids"][args["new_item_ids"].index(item)]
    same_order = [
        i for i, (n, _, a) in enumerate(failed) if n == name and a["order_id"] == args["order_id"]
    ]
    fixed_memory, changed_pure = failed.copy(), passed.copy()
    if same_order:
        (pos,) = same_order

        def replacement(call, new_id):
            tool, actor, a = call
            ids = a["new_item_ids"].copy()
            ids[a["item_ids"].index(original_item)] = new_id
            return tool, actor, {**a, "new_item_ids": ids}

        recorded = failed[pos][2]
        old_choice = recorded["new_item_ids"][recorded["item_ids"].index(original_item)]
        require(old_choice != item, "No difference in the catalog choice")
        fixed_memory[pos] = replacement(failed[pos], item)
        changed_pure[index] = replacement(passed[index], old_choice)
        edit = "replace_one_variant"
    else:
        require(args["new_item_ids"] == [item], "Missing exchange would also change another item")
        fixed_memory.append(passed[index])
        del changed_pure[index]
        edit = "add_omitted_single_item_exchange"
    require(
        not matches(memory["run"], failed) and matches(pure["run"], passed),
        "Catalog case baseline changed",
    )
    return {
        "edit": edit,
        "memory_db_with_correction": matches(memory["run"], fixed_memory),
        "pure_db_with_reverse_edit": matches(pure["run"], changed_pure),
    }


def replay_task_72(memory_runs, pure_run, native, cells, matches):
    """Exchange the observed item scope and payment route with the matched pure control."""
    streams, decisions = {}, {}
    for run in [pure_run, *memory_runs]:
        report, _ = native(run)
        calls, _ = business_events(report["messages"])
        streams[run] = action_stream(cells[run])
        (item_change,) = [w for w in calls if w["name"] == "modify_pending_order_items"]
        decisions[run] = {
            "item_change_turn": item_change["turn"],
            "customer_turns_before_change": [
                i
                for i, m in enumerate(report["messages"][: item_change["turn"]])
                if m["role"] == "user"
            ],
        }

    def substitute(calls, other, fields):
        (replacement,) = [args for name, _, args in other if name == "modify_pending_order_items"]
        return [
            (name, actor, {**args, **{field: replacement[field] for field in fields}})
            if name == "modify_pending_order_items"
            else (name, actor, args)
            for name, actor, args in calls
        ]

    control = streams[pure_run]
    require(matches(pure_run, control), "Original pure actions do not pass")
    (address,) = [c for c in control if c[0] == "modify_pending_order_address"]
    edits = {
        "scope_only": ["item_ids", "new_item_ids"],
        "payment_only": ["payment_method_id"],
        "scope_and_payment": ["item_ids", "new_item_ids", "payment_method_id"],
    }
    outcomes = []
    for run in memory_runs:
        actual = streams[run]
        require(not matches(run, actual), "Original note actions do not fail")
        revised = {
            label: matches(run, substitute(actual, control, fields)) for label, fields in edits.items()
        }
        both = edits["scope_and_payment"]
        corrected = substitute(actual, control, both)
        has_address = any(call[0] == address[0] for call in actual)
        outcomes.append(
            {
                "memory_run": run,
                "memory_original": False,
                "memory_with_pure_fields": revised,
                "address_missing": not has_address,
                "corrected_with_required_address": matches(
                    run, corrected if has_address else [address, *corrected]
                ),
                "pure_with_memory_scope_and_payment": matches(
                    pure_run, substitute(control, actual, both)
                ),
            }
        )
    return {"pure_run": pure_run, "pure_original": True, "turns": decisions, "cases": outcomes}


def control_sensitivity(rows, alternative):
    common = [r for r in rows if r[alternative] is not None]
    return {
        "matched": len(common),
        "distinct_tasks": len({r["task"] for r in common}),
        "tasks_with_different_control_scores": sorted(
            {r["task"] for r in common if r["pure_success"] != r[alternative]}, key=int
        ),
        "memory_successes": sum(r["memory_success"] for r in common),
        "same_round_pure_successes": sum(r["pure_success"] for r in common),
        "alternative_successes": sum(r[alternative] for r in common),
        "alternative_scores_on_discordant": {
            state: {
                k: sum(r[alternative] is v for r in rows if r["state"] == state)
                for k, v in [("pass", True), ("fail", False), ("missing", None)]
            }
            for state in ("gain", "loss")
        },
    }


def analyze(workspace):
    inputs = {k: read_json(verified(workspace / path, sha)) for k, (path, sha) in FROZEN.items()}
    current, previous = inputs["results"], inputs["previous"]
    tasks = {t["id"]: t for t in inputs["tasks"]}
    history = inputs["sources"]["database_replay"]
    cells = {s["run"]: s for s in current["solves"] + previous["cells"]}
    require(len(cells) == len(current["solves"]) + len(previous["cells"]), "Reused run in two studies")
    native = native_reader(workspace, cells)
    db_matches = db_replay(workspace, tasks, history["reference"], native)
    controls = {(s["task"], s["stage"], s["arm"]): s for s in current["solves"] if not s["source"]}
    other_notes = {
        (s["task"], s["stage"], s["source"], s["arm"]): s for s in current["solves"] if s["source"]
    }
    writers = {(s["source"], s["arm"]): s for s in current["writers"]}
    matched, discordant, past = [], [], defaultdict(Counter)
    for pair in current["solve_summary"]["pairs"]["revised/no_memory"]:
        state = pair["state"]
        if state == "missing":
            continue
        memory, pure = cells[pair["run"]], cells[pair["control_run"]]
        t, stage, donor, arm = (memory[k] for k in ("task", "stage", "source", "arm"))
        require(
            pure == controls[t, stage, "no_memory"]
            and state == status(memory["official"], pure["official"])
            and memory["preload_notice_version"] == "domain-auth-v2"
            and arm.endswith("_explicit")
            and memory["opening"]["simulator_request"] == pure["opening"]["simulator_request"],
            "Mispaired treatment or control",
        )
        empty = controls[t, stage, "empty_preload"]
        other = controls[t, "solve2" if stage == "solve1" else "solve1", "no_memory"]
        wr = writers[donor, arm]
        note = "\n".join(i["title"] + "\n" + i["body"] for i in wr["items"])
        require(
            bool(wr["items"]) == memory["body_exposed"], "Unobserved writer note in a graded treatment"
        )
        source, target = (
            mutations(history["observed"][donor]["executed"]),
            mutations(tasks[t]["evaluation_criteria"]["actions"]),
        )
        mem_report, m = native(memory["run"])
        pure_report, c = native(pure["run"])
        row = {
            "task": t,
            "source": donor,
            "arm": arm,
            "stage": stage,
            "customer": pair["customer"],
            "state": state,
            "memory": memory["run"],
            "pure": pure["run"],
            "writer": wr["run"],
            "exposed": memory["body_exposed"],
            "note_mentions_handoff": bool(HANDOFF_WORDS.search(note)),
            "memory_success": memory["official"],
            "pure_success": pure["official"],
            "empty_run": empty["run"],
            "empty_success": empty["official"],
            "other_pure_run": other["run"],
            "other_pure_success": other["official"],
            "same_opening": memory["opening"]["customer_text"] == pure["opening"]["customer_text"],
            "handoffs": handoffs(m, c),
            "memory_source_extras": repeated_source_actions(memory, source, target),
            "pure_source_extras": repeated_source_actions(pure, source, target),
        }
        matched.append(row)
        if state in ("gain", "loss"):
            loser = pure if state == "gain" else memory
            detail = {
                "memory": memory["run"],
                "pure": pure["run"],
                "task": t,
                "source": donor,
                "arm": arm,
                "state": state,
                "memory_trace": m,
                "pure_trace": c,
                "losing_checks": grade_reason(loser),
                "first_public_difference": first_difference(
                    [public_message(msg) for msg in mem_report["messages"]],
                    [public_message(msg) for msg in pure_report["messages"]],
                ),
                "failed_assertions": [a["assertion"] for a in loser["grade"]["nl"] if not a["met"]],
            }
            if detail["failed_assertions"]:
                detail["amounts"] = {
                    "memory": amount_evidence(memory["grade"]["nl"], mem_report["messages"]),
                    "pure": amount_evidence(pure["grade"]["nl"], pure_report["messages"]),
                    "note_amounts": sorted(amounts(note)),
                }
            if reviewed := CATALOG_CASES.get((t, donor, arm)):
                detail["catalog"] = catalog_claim(workspace, memory, *reviewed, note)
                detail["catalog"]["db_replay"] = replay_catalog(memory, pure, reviewed[1], db_matches)
            discordant.append(detail)

    for pair in previous["pairs"]:
        outcome = pair["no_memory"]
        state = outcome["state"]
        if state != "missing":
            m, c = native(pair["run"])[1], native(outcome["run"])[1]
            past[state][handoffs(m, c)] += 1

    by_task = defaultdict(Counter)
    by_state = defaultdict(Counter)
    reasons = defaultdict(Counter)
    for row in matched:
        by_task[row["task"]][row["state"]] += 1
        by_state[row["state"]][row["handoffs"]] += 1
    for entry in discordant:
        reasons[entry["state"]][entry["losing_checks"]] += 1
    primary = current["solve_summary"]["contrasts"]["revised/no_memory"]["counts"]
    require(
        Counter(r["state"] for r in matched)
        == Counter({s: n for s, n in primary.items() if s != "missing"})
        and len(discordant) == primary["gain"] + primary["loss"]
        and sum("catalog" in d for d in discordant) == len(CATALOG_CASES),
        "Incomplete coverage",
    )
    cases_72 = [r for r in matched if r["task"] == "72" and r["memory_source_extras"]]
    (control_72,) = {r["pure"] for r in cases_72}
    replay = replay_task_72(
        [r["memory"] for r in cases_72],
        control_72,
        native,
        cells,
        db_matches,
    )
    replay["same_reference_changes_for_source_and_target"] = mutations(
        tasks["71"]["evaluation_criteria"]["actions"]
    ) == mutations(tasks["72"]["evaluation_criteria"]["actions"])
    replay["other_round_same_banks"] = []
    for row in cases_72:
        other = other_notes["72", cells[row["other_pure_run"]]["stage"], row["source"], row["arm"]]
        replay["other_round_same_banks"].append(
            {
                "run": other["run"],
                "arm": other["arm"],
                "official": other["official"],
                "source_extras": repeated_source_actions(
                    other,
                    mutations(history["observed"]["71"]["executed"]),
                    mutations(tasks["72"]["evaluation_criteria"]["actions"]),
                ),
            }
        )
    return {
        "inputs": {path: sha for path, sha in FROZEN.values()},
        "analysis_model_calls": 0,
        "missing": primary["missing"],
        "by_task": by_task,
        "handoffs_by_outcome": by_state,
        "previous_handoffs_by_outcome": past,
        "losing_grade_components": reasons,
        "matched": matched,
        "control_sensitivity": {
            k: control_sensitivity(matched, k) for k in ("empty_success", "other_pure_success")
        },
        "discordant": discordant,
        "task_72_db_replay": replay,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with patch("socket.socket.connect", side_effect=AssertionError("The analysis is offline")):
        result = analyze(args.workspace.resolve())
    fixed(args.output, result)
    print(len(result["matched"]), len(result["discordant"]), result["losing_grade_components"])
