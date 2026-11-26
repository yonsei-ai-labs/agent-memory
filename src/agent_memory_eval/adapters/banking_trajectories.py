"""Project registered banking account-opening allocations and their sealed native states."""

from collections import Counter
import re

from agent_memory_eval.adapters.tau2 import PIN, native_modules, verify_checkout
from agent_memory_eval.adapters.tau_banking_opening import OPENING_OUTCOMES, observed_accounts, opening_receipts, target_accounts
from agent_memory_eval.analysis.decision_attribution import Action
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require, sealed_bytes
from agent_memory_eval.analysis.trajectory import allocation, cue, decision_path, recorded_effort, source_admission, trajectory_report
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.experiments.paired_repetitions import PairedRepetitions, jobs_for
from agent_memory_eval.recording.batch import sealed_rows
from agent_memory_eval.recording.journal import safe_path


METRICS = {
    "target_accounts": "Distinct registered account targets opened at the observed stop, verified against native DB rows.",
    "other_accounts": "Accepted non-target openings at the observed stop, checked against native DB rows.",
    "new_account_rows": "New account IDs at the observed stop, verified by native replay.",
    "all_targets_opened": "All requested accounts were opened, according to public receipts and their native DB rows.",
    "first_search_hit": "At least one task-required document ID in the public return of the first decision's single grep.",
}
CUES = {
    "opening_return": "Linked public opening-tool returns with a recognized outcome. Compare the public source and first input.",
    "target_opening_return": "Recognized results linked to calls for the requested customer, product and account class. Compare the product, outcome and account ID.",
    "required_document": "Task-required document IDs returned by grep in the public source, compared with literal IDs in the initial business input.",
}
RANK = re.compile(r"^(\d+)\. [^\n]*\n   ID: (doc_\S+)\n   Score: ", re.MULTILINE)


def search_documents(content):
    """Only count IDs in native result headers. Unknown renderings have no measured denominator."""
    if not isinstance(content, str):
        return None
    if content.startswith("No matches found for pattern:") or content.startswith("No relevant documents found."):
        return []
    hits = RANK.findall(content)
    if not hits or [int(rank) for rank, _ in hits] != list(range(1, len(hits) + 1)):
        return None
    return [doc for _, doc in hits]


def _searches(messages):
    requests, returns = {}, {}
    for m in messages:
        if m["role"] == "assistant":
            for call in m.get("tool_calls") or []:
                fn = call.get("function", call)
                if fn["name"] == "grep":
                    require(call["id"] not in requests, "Duplicate native search call")
                    requests[call["id"]] = Action.from_function(fn).record()
        elif m["role"] == "tool" and m.get("requestor") in (None, "assistant"):
            call_id = m.get("tool_call_id", m.get("id"))
            if call_id in requests:
                require(call_id not in returns, "Duplicate native search response")
                content = m.get("content")
                returns[call_id] = dict(documents=search_documents(content) if not m.get("error") else None,
                                        sha256=digest(content.encode()) if isinstance(content, str) else None)
    return requests, returns


def _requests_in_error(log, code, stage, repeat):
    faults = []
    first, positions = {}, Counter()
    for req in log.requests:
        role = req["actor_role"]
        positions[role] += 1
        first.setdefault(role, req["request_ref"])
        finish = log.finishes.get(req["request_id"])
        if not finish or finish["status"] != "error":
            continue
        ref = finish["response_ref"]
        error = (log.blobs.get(ref).get("error") or {}) if ref else {}
        size = len(sealed_bytes(safe_path(log.folder, req["request_ref"]["path"]), req["request_ref"]["sha256"]))
        origin = first[role]
        initial_size = len(sealed_bytes(safe_path(log.folder, origin["path"]), origin["sha256"]))
        faults.append(dict(run=log.run, phase=stage, reader_repeat=repeat, actor=role,
                           role_request_number=positions[role], first_role_wire_bytes=initial_size,
                           first_role_request_sha256=origin["sha256"],
                           code=code, provider_code=error.get("code"), delivery=finish["delivery_status"],
                           retry_after_seconds=error.get("retry_after_seconds"), retry_after_utc=error.get("retry_after_utc"),
                           request_sha256=req["request_ref"]["sha256"], response_sha256=ref["sha256"] if ref else None,
                           wire_bytes=size))
    return faults


def _study(folder, protocol):
    close_bytes = (folder / "completion.json").read_bytes()
    close = read_json(close_bytes)
    registration = read_json(sealed_bytes(folder / "registration.json", close["registration_sha256"]))
    plan = registration["preflight"]
    require(plan["protocol_sha256"] == protocol, "Study belongs to another protocol")
    binding = dict(recording=folder.name, registration_sha256=close["registration_sha256"],
                   completion_sha256=digest(close_bytes))
    return plan, close, binding


def _shard(root, case, stage_name, kind, close, plan_sha):
    path = safe_path(root, f"task-{case['task']}/{stage_name}")
    expected = jobs_for(case, kind)
    seal = close["stages"].get(str(path.relative_to(root)))
    if seal is None or seal.get("status") == "not_started":
        require(not path.exists() and (seal is not None or not case["scheduled_stages"]), "Missing scheduled shard")
        reason = (seal or {}).get("reason") or case.get("missing")
        require(reason, "Unstarted shard lacks a reason")
        return path, [dict(job, status="not_started", reason=reason) for job in expected]
    require(isinstance(seal.get("seal"), str), "Missing registered shard seal")
    _, control, rows = sealed_rows(path, seal["seal"])
    require(control["profile"]["registration_sha256"] == plan_sha and control["profile"]["case"] == case["task"]
            and control["design"]["stage"] == kind and len(rows) == len(expected)
            and all(all(row[k] == v for k, v in job.items()) for row, job in zip(rows, expected)), "Changed banking allocation")
    return path, rows


def _opening_keys(receipts, targets=None):
    relevant = [r for r in receipts if targets is None or r["arguments"] in targets]
    if any(r["outcome"] not in OPENING_OUTCOMES for r in relevant):
        return None
    return [canonical([r["arguments"], r["outcome"], r["account_id"]]).decode() for r in relevant]


def _episode(obs, folder, assignment, task, required_docs, bank, faults, collector=None):
    raw = assignment.get("result") or {}
    obs.update(run=assignment.get("run_id"), reason=assignment.get("reason"))
    if not raw:
        require(assignment["status"] == "not_started" and obs["reason"], "Missing recorded episode")
        return
    if not raw.get("log_sha256"):
        require(raw.get("official_success") is None and not raw.get("report_ref"), "Unsealed official result")
        obs.update(status="ungraded", reason=raw.get("error_code") or raw.get("status") or assignment["status"])
        if raw.get("partial_log_sha256"):
            journal = safe_path(folder, assignment["directory"]) / "log.jsonl"
            sealed_bytes(journal, raw["partial_log_sha256"])
            obs["evidence"]["partial_journal_sha256"] = raw["partial_log_sha256"]
        return
    path = safe_path(folder, assignment["directory"])
    log = RecordedDecisions(path, obs["run"], raw["log_sha256"])
    grade, = [e["payload"] for e in log.of_type("outcome.scored")]
    native, = [e["payload"] for e in log.of_type("business.transition") if e["payload"].get("origin") == "forms.native_result"]
    graded = grade["grading_status"] == "graded"
    require(raw["run_id"] == log.run and raw["task_id"] == task.id
            and all(raw[key] == grade[key] for key in ("official_success", "grading_status", "official_score"))
            and raw["report_ref"] == grade["official_artifact_ref"] and
            all(raw[key] == native[key] for key in ("partial_trajectory_ref", "final_db_hash", "error_code")),
            "Allocation differs from its recorded native result")
    if obs["phase"] == "solve":
        origin = log.state["assignments"][log.run]
        require(bank and log.start["source_run_ids"] == [bank["run_id"]] and
                origin["source_writer_run_id"] == bank["run_id"] and bank["source_run_id"] == obs["source_run"] and
                origin["source_writer_snapshot_id"] == bank["snapshot_id"] and
                origin["source_writer_bank_sha256"] == digest(canonical(bank)) and log.items == bank["items"] and
                origin["input_items_sha256"] == digest(canonical(log.items)), "Reader received a different writer's bank")
        obs.update(snapshot=bank["snapshot_id"], writer_run=bank["run_id"])
        obs["evidence"]["local_snapshot"] = log.start["start_snapshot_id"]
    else:
        require(log.start["start_snapshot_id"] is None and not log.start["source_run_ids"], "Source received a past bank")
    obs.update(status="graded" if graded else "ungraded", official_success=grade["official_success"],
               reason=raw.get("error_code") or native["termination_reason"], effort=recorded_effort(log), **decision_path(log))
    if raw.get("error_code"):
        faults.extend(_requests_in_error(log, raw["error_code"], obs["phase"], obs["reader_repeat"]))
    ref = grade["official_artifact_ref"] if graded else native["partial_trajectory_ref"]
    evidence = obs["evidence"]
    evidence.update(journal_sha256=raw["log_sha256"], trajectory_sha256=ref["sha256"] if ref else None)
    if not ref:
        if collector is not None:
            collector.decision(obs, log)
        return
    trajectory = log.blobs.get(ref)
    if graded:
        reward = trajectory["reward_info"]["reward"]
        require(trajectory["task_id"] == task.id and reward == grade["official_score"] and
                (reward == 1) is obs["official_success"], "Grade differs from native report")
    messages = trajectory["messages"] if graded else trajectory
    if collector is not None:
        if obs["phase"] == "source":
            from agent_memory_eval.experiments.forms_write import _project_context
            # Match the projection used by the writer. The native report also contains private simulator activity.
            completed = [r for r in log.requests if r["actor_role"] == "worker" and
                         log.finishes.get(r["request_id"], {}).get("status") == "response"]
            if completed:
                last = completed[-1]
                context = _project_context(log.blobs.get(last["request_ref"]),
                                           log.blobs.get(log.finishes[last["request_id"]]["response_ref"]))
                collector.source(obs["task"], context["messages"])
        collector.decision(obs, log)
    calls, searches = _searches(messages)
    decision = obs["decision"]
    hit = None
    if decision and len(decision["calls"]) == 1 and decision["calls"][0]["name"] == "grep":
        choice, = decision["calls"]
        call_id = choice["id"]
        if call_id in searches:
            require(calls[call_id] == {k: v for k, v in choice.items() if k != "id"}, "Search arguments differ from native call")
            evidence["first_search"] = dict(call_id=call_id, **searches[call_id])
            docs = searches[call_id]["documents"]
            if docs is not None:
                hit = bool(required_docs.intersection(docs))
    obs["progress"] = dict(first_search_hit=hit)
    if native["final_db_hash"]:
        business = observed_accounts(task, messages, native["final_db_hash"])
        effects = business["effects"]
        obs["progress"].update(target_accounts=effects["distinct_targets_opened"], other_accounts=effects["other_accounts_opened"],
                               new_account_rows=business["new_account_rows"], all_targets_opened=effects["all_target_accounts_opened"])
        evidence.update(db_sha256=native["final_db_hash"], opening_calls=[
            {k: r[k] for k in ("call_id", "arguments", "outcome", "account_id", "receipt_sha256")} for r in business["receipts"]])
    if obs["phase"] == "source":
        req = next((r for r in log.requests if r["actor_role"] == "worker"), None)
        wire = log.blobs.get(req["request_ref"]) if req and obs["first_input_sha256"] else None
        input_text = canonical(wire).decode() if wire else None
        current_docs = None if input_text is None else [doc for doc in required_docs
            if re.search(r"(?<![\w-])" + re.escape(doc) + r"(?![\w-])", input_text)]
        all_docs = {doc for result in searches.values() for doc in (result["documents"] or [])}
        complete = calls.keys() == searches.keys() and all(s["documents"] is not None for s in searches.values())
        evidence["source_searches"] = [dict(call_id=cid, reply=searches.get(cid)) for cid in calls]
        past = opening_receipts(messages)
        current = opening_receipts(wire["messages"]) if wire else None
        targets = target_accounts(task)
        obs["source_cues"] = dict(opening_return=cue(_opening_keys(past), _opening_keys(current) if current is not None else None),
            target_opening_return=cue(_opening_keys(past, targets), _opening_keys(current, targets) if current is not None else None),
            required_document=cue(required_docs.intersection(all_docs) if complete else None, current_docs))


def banking_report(profile, workspace, checkout, sources_only, *, collector=None):
    protocol = digest(profile.read_bytes())
    spec = read_json(profile.read_bytes())
    require(spec.get("adapter") == "account_opening", "Expected a registered account-opening profile")
    native_modules(verify_checkout(checkout))
    from tau2.domains.banking_knowledge.environment import get_tasks
    native = {task.id: task for task in get_tasks()}
    root = safe_path(workspace / "runs", spec["source_study"])
    plan, close, source_binding = _study(root, protocol)
    require(plan["phase"] == "sources" and len(plan["cases"]) == plan["assigned"] == spec["source"]
            and {c["task_id"] for c in plan["cases"]} == set(spec["tasks"]), "Changed banking source cohort")
    bindings = dict(protocol_sha256=protocol, benchmark_revision=PIN, source=source_binding)
    if collector is not None:
        from agent_memory_eval.analysis.policy_evidence import banking_cost
        source_close = close
        collector.cost = banking_cost(source_close)
        collector.cluster_unit = "customer"
    rows, sources, documents, faults = [], {}, {}, []
    plan_sha = digest(canonical(plan))
    for case in plan["cases"]:
        tid = case["task_id"]
        task = native[tid]
        require(digest(canonical(task.model_dump(mode="json"))) == case["task_sha256"], "Native task changed")
        owners = {digest(t["user_id"].encode()) for t in target_accounts(task)}
        require(owners == {case["customer"]}, "Registered cluster differs from the native customer")
        domain = checkout / "data/tau2/domains/banking_knowledge/tasks"
        documents[tid] = set(read_json(safe_path(domain, tid + ".json").read_bytes())["required_documents"])
        folder, (raw,) = _shard(root, case, "source", "source", close, plan_sha)
        obs = allocation(tid, case["customer"], "source", "no_memory")
        _episode(obs, folder, raw, task, documents[tid], None, faults, collector)
        rows.append(obs)
        sources[tid] = obs
    rule = spec.get("source_opportunity_requirement")
    admission = None
    if sources_only or rule is not None:
        report = trajectory_report("tau3.banking.account_opening", bindings, rows, metrics=METRICS, cues=CUES,
                                   sources_only=True, request_errors=faults, requirement=rule)
        if sources_only:
            return report
        admission = source_admission(report)
    root = safe_path(workspace / "runs", spec["validation_study"])
    if admission is not None:
        raw_admission = (root / "source-opportunity.json").read_bytes()
        require(read_json(raw_admission) == admission, "Recorded admission differs from its source evidence")
        bindings["source_admission_sha256"] = digest(raw_admission)
        if admission["decision"] == "stop":
            require({p.name for p in root.iterdir()} == {"source-opportunity.json"}, "Stopped study contains validation work")
            missing = _stopped_readers(spec, sources, admission["reason"])
            rows.extend(missing)
            if collector is not None:
                writer_slots = sorted({(r["task"], r["form"], r["condition"], r["writer_draw"]) for r in missing})
                for task, form, condition, draw in writer_slots:
                    collector.writer(task, form, condition, "not_started", draw=draw, reason=admission["reason"])
            return trajectory_report("tau3.banking.account_opening", bindings, rows, metrics=METRICS, cues=CUES,
                                     sources_only=False, request_errors=faults, requirement=rule)
    plan, close, bindings["validation"] = _study(root, protocol)
    if collector is not None:
        collector.cost = banking_cost(source_close, close)
    require(plan.get("source_opportunity_check") == admission, "Validation registration and source admission differ")
    require(plan["phase"] == "validation" and plan["source_registration_sha256"] == source_binding["registration_sha256"],
            "Mismatched validation and source registration")
    plan_sha = digest(canonical(plan))
    repeats = PairedRepetitions.from_spec(spec)
    expected = Counter((tid, policy, draw) for tid in sources for policy in spec["validation"]["writer_policies"]
                       for draw in range(1, repeats.writer_draws + 1))
    require(Counter((c["task_id"], c["writer_policy"], c.get("writer_draw", 1)) for c in plan["cases"]) == expected,
            "Changed task, policy or writer repetition")
    for case in plan["cases"]:
        tid, policy, draw = case["task_id"], case["writer_policy"], case.get("writer_draw", 1)
        source = sources[tid]
        require(case["customer"] == source["cluster"], "Customer cluster changed")
        banks, writer_logs = {}, {}
        for name, kind, rep in repeats.stages():
            folder, assignments = _shard(root, case, name, kind, close, plan_sha)
            for raw in assignments:
                if kind == "write":
                    data = raw.get("result") or {}
                    if collector is not None:
                        checked = (RecordedDecisions(safe_path(folder, raw["directory"]), raw["run_id"], data["log_sha256"])
                                   if data.get("log_sha256") else None)
                        collector.writer(tid, raw["arm"], policy, raw["status"], checked, checksum=data.get("log_sha256"),
                                         draw=draw, reason=raw.get("reason") or data.get("error_code"))
                    if raw["status"] == "completed":
                        bank = read_form_bank(safe_path(folder, raw["directory"]), expected_log_sha256=data["log_sha256"])
                        imported = case["imported_first_pass"]
                        require(bank["run_id"] == raw["run_id"] and bank["source_run_id"] == source["run"]
                                and bank["source_log_sha256"] == source["evidence"]["journal_sha256"] == imported["log_sha256"]
                                and imported["run_id"] == source["run"] and bank["public_context_sha256"] == imported["public_context_sha256"]
                                and source["status"] == "graded" and bank["items"], "Unbound writer")
                        banks[raw["arm"]] = bank
                        writer_logs[raw["arm"]] = data["log_sha256"]
                    elif data.get("log_sha256") and data.get("error_code"):
                        log = RecordedDecisions(safe_path(folder, raw["directory"]), raw["run_id"], data["log_sha256"])
                        faults.extend(_requests_in_error(log, data["error_code"], "write", None))
                else:
                    form, delivery = raw["arm"].split("__")
                    obs = allocation(tid, case["customer"], "solve", policy, form=form, delivery=delivery, draw=draw, repeat=rep)
                    obs["source_run"] = source["run"]
                    _episode(obs, folder, raw, native[tid], documents[tid], banks.get(form), faults, collector)
                    if obs["writer_run"]:
                        obs["evidence"].update(writer_journal_sha256=writer_logs[form],
                                               public_experience_sha256=banks[form]["public_context_sha256"],
                                               writer_instruction_sha256=banks[form]["instruction_sha256"])
                    rows.append(obs)
    require(len(rows) - len(sources) == plan["assigned"]["solve"] == spec["validation"]["solve"],
            "Missing registered reader allocations")
    return trajectory_report("tau3.banking.account_opening", bindings, rows, metrics=METRICS, cues=CUES,
                             sources_only=sources_only, request_errors=faults, requirement=spec.get("source_opportunity_requirement"))


def _stopped_readers(spec, sources, reason):
    repeats = PairedRepetitions.from_spec(spec)
    rows = []
    for tid, source in sources.items():
        case = dict(task_id=tid, reader_modes=[spec["reader"]])
        for _, cell in repeats.writer_cells(case, spec["validation"]["writer_policies"]):
            for _, kind, rep in repeats.stages():
                if kind != "solve":
                    continue
                for job in jobs_for(cell, kind):
                    form, delivery = job["arm"].split("__")
                    obs = allocation(tid, source["cluster"], "solve", cell["writer_policy"], form=form, delivery=delivery,
                                     draw=cell.get("writer_draw", 1), repeat=rep)
                    obs.update(reason=reason, source_run=source["run"])
                    rows.append(obs)
    require(len(rows) == spec["validation"]["solve"], "Stopped study does not match the registered reader count")
    return rows
