"""Trace the four registered writer-audit cases to individual reader choices, offline.

Run with --out DIR. Code the business turns in blinded-packets.json, using only that
file and the prospective rubric. --reviews accepts an object keyed by packet ID.
Each value has packet_sha256 and axes. Each axis contains one or more findings:
{"fate":"corrected_or_reverified","action":3,"evidence":[2,3],"basis":"..."}.
Use a null action for no_opportunity or an unresolved cutoff. Cite numbered turns
and identify the customer statement, emitted arguments or returned receipt in basis.
"""

import argparse
import csv
from collections import defaultdict
from functools import lru_cache
from io import StringIO
import json
from pathlib import Path
import re

from agent_memory_eval.analysis.decision_attribution import Action
from agent_memory_eval.analysis.recorded_decisions import require, sealed_bytes
from agent_memory_eval.analysis.selection import worker_choices
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.assets import asset_bytes, asset_text, publish_json
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path
from agent_memory_eval.study.adapters.bfcl_transfer import run_events, run_key, schedule
from agent_memory_eval.study.adapters.tau_banking import outcome_fields, recorded_stages, study_jobs
from agent_memory_eval.study.cohort import pool_sources
from agent_memory_eval.study.cohort_evidence import item_exposure
from agent_memory_eval.study.plan import load_plan, plan_digest
from agent_memory_eval.study.records import read, sealed_rows


BATCH = ROOT / "protocols/diagnostics/own-mixed-readers-smoke-01.json"


@lru_cache(maxsize=2)
def journal(path):
    return read_experiment(path)


def writer_note(benchmark, folder, index):
    run = index["writer_run"]
    if benchmark != "bfcl":
        shard = folder / f"task-{index['task']}__source-1" / "write"
        row, = [r for r in sealed_rows(shard)[2] if r["arm"] == index["form"]]
        folder = safe_path(shard, row["directory"])
        require(row["run_id"] == run, "Writer shard differs from released run")
        sealed_bytes(folder / "log.jsonl", row["result"]["log_sha256"])
    scan, state, _ = journal(folder)
    events = run_events(scan, run)
    saved, = [e for e in events if e["type"] == "memory.stored"]
    end, = [e["payload"] for e in events if e["type"] == "run.ended"]
    require(end["end_snapshot_id"] == index["snapshot"] and scan["starts"][run]["condition_id"] == index["form"]
            and scan["starts"][run]["payload"]["source_run_ids"] == [index["source_run"]],
            "Writer identity, first pass or snapshot differs from released index")
    item, = snapshot_items(BlobStore(folder), state, index["snapshot"])
    require((item["id"], item["version"]) == (saved["payload"]["item_id"], saved["payload"]["version_id"])
            and all(digest(item[f].encode()) == index[f + "_sha256"] for f in ("title", "body")),
            "Reader analysis note differs from the originally released writer version")
    return dict(source_task=index["task"], source_run=index["source_run"], writer_run=run, snapshot=index["snapshot"],
                item_id=item["id"], version_id=item["version"], title=item["title"], body=item["body"],
                stored_event=saved["event_id"], public_context_sha256=index["public_context_sha256"])


def tau_attempts(dest, record, case, plan):
    """Read the registered target only. Keep partial or unstarted allocation state."""
    stages = recorded_stages(dest)
    jobs = {j["arm"]: j for j in study_jobs(case, "solve")}
    result = defaultdict(list)
    for attempt in range(len(plan["retry"]["wait_seconds"]) + 1):
        stage = "solve" if attempt == 0 else f"solve-retry-{attempt}"
        shard = dest / ("task-" + case["task"]) / stage
        entry = stages.get(str(shard.relative_to(dest)), {})
        if (shard / "completion.json").is_file():
            _, control, rows = sealed_rows(shard, entry.get("seal"))
        elif (shard / "allocations.json").is_file():
            control, rows = read(shard / "control.json"), read(shard / "allocations.json")
        else:
            if entry.get("reason") and attempt == 0:
                for arm in jobs:
                    result[arm].append(dict(attempt=attempt, reason=entry["reason"]))
            continue
        require(control["profile"]["registration_sha256"] == digest(canonical(record))
                and control["profile"]["case"] == case["task"] and control["design"]["stage"] == "solve",
                "Reader shard is outside the original target registration")
        seen = set()
        for row in rows:
            arm = row["arm"]
            require(arm in jobs and arm not in seen and all(row[k] == v for k, v in jobs[arm].items()),
                    "Reader attempt differs from a registered slot")
            seen.add(arm)
            folder = safe_path(shard, row["directory"])
            item = dict(attempt=attempt, reason=row.get("reason") or row["status"])
            if (folder / "log.jsonl").is_file():
                require(row["status"] not in {"planned", "not_started"}, "Undispatched slot has reader evidence")
                checksum = (row.get("result") or {}).get("log_sha256")
                if checksum:
                    sealed_bytes(folder / "log.jsonl", checksum)
                scan = journal(folder)[0]
                if scan["starts"]:
                    require(set(scan["starts"]) == {row["run_id"]}, "Journal has a different reader run")
                    item.update(run=row["run_id"], folder=folder)
                else:
                    item["reason"] = "no_committed_reader_start"
            result[arm].append(item)
    return result


def reader_inputs(benchmark, plan, dest, task):
    record = read(dest / "registration.json")["preflight"]
    require(record["plan_sha256"] == plan_digest(plan), "Original registration does not match the pinned plan")
    if benchmark != "bfcl":
        case, = [c for c in record["cases"] if c["task_id"] == task and "donor" in c]
        return dest, tau_attempts(dest, record, case, plan)
    group, = [g for g in record["cohort_journals"] if task in g["tasks"]]
    folder = safe_path(dest, group["directory"])
    scan = journal(folder)[0]
    attempts = defaultdict(list)
    _, _, jobs = next(g for g in schedule(plan) if g[0] == task)
    for job in jobs:
        for attempt in range(len(plan["retry"]["wait_seconds"]) + 1):
            run = run_key(plan["id"], job, attempt)
            while run in scan["starts"]:
                attempts[job["condition"]].append(dict(attempt=attempt, run=run, folder=folder))
                run = run_key(plan["id"], run, "unsubmitted")
    return folder, attempts


def verify_pool(benchmark, run, condition, task, notes, evidence, blobs):
    scan, state, _ = evidence
    start = scan["starts"][run]
    head = start["payload"]
    require(start["condition_id"] == condition and head["fresh_session"], "Unexpected reader condition or session")
    items = snapshot_items(blobs, state, head["start_snapshot_id"])
    require(items == [dict(id=n["item_id"], version=n["version_id"], title=n["title"], body=n["body"]) for n in notes],
            "Assigned items differ from the sealed reader snapshot")
    if benchmark == "bfcl":
        assignment, = [e["payload"] for e in run_events(scan, run)
                       if e["payload"].get("origin") == "bfcl.cohort_assignment"]
        require(assignment["bank"] == condition.rsplit("__", 1)[1]
                and assignment["sources"] == [{k: n[k] for k in ("source_task", "source_run", "writer_run", "snapshot")}
                                              for n in notes]
                and head["source_run_ids"] == [parent for n in notes for parent in (n["source_run"], n["writer_run"])],
                "BFCL reader lineage differs from released writers")
    else:
        allocation = state["assignments"][run]
        lineage = blobs.get(allocation["ordered_writer_sources_ref"])
        require((allocation["task_id"], allocation["arm"]) == (task, condition)
                and head["source_run_ids"] == [n["writer_run"] for n in notes]
                and [(v["task_id"], v["source_run_id"], v["run_id"], v["snapshot_id"], v["heads"]) for v in lineage]
                == [(n["source_task"], n["source_run"], n["writer_run"], n["snapshot"], [[n["item_id"], n["version_id"]]])
                    for n in notes], "Native reader lineage differs from released writers")
    return items


def receipt_detail(name, content):
    """Compact, public projections for this audit. Every complete return also has a blob reference."""
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        value = None
    if isinstance(value, dict) and "address" in value:
        return {k: value[k] for k in ("order_id", "user_id", "status", "address") if k in value}
    if isinstance(value, dict) and "reservation_id" in value:
        return {k: value[k] for k in ("reservation_id", "cabin", "flights", "payment_history", "passengers") if k in value}
    if name in {"search_onestop_flight", "search_direct_flight"} and isinstance(value, list):
        itineraries = []
        for option in value:
            legs = option if isinstance(option, list) else [option]
            itineraries.append([{k: f[k] for k in ("flight_number", "date", "origin", "destination", "prices") if k in f}
                                for f in legs if isinstance(f, dict)])
        return itineraries
    if isinstance(content, str) and "transaction_id:" in content:
        records = [dict(re.findall(r"(?m)^\s+(transaction_id|merchant_name|transaction_date|transaction_amount): ([^\n]+)", part))
                   for part in re.split(r"(?m)^\s*\d+\. Record ID: ", content)[1:]]
        if records and all("transaction_id" in row for row in records):
            return records
    if isinstance(content, str) and len(content) > 2600:
        return dict(excerpt=content[:2600], full_characters=len(content))
    return content


def public_path(choices, events, blobs, items):
    """Number business responses only. Attach receipts to the actual issuing wire call."""
    receipts, native = defaultdict(list), defaultdict(list)
    starts, receipt_turns = {}, {}
    bfcl_decision = False
    for e in events:
        p = e["payload"]
        if e["type"] == "model.request_started" and p["actor_role"] == "worker":
            starts[p["request_id"]] = e["event_seq"]
        if e["type"] == "business.transition" and "action" in p and "public_message_ref" in p:
            msg = blobs.get(p["public_message_ref"])
            receipts[msg["tool_call_id"]].append((e, msg))
        if p.get("origin") == "forms.native_result":
            reference = p["native_report_ref"] or p.get("partial_trajectory_ref")
            if reference:
                transcript = blobs.get(reference)
                messages = transcript["messages"] if p["native_report_ref"] else transcript
                for i, msg in enumerate(messages):
                    if i and msg["role"] == "assistant":
                        signature = msg.get("content"), tuple(c["id"] for c in msg.get("tool_calls") or [])
                        native[signature].append(i)
        if e["type"] == "run.ended" and p["termination_reason"] == "business_decision":
            bfcl_decision = True
    confirmed = {i["id"]: set() for i in items}
    possible = {i["id"]: set() for i in items}
    turns, proof, customers, seen_customer = [], {}, [], set()
    for index, step in enumerate(choices):
        for source in step["public_inputs"]:
            if source["role"] == "user":
                key = source["producer_event"], source["sha256"]
                if key not in seen_customer:
                    customers.append(source["text"])
                    seen_customer.add(key)
        for f in step["visible_memory"]:
            if f["via"] == "self_authored" or step["delivery"] == "not_submitted":
                continue
            (confirmed if step["status"] == "response" else possible)[f["item_id"]].add(f["field"])
        if step["status"] != "response":
            continue
        reply = step["response"]
        calls = [c for c in reply.get("tool_calls") or [] if not c["function"]["name"].startswith("memory_")]
        if not calls and (reply.get("tool_calls") or not reply.get("content")):
            continue
        action = len(turns) + 1
        sources = dict(request=step["request_id"], wire_sha256=step["request_ref"]["sha256"],
                       response_sha256=step["response_ref"]["sha256"], manifest_sha256=step["manifest_ref"]["sha256"],
                       available_by_item={i: dict(confirmed=sorted(confirmed[i]), possible_only=sorted(possible[i] - confirmed[i]))
                                          for i in confirmed}, receipts=[])
        signature = reply.get("content"), tuple(c["id"] for c in calls)
        native_turns = native.get(signature)
        sources["native_message_index"] = native_turns.pop(0) if native_turns else None
        disposition = "native_public_message" if sources["native_message_index"] is not None else "emitted_only"
        if bfcl_decision and index == len(choices) - 1:
            disposition = "accepted_bfcl_decision"
        seen_receipts = [receipt_turns[f["producer_event"]] for f in step["public_inputs"]
                         if f["role"] == "tool" and f["producer_event"] in receipt_turns]
        public_calls = []
        for call in calls:
            parsed = Action.from_function(call["function"]).record()
            if isinstance(parsed.get("arguments"), dict) and isinstance(parsed["arguments"].get("arguments"), str):
                try:
                    parsed["arguments"]["arguments"] = json.loads(parsed["arguments"]["arguments"])
                except ValueError:
                    pass
            start = starts[step["request_id"]]
            stop = starts[choices[index + 1]["request_id"]] if index + 1 < len(choices) else float("inf")
            matches = [(e, m) for e, m in receipts[call["id"]] if start < e["event_seq"] < stop
                       and e["payload"]["action"] == parsed["name"]]
            require(len(matches) <= 1, "Ambiguous public receipt for a worker call")
            parsed["returned"] = None
            if matches:
                e, m = matches[0]
                raw_args = Action.from_function(call["function"]).record().get("arguments")
                require(raw_args == e["payload"]["arguments"], "Executed tool arguments differ from the emitted call")
                parsed["returned"] = receipt_detail(parsed["name"], m["content"])
                sources["receipts"].append(dict(call=call["id"], event=e["event_id"],
                                                blob_sha256=e["payload"]["public_message_ref"]["sha256"]))
                receipt_turns[e["event_id"]] = [action, len(public_calls) + 1]
            public_calls.append(parsed)
        turns.append(dict(turn=action, disposition=disposition, customer=customers, worker=reply.get("content"),
                          business_calls=public_calls, prior_receipts_in_request=seen_receipts))
        proof[str(action)] = sources
        customers = []
    return turns, proof


def task_witnesses(benchmark, turns, target):
    """Literal observations. The semantic interpretation is recorded separately by the reviewer."""
    calls = [(t["turn"], c) for t in turns for c in t["business_calls"]]
    if benchmark == "bfcl":
        return dict(emitted_business_calls=[dict(turn=n, function=c["name"]) for n, c in calls])
    if benchmark == "retail":
        tools = {"get_user_details", "get_order_details", "modify_user_address", "modify_pending_order_address"}
        return dict(address_records=[dict(turn=n, **c) for n, c in calls if c["name"] in tools])
    if benchmark == "airline":
        prices, quotes, records = [], [], []
        for n, c in calls:
            result = c["returned"]
            if c["name"] in {"get_reservation_details", "update_reservation_flights"} and isinstance(result, dict):
                records.append(dict(turn=n, operation=c["name"], **result))
            if c["name"] == "calculate":
                quotes.append(dict(turn=n, calculation=c))
            if c["name"] in {"search_direct_flight", "search_onestop_flight"} and isinstance(result, list):
                for route in result:
                    for leg in route:
                        if leg.get("date") == "2024-05-24" and leg.get("flight_number") in {"HAT190", "HAT047"}:
                            if (row := dict(turn=n, **leg)) not in prices:
                                prices.append(row)
        return dict(audited_outbound_search_rows=prices, calculations=quotes, reservation_records=records)
    history, filings = {}, []
    baseline = {row[3]: row for row in target["source_public_disputes"]}
    for n, c in calls:
        result, args = c["returned"], c.get("arguments")
        if c["name"] == "get_credit_card_transactions_by_user" and isinstance(result, list):
            position = turns[n - 1]["business_calls"].index(c) + 1
            history.update({v["transaction_id"]: dict(lookup_turn=n, lookup_call=position, **v) for v in result})
        if not isinstance(args, dict) or c["name"] != "call_discoverable_agent_tool":
            continue
        if not (args.get("agent_tool_name") == "file_credit_card_transaction_dispute_4829"
                and isinstance((a := args.get("arguments")), dict)):
            continue
        tid = a.get("transaction_id")
        receipt = re.search(r"(?m)^Transaction: (\S+)", result) if isinstance(result, str) else None
        record = history.get(tid)
        visible = record and [record["lookup_turn"], record["lookup_call"]] in turns[n - 1]["prior_receipts_in_request"]
        filings.append(dict(turn=n, transaction_id=tid, date=a.get("purchase_date"), reason=a.get("dispute_reason"),
                            reader_lookup_before=record if visible else None,
                            round1_comparator=baseline.get(tid),
                            filed_receipt_transaction=receipt.group(1) if receipt else None, returned=result))
    return dict(filings=filings)


def observed_attempt(benchmark, task, condition, target, notes, attempt, workspace):
    folder, run = attempt["folder"], attempt["run"]
    evidence = journal(folder)
    scan, _, _ = evidence
    events = run_events(scan, run)
    blobs = BlobStore(folder)
    items = verify_pool(benchmark, run, condition, task, notes, evidence, blobs)
    choices = worker_choices(folder, run_id=run, evidence=evidence)["choices"]
    exposure = item_exposure([{k: n[k] for k in ("source_task", "writer_run", "item_id", "version_id")} for n in notes],
                             choices, events, None, contents=items, blobs=blobs)
    turns, proof = public_path(choices, events, blobs, items)
    score = next((e for e in events if e["type"] == "outcome.scored"), {})
    official = score.get("payload", {})
    end = next((e["payload"] for e in events if e["type"] == "run.ended"), {})
    reason = end.get("termination_reason", "interrupted")
    success = official.get("official_success")
    grade = None
    if benchmark == "bfcl" and type(success) is bool:
        grade = blobs.get(official["official_artifact_ref"])
        require(grade["metric"] == "bfcl.relevance.call_presence/v1" and grade["valid"] is success and turns
                and grade["any_call"] is bool(turns[-1]["business_calls"]), "BFCL native call-presence record differs")
    if benchmark != "bfcl":
        error = next((e["payload"]["error_code"] for e in events if e["payload"].get("origin") == "forms.native_result"), None)
        _, success, _, _ = outcome_fields(dict(success=success, reason=error or reason, status="interrupted"))
    # Mask assignment, note contents, private reads, provider reasoning and grade in the behavior packet.
    packet = dict(benchmark=benchmark, task=task, axes=target["axes"], public_turns=turns,
                  complete_business_path=official.get("grading_status") == "graded"
                  and reason in {"user_stop", "agent_stop", "business_decision"},
                  observations=task_witnesses(benchmark, turns, target))
    packet_id = digest(canonical(["reader-action-review/v1", run]))[:24]
    item = dict(attempt=attempt["attempt"], run=run, journal=str(folder.relative_to(workspace)),
                reason=reason, official_success=success, outcome_event=score.get("event_id"),
                raw_official_success=official.get("official_success"), bfcl_call_presence=grade and grade["any_call"],
                exposure=exposure, action_proof=proof, packet=packet_id, packet_sha256=digest(canonical(packet)),
                worker_requests=len(choices), answered_worker=sum(s["status"] == "response" for s in choices))
    return item, dict(packet, packet_sha256=item["packet_sha256"])


def apply_review(target, rules, packet, attempt, review, note_matches, notes):
    axes = target["axes"]
    default = "pending_review" if packet["public_turns"] else "no_observed_business_response"
    result = {key: [dict(fate=default)] for key in axes}
    if not review:
        require(not note_matches, "An unblinded note match needs a prior masked behavior review")
        return result
    require(review["packet_sha256"] == attempt["packet_sha256"] and set(review["axes"]) <= axes.keys(),
            "Review belongs to another packet or decision axis")
    turns = {t["turn"] for t in packet["public_turns"]}
    for axis, items in review["axes"].items():
        require(isinstance(items, list) and items, "An axis review needs one or more findings")
        result[axis] = []
        for entry in items:
            fate, action, cites, basis = (entry[k] for k in ("fate", "action", "evidence", "basis"))
            require(fate in rules["fates"] and isinstance(basis, str) and basis.strip() and isinstance(cites, list)
                    and all(type(n) is int and n in turns for n in cites)
                    and (action is None or type(action) is int and action in cites)
                    and (action is not None or fate in {"no_opportunity", "unclear"})
                    and (cites or fate == "unclear") and (fate != "no_opportunity" or
                    (action is None and packet["complete_business_path"])), "Unwitnessed or invalid behavioral finding")
            if axis == "movie_title_question" and fate in {"repeated", "corrected_or_reverified"}:
                require(packet["public_turns"][action - 1]["disposition"] == "accepted_bfcl_decision",
                        "The movie-title phenotype needs a public BFCL decision")
            before = attempt["action_proof"][str(action)]["available_by_item"] if action is not None else None
            result[axis].append(dict(fate=fate, action=action, evidence=cites, basis=basis, before=before, note_matches=[]))
    for match in note_matches:
        axis, i, wr, field, quote = (match[k] for k in ("axis", "finding_index", "writer_run", "field", "quote"))
        require(axis in review["axes"] and type(i) is int and 0 <= i < len(result[axis]),
                "Note match refers to an unreviewed finding")
        finding = result[axis][i]
        note = next((n for n in notes if n["writer_run"] == wr), None)
        require(note and finding["before"] is not None and field in {"title", "body"}
                and field in finding["before"][note["item_id"]]["confirmed"]
                and isinstance(quote, str) and quote.strip() and quote in note[field],
                "Note match requires a literal cue in a confirmed pre-action field from this slot")
        finding["note_matches"].append(dict(source_task=note["source_task"], writer_run=wr, field=field, quote=quote))
    return result


def analyze(workspace, reviews, matches):
    journal.cache_clear()
    batch = read(BATCH)
    rules = read_json(asset_bytes(ROOT, batch["action_review"]))
    writer_index = {(r["benchmark"], r["task"], r["form"]): r
                    for r in csv.DictReader(StringIO(asset_text(ROOT, rules["writer_index"])))}
    audit = {(r["benchmark"], r["task"], r["form"]): r
             for r in csv.DictReader(StringIO(asset_text(ROOT, rules["content_audit"])))}
    rows, packets, writers = [], {}, {}
    for benchmark, target in rules["targets"].items():
        study, task = target["study"], target["task"]
        part = batch["plans"][study]
        require(part["tasks"] == [task] and part["benchmark"] == benchmark
                and part["plan"]["sha256"] == target["plan_file_sha256"], "Batch scope differs from the rubric")
        asset_bytes(ROOT, part["plan"])
        plan = load_plan(ROOT / part["plan"]["path"], ROOT)
        dest = workspace / "runs" / study
        writer_folder, attempts = reader_inputs(benchmark, plan, dest, task)
        directory = dest / "reader-batches" / batch["id"]
        if (directory / "registration.json").is_file():
            registration = read(directory / "registration.json")
            require(registration["batch_ref"]["sha256"] == digest(BATCH.read_bytes()) and registration["tasks"] == [task],
                    "Reader batch was registered with different rules or tasks")
        pool = pool_sources(plan, task)
        catalog = {}
        for source in pool:
            for form in target["body_claim"]:
                key = benchmark, source, form
                writer = writer_index[key]
                require(writer["status"] == "stored" and audit[key]["writer_run"] == writer["writer_run"]
                        and audit[key]["body_sha256"] == writer["body_sha256"], "Content review refers to another memory")
                note = writer_note(benchmark, writer_folder, writer)
                catalog[source, form] = writers[note["writer_run"]] = note
        _, _, jobs = next(g for g in schedule(plan) if g[0] == task)
        require(len(jobs) == 12, "Diagnostic requires the original twelve slots of each case")
        for job in sorted(jobs, key=lambda j: j["condition"]):
            form, arm = job["form"], job["condition"]
            notes = [catalog[s, form] for s in (pool if job["bank"] == "mixed" else [task])]
            assigned = [dict(source_task=n["source_task"], writer_run=n["writer_run"], item_id=n["item_id"],
                             version_id=n["version_id"], own=n["source_task"] == task) for n in notes]
            observations = []
            for candidate in attempts.get(arm, []):
                if "run" not in candidate:
                    observations.append(candidate)
                    continue
                require((directory / "registration.json").is_file(), "Observed reader lacks its batch registration")
                a, packet = observed_attempt(benchmark, task, arm, target, notes, candidate, workspace)
                ident = a["packet"]
                require(ident not in packets, "A reader packet was attached to more than one attempt")
                packets[ident] = packet
                a["findings"] = apply_review(target, rules, packet, a, reviews.get(ident), matches.get(ident, []), notes)
                observations.append(a)
            with_run = [a for a in observations if "run" in a]
            chosen = next((a for a in with_run if a["official_success"] is not None), (with_run or [None])[-1])
            status = "not_started"
            if chosen:
                status = "business_response" if chosen["action_proof"] else "started_without_business_response"
            rows.append(dict(benchmark=benchmark, task=task, form=form, reading=job["reader"], bank=job["bank"],
                             condition=arm, draw=job["draw"], repeat=job["repeat"], assigned=assigned,
                             assigned_claim=target["body_claim"][form], attempts=observations,
                             selected_run=chosen and chosen["run"], official_success=chosen and chosen["official_success"],
                             findings=chosen["findings"] if chosen else {k: [dict(fate="unobserved")] for k in target["axes"]},
                             status=status))
    require(len(rows) == 48 and reviews.keys() <= packets.keys() and matches.keys() <= reviews.keys(),
            "Unknown review packet, or the original 48-slot denominator changed")
    return dict(rules=batch["action_review"], batch=batch["id"], writers=writers, rows=rows), dict(sorted(packets.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=Path, default=ROOT, help="Workspace containing the original run journals")
    parser.add_argument("--out", required=True, type=Path, help="Directory for new analysis files")
    parser.add_argument("--reviews", type=Path, help="Condition-masked public-turn annotations")
    parser.add_argument("--note-matches", type=Path, help="Optional post-review literal matches, keyed by packet ID. "
                        "Each entry is a list of {axis, finding_index (zero based), writer_run, field, quote}.")
    args = parser.parse_args()
    reviews = read(args.reviews) if args.reviews else {}
    result, packets = analyze(args.workspace.resolve(), reviews, read(args.note_matches) if args.note_matches else {})
    args.out.mkdir(parents=True, exist_ok=True)
    for filename, values in (("blinded-packets.json", packets), ("reader-actions.json", result)):
        path = args.out / filename
        if path.exists():
            require(read(path) == values, f"Choose a new output directory for changed {filename}")
        else:
            publish_json(path, values)
    print(json.dumps(dict(assigned=len(result["rows"]), started=sum(r["selected_run"] is not None for r in result["rows"]),
                          with_business_response=sum(r["status"] == "business_response" for r in result["rows"]),
                          packets=len(packets), reviewed_packets=sum(bool(review["axes"]) for review in reviews.values()))))


if __name__ == "__main__":
    main()
