"""Prepare blinded decisions and paired writer inputs from the sealed banking pilot, or import independent reviews."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from agent_memory_eval.analysis.blind_review import blinding_check, compare_sealed_reviews, export_public, public_bundle
from agent_memory_eval.analysis.claim_evidence import _citation
from agent_memory_eval.analysis.decision_attribution import compare_steps
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.analysis.semantic_review import RUBRIC_HASH, compare_reviews, review_packet, validate_reviews
from agent_memory_eval.analysis.tau2_attribution import graded_episode
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.experiments.forms_write import feedback_pair, read_form_bank
from agent_memory_eval.recording.assets import asset_text, publish_json
from agent_memory_eval.recording.journal import safe_path
from scripts import run_banking_memory as pilot
from scripts.summarize_fresh_matched import verify_writer_input


SPEC = "protocols/diagnostics/banking-semantic-review-v1.json"


def add_decisions(packets, mapping, case, ordinals, claim, bank, trial, parent, first, baseline, source, read_sha,
                  *, writer_log_sha256):
    histories = {}
    steps, _ = graded_episode(parent, trial, case, read_sha, first, public_inputs=histories)
    decisions = [s for s in steps if s.actor == "worker"]
    log = RecordedDecisions(safe_path(parent, trial["directory"]), trial["run_id"], trial["result"]["log_sha256"])
    require(log.items == bank["items"] and log.state["assignments"][log.run]["source_writer_bank_sha256"] ==
            digest(canonical(bank)), "Worker bank differs from its writer")
    item, = bank["items"]
    cited = _citation(item, "/body", claim)
    outcome, = log.of_type("outcome.scored")
    for index in ordinals:
        key = dict(task=case["task_id"], arm=trial["arm"], public_worker_decision=index,
                   source_run=source.source_run_id, source_log_sha256=source.source_log_sha256,
                   source_public_sha256=source.public_context_sha256, source_outcome_event_id=source.outcome_event_id,
                   writer_run=bank["run_id"], writer_log_sha256=writer_log_sha256,
                   snapshot=bank["snapshot_id"], item_id=item["id"], version=item["version"],
                   claim_location=cited, worker_run=log.run, worker_log_sha256=trial["result"]["log_sha256"],
                   outcome_event_id=outcome["event_id"], official_report_sha256=outcome["payload"]["official_artifact_ref"]["sha256"],
                   official_success=outcome["payload"]["official_success"])
        mapping.append(key)
        if index > len(decisions):
            key.update(status="not_reached", packet_id=None)
            continue
        step = decisions[index - 1]
        event = log.starts[step.request][0]
        req = event["payload"]
        wire = log.blobs.get(req["request_ref"])
        tools = [t for t in wire.get("tools", ()) if not t["function"]["name"].startswith("memory_")]
        packet = review_packet(source=read_json(source.public_context.encode()), claim=claim,
            current=dict(messages=histories[step.request], tools=tools),
            action=dict(text=step.text, calls=[a.record() for a in step.actions]))
        packets[packet["packet_id"]] = packet
        comparison = compare_steps(baseline[index - 1] if len(baseline) >= index else None, step)
        key.update(status="recorded", packet_id=packet["packet_id"], request=step.request, request_event_id=event["event_id"],
                   input_sha256=req["request_ref"]["sha256"], response_sha256=log.finishes[step.request]["response_ref"]["sha256"],
                   public_comparison=comparison["status"], reference_request=comparison.get("reference_request"),
                   response_change=comparison["change"], body_in_input="body" in step.fields)


def prepare(workspace):
    study = pilot.matched
    spec = study.read(pilot.ROOT / SPEC)
    result = json.loads(asset_text(pilot.ROOT, spec["source_result"]))
    proto = study.read(pilot.ROOT / pilot.REQUIRED_PROFILE)
    parent = json.loads(asset_text(pilot.ROOT, proto["parent"]))
    cfg = pilot.required_design(json.loads(asset_text(pilot.ROOT, parent["base"])), proto)
    read_sha = cfg["delivery_instruction"]["sha256"]
    root = workspace / "runs" / proto["name"]
    reg = study.read(root / "registration.json")["preflight"]
    require(digest(canonical(reg)) == result["registration_sha256"], "Unregistered banking cohort")
    cases = reg["cases"]
    require(set(spec["claims"]) == {c["task_id"] for c in cases}, "Incomplete clause selection")
    common = asset_text(pilot.ROOT, cfg["writing"]["common_instruction_ref"]).rstrip("\r\n")
    prompts = {f: digest((common + "\n" + line).encode()) for f, line in cfg["writing"]["lines"].items()}
    archived = {r["run"]: r for group in ("sources", "writers", "trials") for r in result[group]}
    packets, mapping, pairs = {}, [], []
    for case in cases:
        tid = case["task_id"]
        base = root / ("task-" + tid)
        source_dir = safe_path(workspace / "runs", case["imported_first_pass"]["directory"])
        source = study.source_from(source_dir, tid)
        first, = study.sealed_rows(source_dir)[2]
        require(source.source_log_sha256 == archived[source.source_run_id]["log_sha256"]
                and source.public_context_sha256 == case["imported_first_pass"]["public_context_sha256"]
                and source.official_success is archived[source.source_run_id]["success"], "Wrong first-pass result")
        original, _ = graded_episode(source_dir, first, case, read_sha)
        baseline = [s for s in original if s.actor == "worker"]
        trials = {t["arm"]: t for t in study.sealed_rows(base / "solve")[2]}
        writers = study.sealed_rows(base / "write")[2]
        require({w["arm"] for w in writers} == set(study.FORMS) and len(writers) == len(study.FORMS),
                "Missing or duplicate writer form")
        proposed = study.design_for(dict(cfg, writer_feedback_level="result_only"), case, "write", source)
        require(proposed["source"]["cohort"][0]["result_feedback"] == source.result_receipt(), "Unbound proposed outcome")
        for writer in writers:
            form = writer["arm"]
            folder = safe_path(base / "write", writer["directory"])
            checksum = writer["result"]["log_sha256"]
            expected = archived[writer["run_id"]]
            require(expected["log_sha256"] == checksum and expected["form"] == form and expected["task"] == tid
                    and verify_writer_input(base/"write", writer, source, dict(writer_prompt_sha256=prompts)),
                    "Unverified original writer input")
            log = RecordedDecisions(folder, writer["run_id"], checksum)
            request = log.requests[0]
            pair = feedback_pair(log.blobs.get(request["request_ref"]), source)
            require(pair["none_input_sha256"] == request["request_ref"]["sha256"], "Writer wire digest differs")
            pairs.append(dict(task=tid, form=form, source_run=source.source_run_id, source_log_sha256=source.source_log_sha256,
                              writer_run=log.run, writer_log_sha256=checksum, original_request=request["request_id"], **pair))
            if form not in spec["claims"][tid]:
                continue
            bank = read_form_bank(folder, expected_log_sha256=checksum)
            require(bank["items"] == expected["items"] and bank["snapshot_id"] == expected["snapshot"], "Changed candidate bank")
            for mode in study.MODES:
                trial = trials[form + "__" + mode]
                expected_trial = archived[trial["run_id"]]
                require(expected_trial["log_sha256"] == trial["result"]["log_sha256"] and expected_trial["task"] == tid
                        and expected_trial["arm"] == trial["arm"], "Changed reader assignment")
                add_decisions(packets, mapping, case, spec["worker_decisions"], spec["claims"][tid][form], bank,
                              trial, base/"solve", first, baseline, source, read_sha, writer_log_sha256=checksum)
    return packets, mapping, pairs


def save(path, data):
    try:
        publish_json(path, data)
    except FileExistsError:
        require(read_json(path.read_bytes()) == data, f"Different evidence already exists at {path}")


def writer_note_packets(workspace):
    """Hide the assignment and later reader outcomes while reviewing every available authored note."""
    study = pilot.matched
    spec = study.read(pilot.ROOT / pilot.FEEDBACK_PROFILE)
    previous = json.loads(asset_text(pilot.ROOT, spec["comparison_results"]))
    root = workspace/"runs"/spec["name"]
    registration = study.read(root/"registration.json")["preflight"]
    require(registration["protocol_sha256"] == digest((pilot.ROOT / pilot.FEEDBACK_PROFILE).read_bytes()),
            "Writer comparison does not match the registered protocol")
    archived = {(r["task"],r["form"]): r for r in previous["writers"]}
    packets, assignments = {}, []
    for case in registration["cases"]:
        tid = case["task_id"]
        folder = root/("task-" + tid)/"write"
        if folder.exists():
            _, control, rows = study.sealed_rows(folder)
            require(control["profile"]["registration_sha256"] == digest(canonical(registration)), "Different writer registration")
            jobs = {r["arm"]: r for r in rows}
        else:
            stop = study.read(root/"completion.json")["stages"][str(folder.relative_to(root))]
            require(stop.get("status") == "not_started", "Unsealed writing stage")
            jobs = {f: dict(status="not_started") for f in study.FORMS}
        source = study.source_from(safe_path(workspace/"runs", case["imported_first_pass"]["directory"]), tid)
        for form in study.FORMS:
            old = archived[tid,form]
            row = jobs[form]
            result = row.get("result") or {}
            bank = None
            if row["status"] == "completed":
                bank = read_form_bank(safe_path(folder, row["directory"]), expected_log_sha256=result["log_sha256"])
                require(bank["result_feedback"] == source.result_receipt() and bank["source_run_id"] == source.source_run_id
                        and bank["public_context_sha256"] == old["source_public_sha256"], "Different first pass or feedback")
            for policy, items, writer, checksum in (("none",old["items"],old["run"],old["log_sha256"]),
                    ("result_only",bank["items"] if bank else [],row.get("run_id"),result.get("log_sha256"))):
                linkage = dict(task=tid, form=form, feedback=policy, writer_run=writer, writer_log_sha256=checksum,
                               first_pass_success=source.official_success, note_id=None)
                assignments.append(linkage)
                if not items:
                    continue
                public = dict(endpoint=spec["analysis"]["source_endpoints"][tid],
                              items=[dict(title=i["title"], body=i["body"]) for i in items])
                note_id = digest(canonical(public))
                packets[note_id] = public
                linkage.update(note_id=note_id, items=[dict(item=i["id"],version=i["version"]) for i in items])
    return packets, assignments


def note_ratings(packets, assignments, submission):
    """Join a condition-hidden analyst reading only after validating its verbatim evidence."""
    require(submission.get("format") == "banking-feedback-note-ratings/v1" and submission.get("reviewer")
            and submission.get("packet_sha256") == digest(canonical(packets)), "Wrong analyst or note set")
    raw = submission["ratings"]
    require(set(raw) == set(packets), "Review must cover all distinct observed notes")
    counts, results = {}, []
    for row in assignments:
        cell = counts.setdefault(row["feedback"], {}).setdefault("success" if row["first_pass_success"] else "failure",
                               dict(assigned=0, reviewed=0, endpoint=Counter(), overall_result=Counter()))
        cell["assigned"] += 1
        if row["note_id"] is None:
            results.append(row)
            continue
        note = packets[row["note_id"]]
        rated = {}
        axes = {"endpoint": ({"conditional","generalized","indeterminate"} if row["first_pass_success"] else
                              {"sufficient","limited","indeterminate"}), "overall_result":{"success","failure","unstated","ambiguous"}}
        for axis, allowed in axes.items():
            entry = raw[row["note_id"]][axis]
            label = entry["label"]
            require(label in allowed, "Invalid writing rubric label")
            cites = entry["citations"]
            require(isinstance(cites, list) and (cites or label in {"unstated","ambiguous","indeterminate"}),
                    "A decisive judgment requires a quotation")
            checked = []
            for c in cites:
                require(c["pointer"] in {f"/items/{i}/{f}" for i in range(len(note["items"])) for f in ("title","body")},
                        "Only authored text supports a note judgment")
                checked.append(_citation(note, c["pointer"], c["quote"]))
            rated[axis] = dict(label=label, citations=checked)
            cell[axis][label] += 1
        cell["reviewed"] += 1
        results.append(dict(row, **rated))
    return dict(format="banking-feedback-note-review/v1", reviewer=submission["reviewer"],
                packet_sha256=submission["packet_sha256"], summary=counts, allocations=results)


def summary(packets, mapping, pairs):
    observed = [m for m in mapping if m["status"] == "recorded"]
    outcomes = {p["source_run"]: p["success"] for p in pairs}
    return dict(format="banking-semantic-review-audit/v1", status="retrospective_inputs_prepared",
                selection_sha256=digest((pilot.ROOT / SPEC).read_bytes()),
                rubric_sha256=RUBRIC_HASH, planned_decisions=len(mapping), recorded_decisions=len(observed),
                unique_packets=len(packets), body_in_input=sum(m["body_in_input"] for m in observed),
                public_comparisons=dict(Counter(m["public_comparison"] for m in observed)),
                source_successes=sum(outcomes.values()), source_failures=len(outcomes)-sum(outcomes.values()),
                projected_writer_inputs=len(pairs), projected_success_inputs=sum(p["success"] for p in pairs),
                projected_failure_inputs=sum(not p["success"] for p in pairs),
                packet_ids_sha256=digest(canonical(sorted(packets))), analyst_key_sha256=digest(canonical(mapping)),
                feedback_pairs_sha256=digest(canonical(pairs)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=pilot.ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ratings", type=Path, help="Import a human or model submission against rebuilt source evidence")
    parser.add_argument("--human-reference", type=Path, help="Compare to a separately identified human submission")
    parser.add_argument("--writer-comparison", action="store_true", help="Review the two feedback conditions' authored text")
    parser.add_argument("--note-ratings", type=Path, help="Import complete quote-checked analyst ratings of the authored text")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--reviewer-only", action="store_true", help="Create a fresh directory with only public claim inputs")
    group.add_argument("--sealed-review", type=Path, help="Score delivery-condition guesses after a claim review has been sealed")
    parser.add_argument("--second-sealed-review", type=Path, help="Measure agreement with another sealed reviewer")
    args = parser.parse_args()
    if args.second_sealed_review and not args.sealed_review:
        parser.error("--second-sealed-review requires --sealed-review")
    if (args.reviewer_only or args.sealed_review) and (args.writer_comparison or args.ratings or args.human_reference or args.note_ratings):
        parser.error("The isolated claim review has its own export and sealed-submission modes")
    if args.writer_comparison:
        if args.ratings or args.human_reference:
            parser.error("Use --note-ratings for the writer comparison")
        packets, assignments = writer_note_packets(args.workspace.resolve())
        args.output.mkdir(parents=True, exist_ok=True)
        save(args.output/"blinded-notes.json", dict(sorted(packets.items())))
        save(args.output/"note-assignments.json", assignments)
        if args.note_ratings:
            save(args.output/"note-review.json", note_ratings(packets, assignments, read_json(args.note_ratings.read_bytes())))
        print(json.dumps(dict(assigned=len(assignments), unique_notes=len(packets), packet_sha256=digest(canonical(packets)))))
        return
    if args.note_ratings:
        parser.error("--note-ratings requires --writer-comparison")
    if args.human_reference and not args.ratings:
        parser.error("--human-reference requires --ratings")
    packets, mapping, pairs = prepare(args.workspace.resolve())
    if args.reviewer_only:
        print(export_public(args.output, packets))
        return
    if args.sealed_review:
        conditions = defaultdict(set)
        for row in mapping:
            if row["status"] == "recorded":
                conditions[row["packet_id"]].add(row["arm"].split("__")[-1])
        bundle = public_bundle(packets)
        sealed = read_json(args.sealed_review.read_bytes())
        result = blinding_check(bundle, sealed, conditions)
        if args.second_sealed_review:
            second = read_json(args.second_sealed_review.read_bytes())
            result = dict(first=result, second=blinding_check(bundle, second, conditions),
                          agreement=compare_sealed_reviews(bundle, sealed, second))
        save(args.output, result)
        print(json.dumps(result))
        return
    report = summary(packets, mapping, pairs)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.ratings:
        submitted = read_json(args.ratings.read_bytes())
        verified = validate_reviews(packets, submitted)
        result = dict(format="banking-semantic-assessment/v1", audit=report, reviewer=submitted["reviewer"],
                      submission_sha256=digest(canonical(submitted)), reviewed_unique_packets=len(verified),
                      verified_reviews=verified, allocations=[dict(m, reviewed=m["packet_id"] in verified) for m in mapping])
        if args.human_reference:
            reference = read_json(args.human_reference.read_bytes())
            result.update(human_reference_sha256=digest(canonical(reference)),
                          calibration=compare_reviews(packets, submitted, reference))
            save(args.output / "human-reference.json", reference)
        save(args.output / "submission.json", submitted)
        save(args.output / "assessment.json", result)
        report = dict(report, reviewed_unique_packets=len(verified))
        if args.human_reference:
            report["paired_human_reviews"] = result["calibration"]["paired"]
    else:
        save(args.output / "blinded-inputs.json", public_bundle(packets))
        save(args.output / "analyst-key.json", mapping)
        save(args.output / "writer-feedback-pairs.json", pairs)
        save(args.output / "summary.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
