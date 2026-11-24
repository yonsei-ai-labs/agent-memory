"""Reconcile the banking pilot against the registered banks, actual requests and official grades."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import json
from pathlib import Path
import re

from agent_memory_eval.analysis.action_grounding import argument_features, input_evidence, literal_matches
from agent_memory_eval.analysis.claim_evidence import paired_counts, trace_claim
from agent_memory_eval.analysis.decision_attribution import compare_episodes, compare_steps
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.analysis.selection import allocation_exposure
from agent_memory_eval.analysis.tau2_attribution import graded_episode
from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.recording.journal import safe_path
from scripts import run_banking_memory as pilot
from scripts.report_fresh_matched import episode
from scripts.run_full_retail import matches_accounting_seal
from scripts.summarize_fresh_matched import verify_writer_input


def document_ids(sources):
    return {match for origin, text in sources if origin == "current_document"
            for match in re.findall(r"^\s*ID: ([^\s]+)\s*$", text, re.MULTILINE)}


def optional_storage(workspace):
    """Independent diagnosis of save attempts, accepted versions and affected reader requests."""
    study = pilot.matched
    root = workspace/"runs/tau-banking-memory-01"
    reg = study.read(root/"registration.json")["preflight"]
    cfg = study.read(pilot.ROOT/study.PROFILE)
    common = study.asset_text(pilot.ROOT, cfg["writing"]["common_instruction_ref"]).rstrip("\r\n")
    prompts = {f: digest((common+"\n"+v).encode()) for f,v in cfg["writing"]["lines"].items()}
    authors, affected, inputs = [], [], {}
    readers = Counter()
    for case in reg["cases"]:
        tid = case["task_id"]
        base = root / ("task-" + tid)
        source = study.source_from(base/"source", tid)
        first, = study.sealed_rows(base/"source")[2]
        original = episode(base/"source"/first["directory"], first, case, digest(canonical(reg)), None, None)
        _, _, rows = study.sealed_rows(base/"write")
        inputs[tid] = source.public_context_sha256
        by_form = {}
        for row in rows:
            form = row["arm"]
            require(verify_writer_input(base/"write", row, source, dict(writer_prompt_sha256=prompts)),
                    "Unobserved optional writer")
            folder = base/"write"/row["directory"]
            checksum = row["result"]["log_sha256"]
            bank = read_form_bank(folder, expected_log_sha256=checksum)
            by_form[form] = bank
            log = RecordedDecisions(folder, row["run_id"], checksum)
            replies = [log.reply(req)["message"] for req in log.requests]
            store_calls = sum(c["function"]["name"] == "memory_store" for msg in replies for c in msg.get("tool_calls") or [])
            author = dict(task=tid, form=form, writer_run=row["run_id"], writer_log=checksum,
                          source_run=source.source_run_id, source_public_sha256=bank["public_context_sha256"],
                          requests=len(log.requests), store_calls=store_calls, stored_events=len(log.of_type("memory.stored")),
                          items=len(bank["items"]), snapshot=bank["snapshot_id"])
            require(author["source_public_sha256"] == source.public_context_sha256, "Wrong optional trajectory")
            if not bank["items"]:
                text = replies[-1].get("content") or ""
                from agent_memory_eval.memory.writing import REQUIRED_STORE_CONTRACT, writer_response_error
                finished = log.finishes[log.requests[-1]["request_id"]]
                raw_reply = log.blobs.get(finished["response_ref"])
                required_error = writer_response_error(raw_reply, observed=True, completion_contract=REQUIRED_STORE_CONTRACT,
                    model_identity=log.state["memory_writes"][row["run_id"]]["verified_model"])
                require(required_error == "WRITER_REQUIRED_STORE_MISSING", "Original failure escaped the new writer check")
                author.update(final_text_chars=len(text), final_text_sha256=digest(text.encode()),
                              final_text_excerpt=text[:160], provider_request=log.requests[-1]["request_id"],
                              required_contract_replay=required_error)
            authors.append(author)
        for trial in study.read(base/"solve/allocations.json"):
            form, mode = trial["arm"].split("__")
            bank = by_form[form]
            job = base/"solve"/trial["directory"]
            if not trial.get("result"):
                require(not (job/"log.jsonl").exists() and not bank["items"], "Unaccounted optional reader")
                affected.append(dict(arm=trial["arm"], task=tid, status="stopped_before_first_request"))
                readers["stopped_before_request"] += 1
                continue
            seen = episode(job, trial, case, digest(canonical(reg)), source.source_run_id, bank)
            require(seen["task_start_sha256"] == original["task_start_sha256"] and
                    seen["source_worker_request_id"] == original["first_worker_request_id"],
                    "Reader was attached to a different first decision")
            readers["matched_opening_and_writer"] += 1
            readers["nonempty_assigned" if bank["items"] else "empty_assigned"] += 1
            readers[mode + "_graded"] += int(seen["status"] == "graded")
            readers[mode + "_body_seen"] += int(bool(seen["body_seen"]))
            readers[mode + "_read_calls"] += seen["reads"] or 0
            if not bank["items"]:
                affected.append(dict(arm=trial["arm"], task=tid, run=trial["run_id"], status=trial["status"],
                                     body_seen=seen["body_seen"], success=seen["success"], snapshot=bank["snapshot_id"]))
    # A post-hoc case study with a falsifiable literal from the worker's first action.
    case = next(c for c in reg["cases"] if c["task_id"] == "task_015")
    base = root / ("task-" + case["task"])
    writer = next(r for r in study.sealed_rows(base/"write")[2] if r["arm"] == "lesson")
    reader = next(r for r in study.sealed_rows(base/"solve")[2] if r["arm"] == "lesson__preload")
    first, = study.sealed_rows(base/"source")[2]
    wdir, rdir = base/"write"/writer["directory"], base/"solve"/reader["directory"]
    bank = read_form_bank(wdir, expected_log_sha256=writer["result"]["log_sha256"])
    item, = bank["items"]
    histories = {}
    second, _ = graded_episode(base/"solve", reader, case, cfg["delivery_instruction"]["sha256"], first,
                               public_inputs=histories)
    original, _ = graded_episode(base/"source", first, case, cfg["delivery_instruction"]["sha256"])
    a, b = original[0], second[0]
    literal = "get_referral_link"
    require(a.history == b.history and a.controls == b.controls and len(a.actions) == len(b.actions) == 1
            and a.actions[0].name == b.actions[0].name == "grep", "Case study is not one matched search decision")
    log = RecordedDecisions(rdir, reader["run_id"], reader["result"]["log_sha256"])
    wire = log.blobs.get(log.starts[b.request][0]["payload"]["request_ref"])
    require(not literal_matches(literal, input_evidence(histories[b.request], wire["tools"], {"grep"})) and
            literal not in a.actions[0].record()["arguments"]["pattern"], "Literal also appears in a public source")
    evidence = trace_claim(wdir, rdir, item=item["id"], version=item["version"], field="body",
        clause="get_referral_link(user_id, card_name)", request_id=b.request,
        source_citations=[("/experience/messages/3/content", "get_referral_link(user_id: str, card_name: str)")],
        action_citations=[("/tool_calls/0/function/arguments", literal)])
    require(evidence["decision"]["exposure"], "Case study lacks actual input exposure")
    example = dict(task=case["task_id"], form=reader["arm"], reference_request=a.request,
                   first_search=a.actions[0].record(), second_search=b.actions[0].record(),
                   first_success=first["result"]["official_success"], second_success=reader["result"]["official_success"],
                   competing_public_literal_sources=[], provenance=evidence)
    return dict(source_public_hashes=inputs, authors=authors, reader_audit=dict(readers),
                empty_bank_readers=affected, exploratory_example=example)


def decision_evidence(log, steps, histories, source_fields, required):
    read_counts = Counter()
    matched = []
    documents = set()
    seen_fields = set()
    for req in log.requests:
        if req["actor_role"] != "worker":
            continue
        reply = log.reply(req)["message"]
        read_counts.update(c["function"]["name"] for c in reply.get("tool_calls") or [])
        seen_fields.update(s["field"] for s in log.spans[req["request_id"]])
    for index, step in enumerate(s for s in steps if s.actor == "worker"):
        req = log.starts[step.request][0]["payload"]
        wire = log.blobs.get(req["request_ref"])
        tools = [t for t in wire.get("tools", []) if not t["function"]["name"].startswith("memory_")]
        current = input_evidence(histories[step.request], tools, {"grep"})
        documents.update(document_ids(current))
        fields = [(f'{s["item_id"]}/{s["version_id"]}/{s["field"]}',
                   log.versions[s["item_id"], s["version_id"]][s["field"]]) for s in log.spans[step.request]]
        for action in step.actions:
            for pointer, kind, value in argument_features(action.record().get("arguments")):
                support = literal_matches(value, fields)
                if support:
                    matched.append(dict(request=step.request, worker_decision=index+1, action=action.name,
                                        pointer=pointer, matching_rule=kind, value=value,
                                        visible_note_fields=support, first_pass_sources=literal_matches(value, source_fields),
                                        current_sources=literal_matches(value, current)))
    return dict(calls=dict(read_counts), exposed_fields=sorted(seen_fields), documents_seen=len(documents),
                tagged_documents_seen=sorted(documents & required), literal_argument_matches=matched)


def native_checks(log, expected_success):
    score, = log.of_type("outcome.scored")
    info = log.blobs.get(score["payload"]["official_artifact_ref"])["reward_info"]
    require((info["reward"] == 1) == expected_success == score["payload"]["official_success"],
            "Official banking reward differs from the observed grade")
    return [dict(id=c["action"]["action_id"], actor=c["action"]["requestor"],
                 name=c["action"]["name"], matched=c["action_match"]) for c in info["action_checks"]]


def referral_search_citations(base, data, banks, trials):
    """Post-hoc literal provenance at the first matched decision in two referral-link trials."""
    clauses = {"episode": "user-facing get_referral_link tool",
               "fact": "their own link via get_referral_link(user_id, card_name)"}
    citations = {}
    for form, clause in clauses.items():
        arm = form + "__preload"
        trial = next(r for r in trials if r["task"] == "task_015" and r["arm"] == arm)
        choice = (trial.get("divergence") or {}).get("first_business_response") or {}
        evidence = [m for m in trial.get("literal_argument_matches", [])
                    if m["worker_decision"] == 1 and m["request"] == choice.get("candidate_request")
                    and m["value"] == "get_referral_link" and m["action"] == "grep" and not m["current_sources"]]
        if not evidence or choice.get("status") != "matched_visible_memory":
            continue
        writer = next(w for w in data["write"] if w["arm"] == form)
        reader = next(r for r in data["solve"] if r["arm"] == arm)
        item, = banks[form]["items"]
        trace = trace_claim(base/"write"/writer["directory"], base/"solve"/reader["directory"],
                            item=item["id"], version=item["version"], field="body", clause=clause,
                            request_id=choice["candidate_request"],
                            source_citations=[("/experience/messages/3/content", "get_referral_link(user_id: str, card_name: str)")],
                            action_citations=[("/tool_calls/0/function/arguments", "get_referral_link")])
        require(trace["decision"]["exposure"], "Referral tool name was not visible in the first search")
        citations[arm] = trace
    return citations


def summarize(sources, writers, trials):
    baselines = {r["task"]:r for r in sources}
    by_arm = {r["arm"] for r in trials}
    def contrast(rows):
        graded = [r for r in rows if r["success"] is not None]
        joint = [r for r in graded if r["success"] and r["baseline_success"]]
        changed = [r["divergence"]["first_business_response"] for r in graded]
        stats = {}
        for metric in ("customer_turns", "business_calls", "tokens", "usd", "document_searches"):
            def value(row):
                if metric == "document_searches":
                    return row["calls"].get("grep", 0)
                return row["effort"].get(metric)
            pairs = [(value(baselines[r["task"]]), value(r)) for r in joint]
            values = [(Decimal(str(a)), Decimal(str(b))) for a,b in pairs if a is not None and b is not None]
            stats[metric] = dict(pairs=len(values), first=str(sum((a for a,b in values), Decimal(0))),
                                 second=str(sum((b for a,b in values), Decimal(0))))
        return dict(outcomes=paired_counts([(r["success"],r["baseline_success"]) for r in rows]),
                    graded=len(graded), success=sum(r["success"] for r in graded),
                    with_title=sum("title" in r.get("exposed_fields", ()) for r in rows),
                    with_body=sum("body" in r.get("exposed_fields", ()) for r in rows),
                    list_calls=sum(r.get("calls", {}).get("memory_list", 0) for r in rows),
                    get_calls=sum(r.get("calls", {}).get("memory_get", 0) for r in rows),
                    matched_first_business=sum(v is not None and v.get("status") == "matched_visible_memory" for v in changed),
                    joint_success_effort=stats)
    by_cell = {(r["task"],r["arm"]):r for r in trials}
    same_bank = [(by_cell[w["task"],w["form"]+"__preload"]["success"],
                  by_cell[w["task"],w["form"]+"__on_demand"]["success"]) for w in writers]
    return dict(stored_forms=sum(bool(w["items"]) for w in writers),
                by_arm={arm:contrast([r for r in trials if r["arm"]==arm]) for arm in sorted(by_arm)},
                by_delivery={mode:contrast([r for r in trials if r["arm"].endswith("__"+mode)]) for mode in pilot.matched.MODES},
                on_demand_to_preload_same_bank=paired_counts(same_bank))


def feedback_contrasts(current, previous):
    """Pair the two writer policies within task, form and delivery; keep missing grades in the denominator."""
    def by_key(rows):
        indexed = {(r["task"], r["arm"]): r for r in rows}
        require(len(indexed) == len(rows), "Duplicate feedback comparison allocation")
        return indexed
    old, new = by_key(previous), by_key(current)
    require(new.keys() == old.keys(), "Feedback cohorts differ")
    def group(keys):
        pairs = [(old[k], new[k]) for k in keys]
        counts = paired_counts([(b["success"], a["success"]) for a, b in pairs])
        both = [(a, b) for a, b in pairs if a["success"] is True and b["success"] is True]
        effort = {}
        for metric in ("customer_turns", "business_calls", "tokens", "usd", "document_searches"):
            def value(row):
                return row["calls"].get("grep", 0) if metric == "document_searches" else row["effort"].get(metric)
            vals = [(Decimal(str(value(a))), Decimal(str(value(b)))) for a, b in both
                    if value(a) is not None and value(b) is not None]
            effort[metric] = dict(pairs=len(vals), none=str(sum((a for a, b in vals), Decimal(0))),
                                  result_only=str(sum((b for a, b in vals), Decimal(0))))
        return dict(outcomes=counts, paired=counts["assigned"] - counts["missing"],
                    none_success=counts["both_success"] + counts["loss"],
                    result_only_success=counts["both_success"] + counts["gain"], joint_success_effort=effort)
    return dict(by_delivery={m: group(k for k in new if k[1].endswith("__" + m)) for m in pilot.matched.MODES},
                by_task={t: {m: group(k for k in new if k[0] == t and k[1].endswith("__" + m))
                             for m in pilot.matched.MODES} for t in sorted({k[0] for k in new})},
                by_form={f: {m: group(k for k in new if k[1] == f + "__" + m)
                             for m in pilot.matched.MODES} for f in pilot.matched.FORMS})


def analyze(workspace, *, exposure_report=False, result_feedback=False):
    study = pilot.matched
    profile, preflight, approval = ((pilot.FEEDBACK_PROFILE, pilot.FEEDBACK_PREFLIGHT, pilot.FEEDBACK_APPROVAL)
                                    if result_feedback else (pilot.REQUIRED_PROFILE, pilot.REQUIRED_PREFLIGHT, pilot.REQUIRED_APPROVAL))
    protocol = study.read(pilot.ROOT / profile)
    root = workspace / "runs" / protocol["name"]
    completion = study.read(root / "completion.json")
    reg = study.read(root / "registration.json")
    if result_feedback and reg["preflight"].get("scope") == "writing":
        preflight = pilot.FEEDBACK_WRITING_PREFLIGHT
    require(completion["registration_sha256"] == digest((root / "registration.json").read_bytes()), "Bad registration seal")
    frozen = study.read(pilot.ROOT / preflight)
    approved = dict(frozen, authorization=study.read(pilot.ROOT / approval))
    require(reg["preflight"] == approved and frozen["protocol_sha256"] == digest((pilot.ROOT / profile).read_bytes()),
            "Cohort changed after registration")
    rid = digest(canonical(approved))
    required_protocol = json.loads(study.asset_text(pilot.ROOT, protocol["required_protocol"])) if result_feedback else protocol
    parent = json.loads(study.asset_text(pilot.ROOT, required_protocol["parent"]))
    cfg = pilot.required_design(json.loads(study.asset_text(pilot.ROOT, parent["base"])), required_protocol)
    stages = study.STAGES
    if result_feedback:
        cfg["writer_feedback_level"] = protocol["writer_feedback_level"]
        previous = json.loads(study.asset_text(pilot.ROOT, protocol["comparison_results"]))
        previous_trials = {(r["task"], r["arm"]): r for r in previous["trials"]}
        stages = ["source", *protocol["scopes"][frozen["scope"]]]
    common = study.asset_text(pilot.ROOT, cfg["writing"]["common_instruction_ref"]).rstrip("\r\n")
    prompts = {f: digest((common + "\n" + line).encode()) for f, line in cfg["writing"]["lines"].items()}
    read_sha = cfg["delivery_instruction"]["sha256"]
    costs = defaultdict(lambda: defaultdict(Decimal))
    cells, writers, firsts = [], [], []
    observations = []
    examples = {}
    session_ids = set()
    for case in frozen["cases"]:
        tid = case["task_id"]
        base = root / ("task-" + tid)
        raw = study.read(workspace / "data/tau2-bench/data/tau2/domains/banking_knowledge/tasks" / (tid + ".json"))
        required = set(raw["required_documents"])
        imported = case["imported_first_pass"]
        source_dir = safe_path(workspace/"runs", imported["directory"])
        data = {"solve": []}
        if result_feedback and "solve" in stages:
            old_folder = workspace/"runs"/required_protocol["name"]/("task-" + tid)/"solve"
            old_readers = {r["arm"]:r for r in study.sealed_rows(old_folder)[2]}
        for phase in stages:
            jobs = study.jobs_for(case, phase)
            if phase == "source":
                _, control, rows = study.sealed_rows(source_dir)
                require(len(rows) == 1 and rows[0]["run_id"] == imported["run_id"] and
                        rows[0]["result"]["log_sha256"] == imported["log_sha256"] and
                        control["profile"]["registration_sha256"] == imported["registration_sha256"],
                        "Wrong imported first pass")
                data[phase] = rows
                continue
            folder = base / phase
            seal = completion["stages"][str(folder.relative_to(root))]
            if seal.get("status") == "not_started":
                require(not folder.exists(), "Unfunded shard unexpectedly exists")
                data[phase] = [dict(j, status="not_started", reason=seal["reason"]) for j in jobs]
            else:
                _, control, rows = study.sealed_rows(folder)
                require(matches_accounting_seal(folder, control["profile"]["guards"]["max_usd"], seal)
                        and control["profile"]["registration_sha256"] == rid
                        and len(rows) == len(jobs) and all(all(r[k] == v for k,v in j.items()) for r,j in zip(rows,jobs)),
                        "Shard accounting, task or jobs differ")
                data[phase] = rows
            for key in ("reported", "held", "spent", "http"):
                costs[phase][key] += Decimal(str(seal[key]))
        first, = data["source"]
        source = study.source_from(source_dir, tid)
        require(source.public_context_sha256 == imported["public_context_sha256"], "Imported public experience changed")
        public = json.loads(source.public_context) if source else None
        source_fields = input_evidence([public["task_context"], *public["messages"]], [], {"grep"}) if public else []
        by_form = {}
        for w in data["write"]:
            result = w.get("result") or {}
            bank = None
            registrar = dict(writer_prompt_sha256=prompts, writer_feedback_level=cfg.get("writer_feedback_level", "none"))
            if result_feedback:
                registrar["first_writer_inputs"] = case["first_writer_inputs"]
            if result.get("log_sha256"):
                verify_writer_input(base / "write", w, source, registrar)
                if result.get("status") in {"completed", "empty"}:
                    bank = read_form_bank(base / "write" / w["directory"], expected_log_sha256=result["log_sha256"])
                    if result_feedback:
                        require(bank.get("result_feedback") == source.result_receipt(), "Wrong writer outcome receipt")
            by_form[w["arm"]] = bank
            writers.append(dict(task=tid, form=w["arm"], status=result.get("status", w["status"]),
                                run=w.get("run_id"), log_sha256=result.get("log_sha256"),
                                source_public_sha256=source.public_context_sha256 if source else None,
                                items=bank["items"] if bank else [], snapshot=bank["snapshot_id"] if bank else None))
        baseline = None
        baseline_steps = []
        for phase, rows in (("source", [first]), ("solve", data["solve"])):
            for row in rows:
                result = row.get("result") or {}
                is_second = phase == "solve"
                bank = by_form[row["arm"].split("__")[0]] if is_second else None
                rec = dict(task=tid, arm=row["arm"], run=row.get("run_id"), success=result.get("official_success"),
                           status=result.get("grading_status", row["status"]), error=result.get("error_code") or row.get("reason"))
                observation = dict(state="not_started" if row["status"] == "not_started" else "unavailable")
                if observation["state"] == "not_started" and row.get("directory"):
                    require(not (safe_path(base/phase, row["directory"])/"log.jsonl").exists(),
                            "Unstarted allocation has a journal")
                if result.get("log_sha256"):
                    parent_dir = base / phase if is_second else source_dir
                    folder = safe_path(parent_dir, row["directory"])
                    e = episode(folder, row, case, rid if is_second else imported["registration_sha256"],
                                first.get("run_id") if is_second else None, bank, include_exposure=is_second)
                    require(e["session_id"] not in session_ids, "Shared worker session")
                    session_ids.add(e["session_id"])
                    rec.update(log_sha256=e["log_sha256"], opening=e.get("task_start_sha256"), effort=e["effort"],
                               setup=e.get("setup_requests"), body=e["body_seen"], read_calls=e["reads"])
                    if is_second:
                        observation = e["worker_exposure"]
                        if rec["success"] is None:
                            rec["exposed_fields"] = observation["confirmed"]
                            rec["calls"] = {k: observation["counts"][k] for k in ("memory_list", "memory_get")
                                            if k in observation["counts"]}
                    if rec["success"] is not None:
                        histories = {}
                        steps, _ = graded_episode(parent_dir, row, case, read_sha, first if is_second else None,
                                                  public_inputs=histories)
                        log = RecordedDecisions(folder, row["run_id"], e["log_sha256"])
                        if is_second:
                            assigned = log.state["assignments"][row["run_id"]]
                            require(bank and assigned["source_writer_bank_sha256"] == digest(canonical(bank))
                                    and log.items == bank["items"], "Reader content differs from sealed author output")
                        rec["native_action_checks"] = native_checks(log, rec["success"])
                        rec.update(decision_evidence(log, steps, histories, source_fields, required))
                        if is_second:
                            require(baseline and rec["opening"] == baseline["opening"] and
                                    e["source_worker_request_id"] == baseline["first_worker_request"],
                                    "Replayed start differs from the graded first pass")
                            rec["divergence"] = compare_episodes(baseline_steps, steps)
                            if result_feedback:
                                old = old_readers[row["arm"]]
                                reference = previous_trials[tid, row["arm"]]
                                require(old["run_id"] == reference["run"] and old["result"]["log_sha256"] ==
                                        reference["log_sha256"] and old["run_id"] != row["run_id"], "Wrong control reader")
                                old_steps, _ = graded_episode(old_folder, old, case, read_sha, first)
                                a, b = [next(s for s in seq if s.actor == "worker") for seq in (old_steps, steps)]
                                require(a.controls == b.controls and a.history == b.history, "Different first public decision")
                                rec["feedback_first_decision"] = compare_steps(a, b)
                        else:
                            baseline_steps = steps
                            rec["first_worker_request"] = e["first_worker_request_id"]
                if is_second:
                    rec["baseline_success"] = baseline["success"]
                    cells.append(rec)
                    observations.append(dict(task=tid, arm=row["arm"], run=rec["run"], log_sha256=result.get("log_sha256"),
                                             success=rec["success"], status=rec["status"], observation=observation))
                else:
                    baseline = rec
                    firsts.append(rec)
        if tid == "task_015" and not result_feedback:
            examples = referral_search_citations(base, data, by_form, cells)
    total = Decimal(frozen["prior_covered_usd"]) + sum(stage["spent"] for stage in costs.values())
    require(len(cells) == frozen["assigned"]["solve"] and len(writers) == frozen["assigned"]["write"]
            and len(firsts) == frozen["assigned"]["reused_first_pass"] and
            total == Decimal(completion["total_covered_usd"]) <= Decimal(approved["authorization"]["total_usd"]),
            "Allocation or total cost differs")
    def coverage(group):
        return allocation_exposure((r["success"], r["observation"]) for r in group)
    coverage_summary = dict(total=coverage(observations), by_delivery={
        mode: coverage(r for r in observations if r["arm"].endswith("__" + mode)) for mode in study.MODES})
    if exposure_report:
        return dict(format="agent-memory-banking-exposure/v1", registration_sha256=rid,
                    **coverage_summary, allocations=observations)
    summary = summarize(firsts, writers, cells) if cells else dict(stored_forms=sum(bool(w["items"]) for w in writers))
    if result_feedback:
        summary["allocation_exposure"] = coverage_summary
        if cells:
            summary["paired_writer_feedback"] = feedback_contrasts(cells, previous["trials"])
    if coverage_summary["total"]["ungraded"]:
        summary["allocation_exposure"] = coverage_summary
    report = dict(implementation=reg["implementation"]["implementation_commit"], registration_sha256=rid,
                costs={phase: {k:str(v) for k,v in values.items()} for phase,values in costs.items()},
                prior_covered_usd=frozen["prior_covered_usd"], total_covered_usd=str(total),
                summary=summary, sources=firsts, writers=writers, trials=cells)
    if result_feedback:
        report.update(writer_feedback_level=cfg["writer_feedback_level"], control_results=protocol["comparison_results"])
    else:
        report["exploratory_referral_search_citations"] = examples
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=pilot.ROOT)
    view = parser.add_mutually_exclusive_group()
    view.add_argument("--optional-storage", action="store_true")
    view.add_argument("--exposure-coverage", action="store_true", help="Use all registered allocations for exposure coverage")
    parser.add_argument("--result-feedback", action="store_true", help="Analyze the result-only writer replication")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if args.result_feedback and args.optional_storage:
        parser.error("Optional storage is a separate historical series")
    result = optional_storage(workspace) if args.optional_storage else analyze(
        workspace, exposure_report=args.exposure_coverage, result_feedback=args.result_feedback)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
