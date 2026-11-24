"""Reopen only the new series and report paired outcomes, memory exposure and effort."""

import argparse
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path

from agent_memory_eval.analysis.efficiency import episode_effort, paired_effort, request_effort_by_start
from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.analysis.selection import worker_choices, worker_exposure
from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.experiments.matched_trial import source_task_start
from agent_memory_eval.recording.experience import _source
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.recording.provenance import public_assistant_message
from agent_memory_eval.runner.task_start import FORMAT as TASK_START_FORMAT, TaskStart
from scripts.run_fresh_matched import FORMS, MODES, PREFLIGHT, PROFILE, ROOT, STAGES, check_published_cases, jobs_for, read, sealed_rows


def episode(folder, row, case, registration, source_id, writer, *, include_exposure=False):
    result, run = row["result"], row["run_id"]
    with _source(folder, result["log_sha256"]) as (scan, blobs, _, exp):
        state = blobs.get(scan["records"][-1]["state_ref"])
        evidence = (scan, state, exp)
        audit_requests(folder, evidence=evidence)
        events = list(scan["events"].values())
        grade, = [e["payload"] for e in events if e["run_id"] == run and e["type"] == "outcome.scored"]
        if any(grade[k] != result[k] for k in ("official_success", "official_score", "grading_status")):
            raise ContractError("The published outcome differs from the native result")
        starts = [e for e in events if e["type"] == "model.request_started"]
        controls = [e for e in events if e["payload"].get("origin") == "matched_trial.native_control"]
        proofs = [e for e in events if e["payload"].get("origin") == "task_start.first_decision"]
        if starts or proofs or grade["grading_status"] == "graded":
            check, = controls
            receipt = blobs.get(check["payload"]["receipt_ref"])
            if (grade["grading_status"] == "graded" and not starts or starts and check["event_seq"] >= starts[0]["event_seq"]
                    or proofs and check["event_seq"] >= proofs[0]["event_seq"] or receipt["source_run_id"] != source_id
                    or receipt["registration_sha256"] != registration or receipt["native_sha256"] != case["native_sha256"]
                    or receipt["customer_opening_sha256"] != case["customer_opening_sha256"]):
                raise ContractError("The episode lacks the correct prerequest matched-task receipt")
        assignment = state["assignments"][run]
        if assignment["source_writer_run_id"] != (writer and writer["run_id"]) or (
                writer and assignment["source_writer_snapshot_id"] != writer["snapshot_id"]):
            raise ContractError("The solver did not use the registered writer and bank")
        customers = [e["payload"] for e in starts if e["payload"]["actor_role"] == "user_simulator"]
        opening_reply = None
        extra = {}
        if "task_start_mode" in case:
            workers = [e for e in starts if e["payload"]["actor_role"] == "worker"]
            if (case["task_start_mode"] != TASK_START_FORMAT or len(proofs) > 1 or workers and not proofs
                    or source_id and customers and (not workers or starts[0] != workers[0])):
                raise ContractError("Unexpected first-decision evidence or regenerated customer opening")
            if proofs:
                proof = proofs[0]
                p = proof["payload"]
                start = TaskStart.from_record(blobs.get(p["task_start_ref"]))
                opening = start.record()["public_input"]["messages"][-1]
                if (start.controls_sha256 != case["native_sha256"] or p["source_run_id"] != source_id
                        or start.task.task_id != f'{case["domain"]}/{case["task_id"]}'
                        or p["source_log_sha256"] != receipt["source_log_sha256"]):
                    raise ContractError("First-decision proof belongs to a different source")
                if workers:
                    first_worker = workers[0]
                    wire = blobs.get(first_worker["payload"]["request_ref"])
                    if proof["event_seq"] >= first_worker["event_seq"] or wire["messages"][-1] != opening:
                        raise ContractError("First worker request differs from its registered opening")
                    if source_id is None and source_task_start(
                            blobs.get(check["payload"]["control_ref"]), events, starts, blobs) != start:
                        raise ContractError("Start record differs from the original customer response and worker input")
                    opening_reply = digest(canonical(dict(role="assistant", content=opening["content"])))
                    extra["task_start_sha256"] = p["task_start_ref"]["sha256"]
                    extra["first_worker_request_id"] = first_worker["payload"]["request_id"]
                    extra["source_worker_request_id"] = p["source_worker_request_id"]
        elif customers:
            first = customers[0]
            if digest(canonical(blobs.get(first["request_ref"]))) != case["customer_opening_sha256"]:
                raise ContractError("The first customer request changed")
            for e in events:
                p = e["payload"]
                if e["type"] == "model.request_finished" and p["request_id"] == first["request_id"] and p["status"] == "response":
                    msg = blobs.get(p["response_ref"])["choices"][0]["message"]
                    opening_reply = digest(canonical(public_assistant_message(msg)))
        trace = worker_choices(folder, run_id=run, evidence=evidence)["choices"]
        if include_exposure:
            extra["worker_exposure"] = worker_exposure(trace)
        replies = [s for s in trace if s["status"] == "response"]
        body = [s for s in replies if any(v["field"] == "body" for v in s["visible_memory"])]
        messages = blobs.get(result["report_ref"])["messages"] if result["report_ref"] else None
        effort = episode_effort(row, messages, trace)
        if proofs and "task_start_mode" in case:
            phases = request_effort_by_start(events, proofs[0]["event_seq"], {"worker", "user_simulator"})
            extra.update(setup_requests=phases["setup"], full_episode_effort=effort)
            effort = dict(effort, **phases["solve"])
            if effort["customer_turns"] is not None:
                effort["customer_turns"] -= 1  # The shared public opening precedes both workers.
        return dict(run_id=run, session_id=result["session_id"], log_sha256=result["log_sha256"],
            success=grade["official_success"], status=grade["grading_status"], opening_reply=opening_reply,
            body_seen=bool(body) if replies else None, first_body_request=body[0]["request_id"] if body else None,
            first_response_with_body=body[0]["choice"] if body else None,
            reads=sum(len(s["reads"]) for s in replies) if replies else None, effort=effort, **extra)


def report(output, *, protocol=PROFILE, published_preflight=PREFLIGHT):
    entry, completion = read(output/"registration.json"), read(output/"completion.json")
    registration = entry["preflight"]
    approved = dict(registration)
    authorization = approved.pop("authorization")
    check_published_cases(approved, read(ROOT / published_preflight))
    option = read(ROOT / protocol)["authorization_options"][registration["scope"]]
    if (not authorization.get("user_authorization") or authorization["maximum_usd"] != option["maximum_usd"]
            or Decimal(completion["total_covered_usd"]) > Decimal(str(authorization["maximum_usd"]))
            or registration["protocol_sha256"] != digest((ROOT / protocol).read_bytes())
            or completion["registration_sha256"] != digest((output/"registration.json").read_bytes())):
        raise ContractError("This report requires the sealed registration of the new series")
    reg = digest(canonical(registration))
    pairs, opening_pairs, lookup, customer_differences = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    cases, sessions = {}, set()
    costs = {s: dict(confirmed=Decimal(0), held=Decimal(0)) for s in STAGES}
    for case in registration["cases"]:
        key, sources, banks = case["task"], {}, {}
        destination = output/("task-" + key)
        by_job = cases[key] = {}
        for stage, name in zip(STAGES, case["scheduled_stages"], strict=True):
            folder = destination/name
            seal = completion["stages"][f"task-{key}/{name}"]
            expected = jobs_for(case, stage)
            if seal.get("reason") == "spend_limit" and not folder.exists():
                rows = [dict(j, status="not_started", reason="spend_limit") for j in expected]
            else:
                if digest((folder/"completion.json").read_bytes()) != seal["seal"]:
                    raise ContractError("Study and shard seals differ")
                _, ctl, rows = sealed_rows(folder)
                if (ctl["profile"]["registration_sha256"] != reg or ctl["design"]["stage"] != stage
                        or len(rows) != len(expected)
                        or any(any(r[k] != v for k, v in j.items()) for j, r in zip(expected, rows))):
                    raise ContractError("Cohort or assignment differs from registration")
            costs[stage]["confirmed"] += Decimal(seal["reported"])
            costs[stage]["held"] += Decimal(seal["held"])
            for r in rows:
                arm = r["arm"]
                if "result" not in r:
                    by_job[arm] = dict(status=r["status"], reason=r["reason"])
                    continue
                result = r["result"]
                if result["session_id"] in sessions:
                    raise ContractError("An assignment reused another session")
                sessions.add(result["session_id"])
                job = safe_path(folder, r["directory"])
                if stage == "write":
                    if result["status"] in ("completed", "empty"):
                        bank = read_form_bank(job, expected_log_sha256=result["log_sha256"])
                        if bank["run_id"] != r["run_id"] or bank["arm"] != arm:
                            raise ContractError("The writer bank differs from its assignment")
                        banks[arm] = bank
                    by_job[arm] = dict(status=result["status"], run_id=r["run_id"], log_sha256=result["log_sha256"],
                                       written_items=len(banks[arm]["items"]) if arm in banks else None)
                else:
                    form = arm.partition("__")[0]
                    first_id = sources["run_id"] if stage == "solve" else None
                    item = episode(job, r, case, reg, first_id, banks[form] if stage == "solve" else None)
                    by_job[arm] = item
                    if stage == "source":
                        sources = item
                    elif item.get("task_start_sha256") and (
                            item["task_start_sha256"] != sources.get("task_start_sha256")
                            or item["source_worker_request_id"] != sources.get("first_worker_request_id")):
                        raise ContractError("Worker did not start at its paired source's first decision")
        for form in FORMS:
            for mode, reference in (("preload", "no_memory"), ("on_demand", "no_memory"), ("preload", form+"__on_demand")):
                candidate = by_job[form+"__"+mode]
                control = by_job[reference]
                group = "/".join((case["domain"], form, mode, reference))
                left, right = candidate.get("success"), control.get("success")
                pairs[group].append((left, right, candidate.get("effort", {}), control.get("effort", {})))
                if left is not None and right is not None:
                    customer_differences[group].append((case["customer"], int(left)-int(right)))
                if candidate.get("opening_reply") and control.get("opening_reply"):
                    opening_pairs[group].append(candidate["opening_reply"] == control["opening_reply"])
                if reference == "no_memory" and candidate.get("body_seen") is not None:
                    lookup[group].append((candidate["body_seen"], candidate["reads"]))
    if sum((x["confirmed"]+x["held"] for x in costs.values()), Decimal(0)) != Decimal(completion["total_covered_usd"]):
        raise ContractError("Stage charges differ from the series ledger")
    contrasts = {}
    for k, p in pairs.items():
        customers = defaultdict(list)
        for cust, delta in customer_differences[k]:
            customers[cust].append(delta)
        means = [sum(v)/len(v) for v in customers.values()]
        contrasts[k] = dict(**paired_effort(p), graded_customers=len(means),
            equal_customer_difference_pp=round(100*sum(means)/len(means), 4) if means else None,
            comparable_opening_replies=len(opening_pairs[k]), identical_opening_replies=sum(opening_pairs[k]),
            **(dict(observed_reader_episodes=len(lookup[k]), body_seen=sum(b for b, _ in lookup[k]),
                    read_calls=sum(n for _, n in lookup[k])) if k in lookup else {}))
    return dict(series=registration["series"], registration_sha256=reg, assigned=registration["assigned"],
                unique_customers=registration["unique_customers"], distinct_sessions=len(sessions), contrasts=contrasts,
                stage_cost_usd={s:{k:str(v) for k,v in value.items()} for s,value in costs.items()}, cases=cases)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--protocol", type=Path, default=Path(PROFILE))
    parser.add_argument("--published-preflight", type=Path, default=Path(PREFLIGHT))
    args = parser.parse_args()
    print(json.dumps(report(args.output_directory, protocol=args.protocol, published_preflight=args.published_preflight),
                     ensure_ascii=False, sort_keys=True, indent=2))
