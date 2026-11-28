"""Register all native bank tasks in the cohort and bind reusable sources to sealed recordings."""

from collections import Counter, defaultdict
from decimal import Decimal

from agent_memory_eval.adapters.banking_business import BankingBusiness, bind_call_requests
from agent_memory_eval.adapters.banking_source_screen import contains, document_opportunity
from agent_memory_eval.adapters.banking_trajectories import _shard, _study
from agent_memory_eval.adapters.tau2 import native_modules, verify_checkout
from agent_memory_eval.analysis.claim_evidence import paired_counts
from agent_memory_eval.analysis.clustered_outcomes import clustered_contrast
from agent_memory_eval.analysis.decision_attribution import compare_episodes, compare_steps
from agent_memory_eval.analysis.efficiency import METRICS, customer_questions, episode_effort, paired_effort
from agent_memory_eval.analysis.policy_evidence import PolicyEvidence, counted
from agent_memory_eval.analysis.recorded_decisions import READS, RecordedDecisions, require
from agent_memory_eval.analysis.tau2_attribution import recorded_episode
from agent_memory_eval.analysis.trajectory import decision_path
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.experiments.forms_write import _project_context, read_form_bank
from agent_memory_eval.experiments.paired_repetitions import FORMS, MODES
from agent_memory_eval.recording.batch import sealed_rows
from agent_memory_eval.recording.journal import safe_path


def cohort_slots(spec, split, history, parent_plan):
    groups = spec["task_groups"]
    eligible = {task: meta for task, meta in split.items() if meta["group"] in groups}
    require(dict(Counter(meta["group"] for meta in eligible.values())) == groups
            and tuple(spec["forms"]) == FORMS and tuple(spec["deliveries"]) == MODES, "Cohort or assignments changed")
    confirmed = {r["customer"] for r in eligible.values() if r["group"] == "confirmation"}
    require(None not in confirmed and not confirmed.intersection(
        r["customer"] for r in eligible.values() if r["group"] != "confirmation"), "Confirmation customers overlap")
    draws = range(1, spec["source_draws_per_task"] + 1)
    expected = {(task, draw) for task, r in eligible.items() if r["group"] == "development" for draw in draws}
    past = {(r["task"], r["draw"]): r for r in history["observations"]}
    archived = {(c["task_id"], c["source_draw"]): c for c in parent_plan["cases"]}
    require(expected == past.keys() == archived.keys() and len(past) == len(history["observations"])
            and len(archived) == len(parent_plan["cases"]), "Duplicate, missing or extra archived draw")
    registration = digest(canonical(parent_plan))
    slots, missing = [], []
    for task, meta in sorted(eligible.items()):
        for draw in draws:
            name = f"{task}__source-{draw}"
            cell = dict(task=name, task_id=task, source_draw=draw, cohort=meta["group"],
                        customer=meta["customer"] or "unresolved-" + task, identity_basis=meta["identity_basis"])
            if (task, draw) in past:
                old = past[task, draw]
                require(archived[task, draw]["task"] == name and archived[task, draw]["customer"] == cell["customer"],
                        "Archived allocation identity changed")
                if old["status"] != "graded":
                    require(old["success"] is None, "An ungraded historical draw has a score")
                    missing.append(dict(cell, reason=old["reason"] or old["status"], run=old["run"]))
                    continue
                require(type(old["success"]) is bool, "Historical grade missing")
                cell["imported_first_pass"] = dict(directory=f"{history['binding']['recording']}/task-{name}/source",
                    registration_sha256=registration, run_id=old["run"], log_sha256=old["journal_sha256"],
                    public_context_sha256=old["public_experience_sha256"])
            slots.append(cell)
    imports = [c["imported_first_pass"] for c in slots if "imported_first_pass" in c]
    require(len(imports) == history["graded"] == len({r["run_id"] for r in imports})
            and len(slots) + len(missing) == len(eligible) * len(draws), "Repeated source or missing slot")
    return slots, missing


def historical_registration(workspace, history):
    binding = history["binding"]
    folder = safe_path(workspace / "runs", binding["recording"])
    registration = (folder / "registration.json").read_bytes()
    completion = (folder / "completion.json").read_bytes()
    require(digest(registration) == binding["registration_sha256"] == read_json(completion)["registration_sha256"]
            and digest(completion) == binding["completion_sha256"], "Historical source study changed")
    return read_json(registration)["preflight"], read_json(completion)


def first_release(cases):
    first = {}
    for case in sorted(cases, key=lambda c: (c["task_id"], c["source_draw"])):
        if "imported_first_pass" in case:
            first.setdefault(case["task_id"], case["task"])
    return set(first.values())


def allocation_counts(cases, repeat):
    imported = sum("imported_first_pass" in c for c in cases)
    banks = len(cases) * len(FORMS)
    return dict(reused_first_pass=imported, source=len(cases) - imported, write=banks,
                solve=banks * len(MODES) * repeat, second_pass_no_memory=0)


def observation(folder, row, *, writer=None, business=None, policy=None, transfer=None):
    """Audit one native episode, including delivered input and official outcome, using its sealed journal."""
    result = row.get("result") or {}
    obs = dict(run=row.get("run_id"), status=row["status"], reason=row.get("reason") or result.get("error_code"),
               journal=result.get("log_sha256"), success=None, effort={}, dispatched_worker=0,
               body_requests=0, body_requests_with_response=0, title_requests=0, reads={}, returns={})
    if not obs["journal"]:
        require(result.get("official_success") is None, "Unsealed grade")
        return obs
    log = RecordedDecisions(safe_path(folder, row["directory"]), row["run_id"], obs["journal"])
    require(log.start["fresh_session"], "Shared solver session")
    if row["arm"] == "no_memory":
        require(not log.items and not log.start["source_run_ids"], "Baseline received prior memory")
    else:
        items = [] if transfer and transfer["bank"] == "empty" else (writer or {}).get("items")
        assignment = log.state["assignments"][log.run]
        require(writer and log.start["source_run_ids"] == [writer["run_id"]] and log.items == items
                and assignment["input_items_sha256"] == digest(canonical(items)),
                "Reader used another bank")
        if transfer:
            require(assignment["source_task_id"] == writer["task_id"] == row["source_task_id"]
                    and assignment["task_id"] == row["task_id"] != row["source_task_id"]
                    and assignment["fixed_bank_delivery"]["bank_contents"] == transfer["bank"]
                    and assignment["source_writer_bank_sha256"] == digest(canonical(writer)),
                    "Transfer outcome does not match its writer, target or bank")
        obs.update(writer=writer["run_id"], writer_snapshot=writer["snapshot_id"])
    reads, choices, steps = Counter(), [], []
    for req in log.requests:
        if req["actor_role"] != "worker":
            continue
        end = log.finishes[req["request_id"]]
        sent = end["delivery_status"] != "not_submitted"
        if sent:
            fields = {s["field"] for s in log.visible(req, log.blobs.get(req["request_ref"]))}
            if not obs["dispatched_worker"]:
                obs.update(first_worker=req["request_ref"]["sha256"], first_fields=sorted(fields))
            obs["dispatched_worker"] += 1
            obs["title_requests"] += "title" in fields
            obs["body_requests"] += "body" in fields
            obs["body_requests_with_response"] += "body" in fields and end["status"] == "response"
        calls = []
        if end["status"] == "response":
            calls = log.reply(req)["message"].get("tool_calls") or []
            if transfer:
                actions = []
                for call in calls:
                    fn = call["function"]
                    if fn["name"] not in transfer["rule"]["tools"]:
                        continue
                    try:
                        args = read_json(fn["arguments"].encode())
                    except (ValueError, UnicodeError):
                        args = None
                    actions.append(dict(name=fn["name"], arguments=args if isinstance(args, dict) else {}))
                steps.append(dict(request=req["request_id"], body="body" in fields, calls=actions))
        private = [c["function"]["name"] for c in calls if c["function"]["name"] in READS]
        reads.update(private)
        choices.append(dict(status=end["status"], delivery=end["delivery_status"], reads=private))
    obs.update(reads=dict(reads), returns=dict(Counter(e["payload"]["operation"] for e in log.of_type("memory.returned"))))
    grade, = [e["payload"] for e in log.of_type("outcome.scored")]
    require(grade["official_success"] is result["official_success"] and grade["grading_status"] == result["grading_status"]
            and grade["official_artifact_ref"] == result["report_ref"], "Native grade differs from the journal")
    messages = None
    if grade["grading_status"] == "graded":
        official = log.blobs.get(grade["official_artifact_ref"])
        require(official["task_id"] == row["task_id"] and official["reward_info"]["reward"] == grade["official_score"]
                and (grade["official_score"] == 1) is grade["official_success"], "Official artifact disagrees")
        messages = official["messages"]
        obs.update(success=grade["official_success"], report=grade["official_artifact_ref"]["sha256"])
    obs["effort"] = episode_effort(row, messages, choices)
    if transfer:
        from agent_memory_eval.analysis.efficiency import first_decision_effort
        from agent_memory_eval.study.transfer import decision_evidence
        obs["decision"] = decision_evidence(transfer["rule"], steps, completed=messages is not None)
        obs["effort_from_first_worker"] = first_decision_effort(log.events.values())
        obs["termination_reason"] = result.get("termination_reason") or result.get("error_code")
        obs["official_components"] = None
        if messages is not None:
            reward = official["reward_info"]
            obs["official_components"] = {k: reward.get(k) for k in (
                "reward_basis", "reward_breakdown", "db_check", "communicate_checks", "nl_assertions", "action_checks"
            )}
    if messages is not None:
        questions = customer_questions(messages)
        obs["customer_questions"] = questions
        obs["effort"].update(question_turns=len(questions),
                             repeated_question_turns=sum("repeated_from_index" in q for q in questions))
    if business is not None:
        native, = [e["payload"] for e in log.of_type("business.transition") if e["payload"].get("origin") == "forms.native_result"]
        require(all(native[k] == result[k] for k in ("partial_trajectory_ref", "final_db_hash", "error_code")),
                "Business evidence differs from the original trajectory receipt")
        ref = grade["official_artifact_ref"] if messages is not None else native["partial_trajectory_ref"]
        if ref and native["final_db_hash"]:
            observed = messages if messages is not None else log.blobs.get(ref)
            obs["business"] = business.observe(observed, native["final_db_hash"],
                                               official["reward_info"] if messages is not None else None)
            obs["business"]["trajectory_sha256"] = ref["sha256"]
            bind_call_requests(obs["business"]["calls"], log)
    if policy is not None:
        task = row["task_id"]
        if writer is None:
            completed = [r for r in log.requests if r["actor_role"] == "worker"
                         and log.finishes[r["request_id"]]["status"] == "response"]
            if completed:
                req = completed[-1]
                source = _project_context(log.blobs.get(req["request_ref"]), dict(choices=[log.reply(req)]))
                policy.source(task, [source["task_context"], *source["messages"]])
                obs["public_context_sha256"] = digest(canonical(source))
                if business is not None:
                    first_wire = log.blobs.get(completed[0]["request_ref"])
                    obs["public_document_opportunity"] = document_opportunity(
                        set(business.task.required_documents or []), first_wire, source["messages"])
        path = decision_path(log)
        obs["first_business"] = path["decision"]
        policy.decision(dict(task=task, writer_run=writer["run_id"] if writer else None, decision=path["decision"]), log)
        cues = policy.readers[log.run]["cues"]
        if cues is not None:
            obs["first_action_literals"] = [c for c in cues if counted(c["action"])]
    for entry in log.of_type("business.transition"):
        p = entry["payload"]
        if p.get("origin") == "task_start.first_decision":
            obs.update(start=p["task_start_ref"]["sha256"], start_source=p["source_run_id"])
    return obs


def arm_summary(readers):
    seen = [r for r in readers if r["dispatched_worker"]]
    graded = [r for r in readers if r["success"] is not None]
    return dict(assigned=len(readers), dispatched=len(seen), graded=len(graded), successes=sum(r["success"] for r in graded),
                with_body=sum(bool(r["body_requests"]) for r in seen), with_title=sum(bool(r["title_requests"]) for r in seen),
                with_body_and_response=sum(bool(r["body_requests_with_response"]) for r in seen),
                body_at_open=sum("body" in r["first_fields"] for r in seen),
                list_attempts=sum(r["reads"].get("memory_list", 0) for r in seen),
                get_attempts=sum(r["reads"].get("memory_get", 0) for r in seen),
                body_returns=sum(r["returns"].get("get", 0) for r in seen),
                tasks_exposed=len({r["task"] for r in seen if r["body_requests"]}))


def release_check(arms, rules):
    auto, optional = (arms[mode] for mode in MODES)
    failures = []
    if any(not a["assigned"] or a["graded"] < a["assigned"] * rules["minimum_graded_fraction_per_delivery"] for a in arms.values()):
        failures.append("grade_coverage")
    if rules["require_every_dispatched_preload_has_body"] and auto["dispatched"] != auto["body_at_open"]:
        failures.append("preload_body_coverage")
    if optional["tasks_exposed"] < rules["minimum_distinct_tasks_with_on_demand_body"]:
        failures.append("optional_body_task_coverage")
    return dict(meets_readiness_targets=not failures, reasons=failures)


BUSINESS_METRICS = ("normalized_actor_matches", "reference_business_rows_matched", "additional_business_rows",
                    "customer_extra_final_rows", "customer_calls", "worker_reported_errors", "debit_targets_opened")


def business_metrics(observation):
    business = observation.get("business")
    if business is None:
        return {}
    tables = [v for v in business["tables"].values() if v["kind"] == "business"]
    reference_rows = sum(v["reference_changed"] for v in tables)
    reference_ok = not business["reference_errors"]
    return dict(normalized_actor_matches=business["normalized_actor_matches"],
                reference_business_rows_matched=sum(v["target_values_matched"] for v in tables)
                if reference_rows and reference_ok else None,
                additional_business_rows=sum(v["additional_changed"] for v in tables) if reference_ok else None,
                customer_extra_final_rows=business["customer_extra_final_rows"],
                customer_calls=sum(c["actor"] == "user" for c in business["calls"]),
                worker_reported_errors=sum(c["actor"] == "assistant" and c["receipt"] in
                                           {"reported_error", "rejected_before_execution"} for c in business["calls"]),
                debit_targets_opened=business.get("debit_disputes", {}).get("target_opened"))


def first_source_evidence(workspace, frozen, history):
    plan, close = historical_registration(workspace, history)
    expected = set(frozen["first_release"])
    require(frozen["historical_binding"] == history["binding"], "Different historical source study")
    native_modules(verify_checkout(workspace / "data/tau2-bench"))
    from tau2.domains.banking_knowledge.environment import get_tasks
    tasks = {t.id: t for t in get_tasks()}
    source_root = safe_path(workspace / "runs", history["binding"]["recording"])
    archived = defaultdict(dict)
    for row in history["observations"]:
        archived[row["task"]][row["draw"]] = row
    sources = {}
    repeat_pairs = defaultdict(list)
    for case in plan["cases"]:
        if case["task"] not in expected:
            continue
        name = "task-" + case["task"] + "/source"
        folder = safe_path(source_root, name)
        _, _, (row,) = sealed_rows(folder, close["stages"][name]["seal"])
        tid = case["task_id"]
        original = archived[tid][case["source_draw"]]
        obs = observation(folder, row, business=BankingBusiness(tasks[tid], case), policy=PolicyEvidence())
        require((obs["run"], obs["journal"], obs["public_context_sha256"], obs["first_worker"], obs["success"]) ==
                (original["run"], original["journal_sha256"], original["public_experience_sha256"],
                 original["first_worker_sha256"], original["success"]),
                "Archived evidence does not match the registered source")
        repeats = []
        for draw, other in sorted(archived[tid].items()):
            if draw == case["source_draw"]:
                continue
            first_worker = other.get("first_worker_sha256")
            match = "unknown"
            if first_worker is not None:
                match = "same" if first_worker == obs["first_worker"] else "different"
            repeat_pairs[match].append((other["success"], obs["success"]))
            repeats.append(dict(draw=draw, run=other["run"], journal=other.get("journal_sha256"), success=other["success"],
                                first_worker_sha256=first_worker, first_worker_input=match))
        b = obs["business"]
        calls = b["calls"]
        sources[case["task"]] = dict(task=tid, success=obs["success"], run=obs["run"], journal=obs["journal"],
            public_context_sha256=obs["public_context_sha256"], native_report=obs["report"], db=b["verified_db_sha256"],
            reference_actions=b["reference_actions"], reference_errors=len(b["reference_errors"]),
            official_action_matches=b["official_action_matches"], process=business_metrics(obs),
            business_calls=len(calls), request_bound_calls=sum(c["request"] is not None for c in calls),
            first_worker_sha256=obs["first_worker"], other_no_memory_draws=repeats)
    require(sources.keys() == expected, "Missing registered source")
    return dict(format="banking-cohort-first-source-evidence/v1", binding=history["binding"],
                graded=len(sources), successes=sum(v["success"] for v in sources.values()), sources=sources,
                other_no_memory_draw_minus_selected=dict(
                    all=paired_counts(p for pairs in repeat_pairs.values() for p in pairs),
                    by_first_worker_input={k: paired_counts(v) for k, v in sorted(repeat_pairs.items())}))


def outcome_tables(cells):
    index = {(r["task"], r["draw"], r["form"], r["repeat"], r["delivery"]): r for r in cells}
    require(len(index) == len(cells), "Duplicate reader")
    arms, clusters, deliveries, progress = (defaultdict(list) for _ in range(4))
    for r in cells:
        key = f"{r['form']}__{r['delivery']}"
        source = r["source"]
        arms[key].append((r["success"], source["success"], r["effort"], source["effort"]))
        if r["delivery"] == "preload":
            other = index[r["task"], r["draw"], r["form"], r["repeat"], "on_demand"]
            require(r.get("expected_writer") == other.get("expected_writer"), "Delivery contrast used different memories")
            subgroup = "confirmation" if r["cohort"] == "confirmation" else "development"
            slot = (r["draw"], r["repeat"])
            clusters[subgroup].append((r["customer"], r["task"], r["form"], slot, other["success"], r["success"]))
            deliveries[subgroup].append((r["success"], other["success"], r["effort"], other["effort"]))
            progress[subgroup].append((r["success"], other["success"], business_metrics(r), business_metrics(other)))
    effort_metrics = (*METRICS, "question_turns", "repeated_question_turns")
    return dict(versus_first_pass={k: paired_effort(v, effort_metrics) for k, v in sorted(arms.items())},
                delivery_pairs={k: paired_effort(v, effort_metrics) for k, v in sorted(deliveries.items())},
                business_delivery_pairs={k: paired_effort(v, BUSINESS_METRICS) for k, v in sorted(progress.items())},
                preload_minus_on_demand={k: clustered_contrast(v, cluster_unit="customer" if k == "confirmation"
                    else "identified customer or unresolved task") for k, v in sorted(clusters.items())})


def interrupted_reader_cases(root, plan, close, manifest, spec):
    """Bind each technical loss to its sealed original request, source and stored memory."""
    require(close["registration_sha256"] == manifest["original_registration_sha256"],
            "Outage manifest belongs to a different original release")
    by_task = defaultdict(dict)
    for receipt in manifest["records"]:
        task, arm = receipt["task"], receipt["arm"]
        require(arm not in by_task[task], "Duplicate recovery allocation")
        by_task[task][arm] = receipt
    require(sum(map(len, by_task.values())) == spec["assigned"]["solve"], "Wrong number of interrupted consultations")
    registration = digest(canonical(plan))
    cases, counts = [], Counter()
    pair_sizes = Counter()
    for case in plan["cases"]:
        receipts = by_task.pop(case["task_id"], None)
        if receipts is None:
            continue
        sdir, solved = _shard(root, case, "solve", "solve", close, registration)
        wdir, written = _shard(root, case, "write", "write", close, registration)
        writers = {r["arm"]: r for r in written}
        allocations = {}
        for row in solved:
            arm = row["arm"]
            if arm not in receipts:
                continue
            r = receipts[arm]
            result = row["result"]
            require(row["run_id"] == r["run"] and row["index"] == r["row_index"] and row["status"] == "error"
                    and result["log_sha256"] == r["journal_sha256"] and result["official_success"] is None
                    and result["report_ref"] is None and result["error_code"] == r["code"] in {"HTTP_502", "HTTP_503"}
                    and close["stages"][str(sdir.relative_to(root))]["seal"] == r["stage_seal"], "Changed terminal loss")
            log = RecordedDecisions(safe_path(sdir, row["directory"]), row["run_id"], result["log_sha256"])
            req = log.requests[-1]
            finish = log.finishes[req["request_id"]]
            require(req["request_id"] == r["failed_request"] and req["actor_role"] == r["actor"] == "user_simulator"
                    and req["request_ref"]["sha256"] == r["request_sha256"] and finish["status"] == "error"
                    and finish["delivery_status"] == r["delivery"] == "response_received"
                    and finish["response_ref"]["sha256"] == r["response_sha256"]
                    and log.blobs.get(finish["response_ref"])["error"]["code"] == r["provider_code"]
                    and r["code"] == "HTTP_" + str(r["provider_code"]), "Wrong provider or terminal request")
            form, mode = arm.split("__")
            writer = writers[form]
            bank = read_form_bank(safe_path(wdir, writer["directory"]), expected_log_sha256=writer["result"]["log_sha256"])
            source = case["imported_first_pass"]
            require(log.start["fresh_session"] and log.start["source_run_ids"] == [writer["run_id"]]
                    and log.items == bank["items"] and (bank["source_run_id"], bank["source_log_sha256"]) ==
                    (source["run_id"], source["log_sha256"]), "Interrupted assignment had another source or bank")
            allocations[arm] = dict(run=row["run_id"], index=row["index"], journal=result["log_sha256"],
                                    request=r["failed_request"], writer=bank["run_id"], snapshot=bank["snapshot_id"])
            counts[mode] += 1
        require(allocations.keys() == receipts.keys(), "Recovery contains an unassigned delivery")
        pair_sizes.update(Counter(a.split("__")[0] for a in allocations).values())
        key = str(wdir.relative_to(root))
        stage = dict(directory=root.name + "/" + key, seal=close["stages"][key]["seal"], registration_sha256=registration)
        cases.append(dict(case, scheduled_stages=["solve"], imported_writer_stage=stage, interrupted_readers=allocations))
    expected = spec["original_pairs"]
    require(not by_task and counts == spec["by_delivery"] and pair_sizes[2] == expected["both_interrupted"]
            and pair_sizes[1] == expected["one_interrupted"]
            and plan["assigned"]["solve"] // 2 - sum(pair_sizes.values()) == expected["neither_interrupted"],
            "Original customer and memory pairs are not covered by the recovery")
    return cases


def read_release(workspace, frozen, release, *, decision_interface=None):
    root = workspace / "runs" / (frozen["series"] + "-" + release)
    plan, close, binding = _study(root, frozen["protocol_sha256"])
    require(all(plan[k] == v for k, v in frozen.items()) and plan["release"] == release, "Different cohort release")
    registration = digest(canonical(plan))
    sources, writers, readers, decision_pairs = [], [], [], []
    native_modules(verify_checkout(workspace / "data/tau2-bench"))
    from tau2.domains.banking_knowledge.environment import get_tasks
    tasks = {t.id: t for t in get_tasks()}
    for case in plan["cases"]:
        ref = case.get("imported_first_pass")
        if ref:
            folder = safe_path(workspace / "runs", ref["directory"])
            _, _, (row,) = sealed_rows(folder)
            require(row["run_id"] == ref["run_id"] and row["result"]["log_sha256"] == ref["log_sha256"], "Changed source receipt")
        else:
            folder, (row,) = _shard(root, case, "source", "source", close, registration)
        source_row = row
        policy = PolicyEvidence()
        business = BankingBusiness(tasks[case["task_id"]], case)
        src = observation(folder, row, business=business, policy=policy)
        if ref:
            require(src["public_context_sha256"] == ref["public_context_sha256"], "Different public source for evidence")
        sources.append(dict(src, task=case["task_id"], draw=case["source_draw"], reused=bool(ref)))
        wdir, written = _shard(root, case, "write", "write", close, registration)
        banks = {}
        for w in written:
            res = w.get("result") or {}
            writer_evidence = {}
            if w["status"] == "completed":
                bank = read_form_bank(safe_path(wdir, w["directory"]), expected_log_sha256=res["log_sha256"])
                require(len(bank["items"]) == 1 and (bank["source_run_id"], bank["source_log_sha256"], bank["result_feedback"]["success"])
                        == (src["run"], src["journal"], src["success"]) and bank["arm"] == w["arm"], "Wrong writer source or feedback")
                banks[w["arm"]] = bank
                writer_log = RecordedDecisions(safe_path(wdir, w["directory"]), w["run_id"], res["log_sha256"])
                policy.writer(case["task_id"], w["arm"], "registered", w["status"], writer_log, checksum=res["log_sha256"])
                proof = policy.writers[w["run_id"]][0]
                require([(c["run"], c["public_context_sha256"]) for c in proof["public_contexts"]] ==
                        [(src["run"], src["public_context_sha256"])] and bank["public_context_sha256"] == src["public_context_sha256"],
                        "Decision evidence was harvested from a different public experience")
                writer_evidence = {k: proof[k] for k in ("public_contexts", "request_sha256", "instruction_sha256", "versions")}
                item, = bank["items"]
                source_cues = proof["source_cues"]
                writer_evidence.update(version_sha256=writer_log.state["versions"][item["version"]]["sha256"],
                                       title_chars=len(item["title"]), body_chars=len(item["body"]),
                                       source_literal_candidates=len(source_cues),
                                       stored_source_literals=[c for c in source_cues if counted(c["stored"])])
                writer_evidence["source_document_fields"] = {
                    doc: [field for field in ("title", "body") if contains(item[field], doc)]
                    for doc in src["public_document_opportunity"]["source_only"]}
            writers.append(dict(task=case["task_id"], draw=case["source_draw"], form=w["arm"], status=w["status"],
                                reason=w.get("reason") or res.get("error_code"), run=w.get("run_id"),
                                journal=res.get("log_sha256"), snapshot=res.get("snapshot_id"), source=src["run"],
                                evidence=writer_evidence))
        for repeat, stage in enumerate([s for s in case["scheduled_stages"] if s.startswith("solve")], 1):
            folder, rows = _shard(root, case, stage, "solve", close, registration)
            steps = {}
            for row in rows:
                form, mode = row["arm"].split("__")
                bank = banks.get(form)
                obs = observation(folder, row, writer=bank, business=business, policy=policy)
                if obs["dispatched_worker"]:
                    require(obs.get("start_source") == src["run"] and obs.get("start") == src.get("start"),
                            "Reader first public decision differs from its source")
                readers.append(dict(obs, task=case["task_id"], draw=case["source_draw"], repeat=repeat, form=form,
                                    delivery=mode, cohort=case["cohort"], customer=case["customer"],
                                    expected_writer=bank["run_id"] if bank else None,
                                    source={k: src[k] for k in ("run", "success", "effort")}))
                if decision_interface is not None and obs["dispatched_worker"]:
                    path, _ = recorded_episode(folder, row, case, source=source_row, **decision_interface,
                                               include_partial=obs["success"] is None)
                    steps[form, mode] = dict(run=obs["run"], path=path, graded=obs["success"] is not None,
                                             first_worker=next((s for s in path if s.actor == "worker"), None))
            if decision_interface is not None:
                for form in FORMS:
                    reference, candidate = (steps.get((form, mode), {}) for mode in ("on_demand", "preload"))
                    whole_pair = reference.get("graded") and candidate.get("graded")
                    decision_pairs.append(dict(task=case["task_id"], draw=case["source_draw"], form=form,
                        repeat=repeat, reference=reference.get("run"), candidate=candidate.get("run"),
                        first_worker_choice=compare_steps(reference.get("first_worker"), candidate.get("first_worker")),
                        comparison=compare_episodes(reference["path"], candidate["path"]) if whole_pair else None))
    expected = plan["assigned"]
    require(len(sources) == expected["source"] + expected["reused_first_pass"] and len(writers) == expected["write"]
            and len(readers) == expected["solve"], "Unaccounted cohort assignment")
    costs = defaultdict(lambda: defaultdict(Decimal))
    for path, v in close["stages"].items():
        stage = path.split("/")[-1].split("-")[0]
        for field in ("reported", "held", "spent"):
            costs[stage][field] += Decimal(v[field])
    spend = sum((v["spent"] for v in costs.values()), Decimal(0))
    require(spend + Decimal(plan["prior_covered_usd"]) == Decimal(close["total_covered_usd"])
            <= Decimal(plan["authorization"]["total_usd"]), "Cohort budget does not reconcile")
    result = dict(binding=binding, prior_binding=plan["prior_release"], prior_covered_usd=plan["prior_covered_usd"],
                by_stage_usd={k: {f: str(s) for f, s in v.items()} for k, v in costs.items()},
                spent=str(spend), sources=sources, writers=writers, readers=readers)
    if decision_interface is not None:
        result["decision_pairs"] = decision_pairs
    return result


def cohort_results(workspace, frozen, rules, *, full, decision_interface=None):
    releases = ["first", "remainder"] if full else ["first"]
    raw = {name: read_release(workspace, frozen, name, decision_interface=decision_interface) for name in releases}
    if full:
        require(raw["remainder"]["prior_binding"] == raw["first"]["binding"]
                and Decimal(raw["remainder"]["prior_covered_usd"]) == Decimal(raw["first"]["spent"]),
                "Remainder was not bound to this initial release and its spending")
    readers = [r for release in raw.values() for r in release["readers"]]
    deliveries = {mode: arm_summary([r for r in readers if r["delivery"] == mode]) for mode in MODES}
    first_readers = raw["first"]["readers"]
    check = release_check({mode: arm_summary([r for r in first_readers if r["delivery"] == mode]) for mode in MODES}, rules)
    return dict(format="banking-memory-cohort-results/v1", scope=releases, historical_missing=frozen["historical_missing"],
                releases=raw, tables=outcome_tables(readers), delivery=deliveries, first_release_check=check,
                sensitivity_without_083=outcome_tables([r for r in readers if r["task"] != "task_083"])["preload_minus_on_demand"])


def recovery_results(workspace, expected_plan, original, rules, interface):
    """Re-score the named losses against the original allocation and retain both observations."""
    root = safe_path(workspace / "runs", expected_plan["series"])
    plan, close, binding = _study(root, expected_plan["protocol_sha256"])
    require(plan == expected_plan and plan["original"] == original["binding"]
            and Decimal(plan["prior_covered_usd"]) == Decimal(original["spent"]), "Wrong original release or recovery cost")
    original_root = safe_path(workspace / "runs", original["binding"]["recording"])
    registry = digest(canonical(plan))
    native_modules(verify_checkout(workspace / "data/tau2-bench"))
    from tau2.domains.banking_knowledge.environment import get_tasks
    tasks = {t.id: t for t in get_tasks()}
    originals = {r["run"]: r for r in original["readers"]}
    combined = list(original["readers"])
    positions = {r["run"]: i for i, r in enumerate(combined)}
    observations, pairs = [], Counter()
    for case in plan["cases"]:
        task = case["task_id"]
        business, policy = BankingBusiness(tasks[task], case), PolicyEvidence()
        first_folder = safe_path(workspace / "runs", case["imported_first_pass"]["directory"])
        _, _, (source,) = sealed_rows(first_folder)
        observation(first_folder, source, policy=policy)
        name = case["imported_writer_stage"]
        wdir = safe_path(workspace / "runs", name["directory"])
        _, _, written = sealed_rows(wdir, name["seal"])
        banks = {}
        for w in written:
            folder = safe_path(wdir, w["directory"])
            checksum = w["result"]["log_sha256"]
            banks[w["arm"]] = read_form_bank(folder, expected_log_sha256=checksum)
            log = RecordedDecisions(folder, w["run_id"], checksum)
            policy.writer(task, w["arm"], "registered", w["status"], log, checksum=checksum)
        sdir, rows = _shard(root, case, "solve", "solve", close, registry)
        old_dir = original_root / ("task-" + case["task"]) / "solve"
        _, _, previous = sealed_rows(old_dir)
        old_by_arm = {r["arm"]: r for r in previous}
        for row in rows:
            arm = row["arm"]
            form, mode = arm.split("__")
            receipt = case["interrupted_readers"][arm]
            old = originals[receipt["run"]]
            require(old["success"] is None and old["reason"] in {"HTTP_502", "HTTP_503"} and old["journal"] == receipt["journal"]
                    and old["expected_writer"] == receipt["writer"] and old["writer_snapshot"] == receipt["snapshot"]
                    and (old["task"], old["form"], old["delivery"]) == (task, form, mode),
                    "Recovery would replace a graded or unrelated observation")
            obs = observation(sdir, row, writer=banks[form], business=business, policy=policy)
            check = None
            if obs["dispatched_worker"]:
                require(obs["run"] != old["run"] and obs["start"] == old["start"] and obs["start_source"] == old["start_source"]
                        and obs["first_worker"] == old["first_worker"], "Recovery changed the original worker's first wire")
                old_steps, _ = recorded_episode(old_dir, old_by_arm[arm], case, source=source, include_partial=True, **interface)
                new_steps, _ = recorded_episode(sdir, row, case, source=source, include_partial=obs["success"] is None, **interface)
                check = compare_steps(next((s for s in old_steps if s.actor == "worker"), None),
                                      next((s for s in new_steps if s.actor == "worker"), None))
            annotated = dict(obs, **{k: old[k] for k in ("task", "draw", "repeat", "form", "delivery", "cohort",
                                                         "customer", "expected_writer", "source")})
            combined[positions[old["run"]]] = annotated
            observations.append(dict(original_run=old["run"], original_journal=old["journal"], original_grade=old["success"],
                                     reader=annotated, first_public_choice_under_same_policy=check))
            pairs[task, form] += 1
    require(len({v["original_run"] for v in observations}) == len(observations) == plan["assigned"]["solve"],
            "Repeated or missing recovery allocation")
    by_timing = {}
    for count in (0, 1, 2):
        group = [r for r in combined if pairs[r["task"], r["form"]] == count]
        by_timing[str(count)] = outcome_tables(group)["delivery_pairs"]
    simultaneous = [r for r in combined if pairs[r["task"], r["form"]] != 1]
    costs = {k: sum((Decimal(s[k]) for s in close["stages"].values()), Decimal(0)) for k in ("reported", "held", "spent")}
    total = Decimal(plan["prior_covered_usd"]) + costs["spent"]
    require(total == Decimal(close["total_covered_usd"]) <= Decimal(plan["cumulative_maximum_usd"])
            and costs["reported"] + costs["held"] == costs["spent"], "Recovery costs do not reconcile")
    delivery = {mode: arm_summary([r for r in combined if r["delivery"] == mode]) for mode in MODES}
    return dict(format="banking-memory-cohort-recovery-sensitivity/v1", binding=binding, original_binding=original["binding"],
                original_covered_usd=plan["prior_covered_usd"], recovery_usd={k: str(v) for k, v in costs.items()},
                cumulative_covered_usd=str(total), observations=observations, combined_delivery=delivery,
                combined_tables=outcome_tables(combined), combined_readiness=release_check(delivery, rules),
                paired_by_number_of_recovered_arms=by_timing,
                without_mixed_time_pairs=outcome_tables(simultaneous)["preload_minus_on_demand"],
                without_083=outcome_tables([r for r in combined if r["task"] != "task_083"])["preload_minus_on_demand"])
