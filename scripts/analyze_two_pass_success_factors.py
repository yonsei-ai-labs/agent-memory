"""Verify paired reversals and isolate final DB differences in archived airline trajectories."""
import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
import json
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.adapters.tau2 import PIN, native_modules, verify_checkout
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import FORMS
from agent_memory_eval.memory.context import snapshot_message
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path
from review_tau2_baseline_screening import write_tool_names

ROOT = Path(__file__).resolve().parents[1]
LOCK = "artifacts/pilots/20261118-two-pass-forms-final-reproduction-lock.json"
PAIRS = "artifacts/analyses/20261118-two-pass-forms-final-actions.json"
OUTPUT = "artifacts/analyses/20261118-two-pass-memory-success-factors.json"
# Descriptions of observed write differences, assigned after inspecting all gain blocks.
WRITE_DIFFERENCE = {
    "1": "omit_unwarranted_write", "10": "omit_unwarranted_write", "12": "omit_unwarranted_write",
    "14": "correct_arguments", "21": "correct_arguments",
    "15": "complete_missing_write", "42": "complete_missing_write",
    "11": "different_customer_authorized_action",
}
FOCAL_TERMS = {
    233: ("$207", "$2,580"), 234: ("$207", "$2,580"),
    329: ("one", "$1,786"), 330: ("one travel certificate", "$1,786"), 331: ("one travel certificate", "$1,786"),
    334: ("one certificate maximum", "$1,786"), 342: ("HAT290", "HAT175", "$6", "$113", "6276644"),
    377: ("passenger identity, dates and status", "5BGGWZ", "FDZ0T5", "HSR97W"),
    427: ("$600", "$1,200", "confirmation"),
}
# Public message indices anchoring the judgments discussed in the report.
PUBLIC_TURNS = {
    233: (16, 17), 235: (14, 15), 330: (19, 23, 24), 334: (21, 23, 24),
    341: (31, 32, 33), 342: (23, 24, 25), 365: (7,), 377: (23,),
    403: (23,), 404: (23,), 427: (8, 14),
}


def require(condition, message):
    if not condition:
        raise ContractError(message)


def matched_cases(rows, reviewed, grades):
    solves = {(r["task_id"], r["repetition"], r["arm"]): r for r in rows if r["phase"] == "solve"}
    labeled = {(p["treatment_job"], p["control_job"]): (key, p) for key, p in reviewed["pairs"].items()
               if p["label"] in {"gain", "loss"}}
    counts, pairs, seen = Counter(), [], set()
    for (task, rep, arm), r in solves.items():
        if arm not in FORMS:
            continue
        control = solves[task, rep, "no_memory"]
        empty = solves[task, rep, "empty_preload"]
        outcome = r.get("result", {}).get("official_success")
        baseline = control.get("result", {}).get("official_success")
        if type(outcome) is not bool or type(baseline) is not bool:
            counts["unpaired"] += 1
            continue
        if outcome == baseline:
            counts["both_success" if outcome else "both_failure"] += 1
            continue
        direction = "gain" if outcome else "loss"
        counts[direction] += 1
        key = r["index"], control["index"]
        case_id, case = labeled[key]
        seen.add(key)
        writer = case["assigned_writer"]
        sides = {s["job"]: s for s in case["sides_in_blind_order"]}
        side = sides[r["index"]]
        require((case["task_id"], case["repetition"], case["treatment"], case["label"]) ==
                (task, rep, arm, direction), "Paired review and original assignment disagree")
        for row in (control, r):
            grade, linked = grades[row["index"]], sides[row["index"]]
            require(grade["official_score"] == row["result"]["official_score"] == linked["reward"] and
                    grade["report_sha256"] == row["result"]["report_ref"]["sha256"] and
                    grade["db_match"] == linked["db_match"], "Native replay and linked outcome disagree")
        require(len(side["exposed_notes"]) == writer["item_count"] and
                bool(side["exposed_notes"]) == bool(side["normal_response_memory_spans"]),
                "Written and actually exposed memory disagree")
        pairs.append(dict(task=task, repetition=rep, form=arm, direction=direction, pair_id=case_id,
                          job=r["index"], control=control["index"], empty_control=empty["index"],
                          empty_control_success=empty.get("result", {}).get("official_success"),
                          source_success=case["source_experience"]["first_pass_success"],
                          authored_items=writer["item_count"], grounding=writer["grounding"],
                          authored_text_sha256=digest(canonical(side["exposed_notes"])),
                          source_public_sha256=case["source_experience"]["public_context_sha256"],
                          first_user_context=case["judgment"]["user_context"],
                          first_difference_category=case["judgment"]["category"]))
    require(seen == set(labeled), "Outcome reversals differ from the published masked review")
    return counts, sorted(pairs, key=lambda p: p["job"])


def state_hashes(env):
    return (env.get_db_hash(), env.get_user_db_hash())


def check_worker_input(folder, row, blobs, paired):
    scan, state, _ = read_experiment(folder)
    snapshot = row["result"]["start_snapshot_id"]
    items = snapshot_items(blobs, state, snapshot)
    notes = [{k: item[k] for k in ("title", "body")} for item in items]
    note_hash = digest(canonical(notes))
    if paired:
        require(note_hash == paired["authored_text_sha256"], "Archived notes differ from the linked case")
    expected = snapshot_message(items)
    requests = [e["payload"] for e in scan["events"].values() if e["type"] == "model.request_started"
                and e["payload"]["actor_role"] == "worker"]
    require(requests and all(blobs.get(e["request_ref"])["messages"][1] == expected for e in requests),
            "The assigned memory did not reach every worker request")
    content = "\n".join(value for note in notes for value in note.values()).casefold()
    return dict(snapshot_id=snapshot, title_body_sha256=note_hash, items=len(items),
                worker_requests_with_exact_snapshot=len(requests),
                note_literal_presence={s: s.casefold() in content for s in FOCAL_TERMS.get(row["index"], ())})


def actions_and_probes(view, rows, grades, pairs, checkout):
    modules = native_modules(checkout)
    from loguru import logger
    from tau2.data_model.simulation import SimulationRun
    from tau2.data_model.tasks import Task
    from tau2.multilingual.runtime import language_environment_kwargs
    from tau2.registry import registry
    from tau2.runner.presets import reload_recorded_call
    logger.disable("tau2")
    task_file = checkout / "data/tau2/domains/airline/tasks.json"
    task_defs = {t["id"]: t for t in read_json(task_file.read_bytes())}
    writes = set(write_tool_names((checkout / "src/tau2/domains/airline/tools.py").read_text()))
    factory = registry.get_env_constructor("airline")
    # 326 supplies the observed bag counts for the converse task-23 failure.
    # 378 is the task-42 same-cancellations comparison with a further certificate.
    paired = {p["job"]: p for p in pairs}
    jobs = {326, 378} | set(FOCAL_TERMS) | {p[k] for p in pairs for k in ("job", "control")}
    recorded, actions, reference, env_options = {}, {}, {}, {}
    results = []
    with ExitStack() as stack:
        stop = RuntimeError("A model or network operation was attempted in an offline DB diagnostic")
        guards = [stack.enter_context(patch.object(m, name, side_effect=stop))
                  for m, name in zip(modules, ("generate", "generate", "completion"))]
        guards += [stack.enter_context(patch(name, side_effect=stop)) for name in
                   ("socket.socket.connect", "socket.socket.connect_ex", "tau2.evaluator.evaluator_communicate.generate")]
        for job in sorted(jobs):
            row = rows[job]
            t = row["task_id"]
            result = row["result"]
            report_ref = result["report_ref"]
            folder = safe_path(view, row["directory"])
            require(digest((folder / "log.jsonl").read_bytes()) == result["log_sha256"] and
                    report_ref["sha256"] == grades[job]["report_sha256"], "Archived job or report changed")
            blobs = BlobStore(folder)
            sim = SimulationRun.model_validate(blobs.get(report_ref))
            require(sim.task_id == t and sim.reward_info.reward == result["official_score"],
                    "Native report belongs to a different decision or outcome")
            options = language_environment_kwargs(reload_recorded_call(sim.language_context, "airline")) or {}
            require(options == env_options.setdefault(t, options), "Mixed environment settings for the same task")
            env = factory(**options)
            if t not in reference:
                task = Task.model_validate(task_defs[t])
                require(task.initial_state is None, "This audit requires the recorded initial airline database")
                gold = factory(**options)
                for step in task.evaluation_criteria.actions or []:
                    gold.make_tool_call(step.name, requestor=step.requestor, **step.arguments)
                reference[t] = state_hashes(gold)
            env.set_state(initialization_data=None, initialization_actions=None, message_history=sim.messages, strict=True)
            original = state_hashes(env)
            require(original[0] == result["final_db_hash"] and
                    (original == reference[t]) == grades[job]["db_match"], "Strict replay differs from official recorded DB")

            responses = {m.id: m for m in sim.messages if m.role == "tool"}
            seq, details = [], []
            replay = factory(**options)
            for index, message in enumerate(sim.messages):
                for call in getattr(message, "tool_calls", None) or []:
                    if call.name not in writes:
                        continue
                    observed = responses[call.id]
                    if observed.execution_status == "rejected":
                        continue
                    replayed = replay.get_response(call)
                    require(replayed.error == observed.error, "Business tool error status changed")
                    seq.append(call)
                    details.append(dict(message_index=index, call_id=call.id, tool=call.name,
                                        arguments_sha256=digest(canonical(call.arguments)), error=observed.error))
            require(state_hashes(replay) == original, "Unedited business actions do not reconstruct the actual DB")
            recorded[job] = dict(task=t, job=job, report_sha256=report_ref["sha256"], writes=details,
                                 final_db_sha256=original[0], db_match=grades[job]["db_match"],
                                 missing_communication=grades[job]["missing_communication"])
            if job in PUBLIC_TURNS:
                turns = []
                for index in PUBLIC_TURNS[job]:
                    message = sim.messages[index]
                    require(message.role in {"assistant", "user"} and isinstance(message.content, str)
                            and message.content.strip(), "Selected evidence must be a public spoken turn")
                    turns.append(dict(index=index, role=message.role, text=message.content))
                recorded[job]["selected_public_turns"] = turns
            if row["arm"] in FORMS:
                recorded[job]["worker_memory"] = check_worker_input(folder, row, blobs, paired.get(job))
            actions[job] = seq

        def copy_fields(job, source_call, donor, donor_call, fields):
            call = actions[job][source_call]
            other = actions[donor][donor_call]
            require(call.name == other.name and rows[job]["task_id"] == rows[donor]["task_id"],
                    "Field probes must use recorded values from the same task and tool")
            require(all(call.arguments[k] != other.arguments[k] for k in fields), "A proposed field change had no effect")
            return call.model_copy(update={"arguments": {**call.arguments, **{k: other.arguments[k] for k in fields}}})

        def probe(job, description, seq, donors=()):
            task = rows[job]["task_id"]
            require(all(rows[d]["task_id"] == task for d in donors), "Probe imports actions from another task")
            env = factory(**env_options[task])
            actual = []
            for call in seq:
                reply = env.get_response(call)
                actual.append(dict(tool=call.name, source_call_id=call.id, arguments=call.arguments, error=reply.error))
            final = state_hashes(env)
            results.append(dict(job=job, task=task, edit=description, donor_jobs=list(donors), actions=actual,
                                reference_db_sha256=reference[task][0], original_db_match=recorded[job]["db_match"],
                                edited_db_sha256=final[0], edited_db_match=final == reference[task]))

        for job, tool in ((414, "cancel_reservation"), (386, "update_reservation_flights")):
            require([c.name for c in actions[job]] == [tool], "Expected a single control mutation")
            probe(job, "Omit the sole state-changing call", [])
        for job in (369, 427):
            bag_only = [c for c in actions[job] if c.name == "update_reservation_baggages"]
            require(len(bag_only) == 1 and len(actions[job]) == 2 and
                    any(c.name == "update_reservation_flights" for c in actions[job]), "Expected upgrade and bags")
            probe(job, "Omit the cabin upgrade and retain the recorded baggage change", bag_only)
        probe(369, "Omit both the cabin and baggage updates", [])
        for fields in (("payment_methods",), ("total_baggages",), ("payment_methods", "total_baggages")):
            cancellation, booking = actions[333]
            require(booking.name == "book_reservation", "Expected the recorded replacement booking")
            probe(333, "Use the successful booking's " + ", ".join(fields),
                  [cancellation, copy_fields(333, 1, 330, 1, fields)], (330,))
        declined, paid, bags = actions[341]
        require(paid.name == "update_reservation_flights" and [r["error"] for r in recorded[341]["writes"]]
                == [True, False, False], "Expected one insufficient-card attempt before the paid change")
        probe(341, "Change only the successful flight update's payment_id",
              [declined, copy_fields(341, 1, 342, 2, ("payment_id",)), bags], (342,))
        require(not actions[235] and not actions[384], "Expected two controls with no state-changing call")
        probe(235, "Add the approved PHL flight change", actions[233], (233,))
        fd, hs = actions[377]
        for cancellations in ([fd], [hs], [fd, hs]):
            probe(384, "Cancel " + ", ".join(c.arguments["reservation_id"] for c in cancellations),
                  cancellations, (377,))
        require([c.name for c in actions[403]] == ["cancel_reservation"] and
                [c.name for c in actions[404]] == ["update_reservation_flights"], "Expected cancel versus downgrade")
        probe(403, "Omit the cancellation", [])
        probe(403, "Replace the cancellation with the recorded approved downgrade", actions[404], (404,))
        cancels = [c for c in actions[378] if c.name == "cancel_reservation"]
        require(len(cancels) == 2 and [c.name for c in actions[378]][-1] == "send_certificate"
                and len(actions[378]) == 3, "Expected two cancellations plus the additional certificate")
        probe(378, "Retain both cancellations and omit the additional certificate", cancels)
        probe(323, "Only copy the checked-bag count on each of the three bookings",
              [actions[323][0]] + [copy_fields(323, i, 326, i, ("total_baggages",)) for i in (1, 2, 3)], (326,))
        require(not any(g.call_count for g in guards), "Offline analysis attempted generation or networking")
    return recorded, results


def analyze(view, root):
    lock = read_json((root / LOCK).read_bytes())
    audit = read_json(asset_text(root, lock["audit_ref"]).encode())
    numbers = read_json(asset_text(root, lock["combined_results_ref"]).encode())
    native = read_json(asset_text(root, lock["native_regrade_ref"]).encode())
    paired_bytes = (root / PAIRS).read_bytes()
    reviewed = read_json(paired_bytes)
    require(reviewed["numeric_result_sha256"] == lock["combined_results_ref"]["sha256"] and
            native["benchmark_revision"] == PIN, "Inputs come from different run sets")
    seal = read_json(asset_text(view, dict(path="completion.json", sha256=audit["combined_view_completion_sha256"])).encode())
    rows = read_json(asset_text(view, dict(path="allocations.json", sha256=seal["allocation_sha256"])).encode())
    require(native["allocation_sha256"] == seal["allocation_sha256"] == numbers["completion"]["allocation_sha256"],
            "Episode identity differs across inputs")
    grades = {g["index"]: g for g in native["grades"]}
    counts, pairs = matched_cases(rows, reviewed, grades)
    by_job = {r["index"]: r for r in rows}
    records, probes = actions_and_probes(view, by_job, grades, pairs, verify_checkout(root / "data/tau2-bench"))
    gains = [p for p in pairs if p["direction"] == "gain"]
    groups = defaultdict(list)
    for pair in gains:
        groups[pair["control"]].append(pair)
    require({p["job"] for p in probes} >= set(groups), "Every distinct control failure requires a DB probe")
    blocks = []
    for control, members in sorted(groups.items()):
        first = members[0]
        blocks.append(dict(task=first["task"], repetition=first["repetition"], control=control,
                           original_first_pass_success=first["source_success"], empty_control=first["empty_control"],
                           empty_control_success=first["empty_control_success"], gained_jobs=[p["job"] for p in members],
                           gained_forms=[p["form"] for p in members], observed_write_difference=WRITE_DIFFERENCE[first["task"]]))
    return dict(format="agent-memory-two-pass-success-factors/v1", benchmark_revision=PIN,
                method_sha256=digest(Path(__file__).read_bytes()), lock_sha256=digest((root / LOCK).read_bytes()),
                linked_review_sha256=digest(paired_bytes), audited_view_completion_sha256=audit["combined_view_completion_sha256"],
                paired_outcomes=dict(counts), improved_blocks=blocks, discordant_comparisons=pairs,
                verified_business_trajectories=[records[k] for k in sorted(records)], diagnostic_db_probes=probes,
                model_or_network_attempts=0,
                scope="Post hoc. Each probe replays only local business actions in a fresh native database. "
                      "The original dialogue, approvals, and official grades are kept as observations. "
                      "An edited DB match identifies a state transition and does not estimate a memory intervention.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("view", type=Path, help="The independently audited three-source combined-view")
    parser.add_argument("--write-new", action="store_true", help="Create the published analysis once")
    args = parser.parse_args()
    result = analyze(args.view.resolve(), ROOT)
    data = (json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    output = ROOT / OUTPUT
    if args.write_new:
        with output.open("xb") as stream:
            stream.write(data)
    else:
        require(data == output.read_bytes(), "The new analysis differs from its published evidence")
    print("Verified", len(result["discordant_comparisons"]), "reversals and", len(result["diagnostic_db_probes"]), "DB probes")


if __name__ == "__main__":
    main()
