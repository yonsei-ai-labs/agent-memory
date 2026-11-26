"""Audit preregistered clause decisions and paired, held-out official banking outcomes."""

import argparse
from collections import Counter
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.claim_evidence import paired_counts
from agent_memory_eval.analysis.decision_attribution import Action
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.analysis.tau2_attribution import graded_episode
from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.memory.context import snapshot_message
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.recording.provenance import public_assistant_message
from agent_memory_eval.runner import fixed_decision
from scripts import run_banking_policy_cycle as cycle
from scripts.analyze_banking_memory import decision_evidence, feedback_contrasts, native_checks
from scripts.report_fresh_matched import episode
from scripts.run_full_retail import matches_accounting_seal

read = cycle.read


def dev_summary(rows, arms):
    by_block = {}
    for row in rows:
        key = (row["case"], row["form"], row["repeat"])
        cells = by_block.setdefault(key, {})
        require(row["arm"] not in cells, "Duplicate arm in a replay block")
        cells[row["arm"]] = None if row["behavior"] == "unobserved" else row["behavior"] == "target_action"
    require(all(set(c) == set(arms) for c in by_block.values()), "Incomplete replay schedule")

    def contrast(left, right):
        return paired_counts((block[left], block[right]) for block in by_block.values())

    return dict(
        by_arm={arm: dict(Counter(r["behavior"] for r in rows if r["arm"] == arm)) for arm in arms},
        background_vs_target=contrast("omit_background", "omit_target"),
        original_vs_target=contrast("original", "omit_target"),
        original_vs_background=contrast("original", "omit_background"),
    )


def development(workspace):
    spec, _, _, _, _ = cycle.design(workspace)
    frozen, cells = cycle.development(workspace)
    root = workspace / "runs" / spec["development_study"]
    reg, done = read(root / "registration.json"), read(root / "completion.json")
    require(
        done["registration_sha256"] == digest((root / "registration.json").read_bytes())
        and reg["slots"] == frozen["slots"]
        and reg["profile"]["authorization"]["preflight_sha256"]
        == digest(canonical(read(cycle.ROOT / cycle.PREFLIGHT))),
        "Different development registration",
    )
    model = dict(
        pricing=reg["reservation_pricing"],
        reservation_pricing=reg["reservation_pricing"],
        identity=reg["identity"],
    )
    rows, sessions, generations = [], [], []
    for slot in frozen["slots"]:
        folder = root / f"slot-{slot['slot']:03}"
        if str(slot["slot"]) not in done["journals"]:
            require(not (folder / "log.jsonl").exists(), "An unstarted slot has an undeclared journal")
            rows.append(dict(**slot, behavior="unobserved", status="not_started", journal_sha256=None))
            continue
        row = fixed_decision.receipt(
            folder,
            slot,
            model,
            lambda s, choice: cycle.classification(cells, spec, s, choice),
            result_origin=cycle.ORIGIN,
            full_audit=True,
        )
        require(
            row["journal_sha256"] == done["journals"][str(slot["slot"])] and row["requests"] <= 1,
            "Replaced or repeated fixed decision",
        )
        if row["behavior"] != "unobserved":
            require(
                row["complete"]
                and row["confirmed_input_fields"] == ["body", "title"]
                and len(row["generations"]) == 1,
                "A reported response lacks verified note exposure",
            )
            log = RecordedDecisions(folder, slot["run"], row["journal_sha256"])
            (req,) = log.requests
            wire = log.blobs.get(req["request_ref"])
            cell = cycle.cell_for(cells, slot)
            require(
                wire == cell[3]
                and log.items == cell[2]
                and log.start["source_run_ids"] == cell[4]
                and len(log.visible(req, wire)) == 2,
                "The actual wire, note or source differs",
            )
            fin = log.finishes[req["request_id"]]
            (outcome,) = log.of_type("outcome.scored")
            require(outcome["payload"]["official_success"] is None, "A replay acquired a full-episode grade")
            row.update(
                request_id=req["request_id"],
                response_sha256=fin["response_ref"]["sha256"],
                public_action=public_assistant_message(log.reply(req)["message"]),
            )
        row["status"] = "response" if row["behavior"] != "unobserved" else "ungraded"
        if row["session"]:
            sessions.append(row["session"])
        generations += row["generations"]
        rows.append(row)
    require(
        len(sessions) == len(set(sessions))
        and len(generations) == len(set(generations))
        and sum(r.get("submitted_or_uncertain", 0) for r in rows) == done["submitted"],
        "Session or generation reused",
    )
    for field in ("confirmed_usd", "held_usd"):
        require(sum(Decimal(r.get(field, "0")) for r in rows) == Decimal(done[field]), "Cost mismatch")
    arms = spec["development_arms"]
    return dict(
        protocol_sha256=digest((cycle.ROOT / cycle.PROFILE).read_bytes()),
        completion=done,
        implementation=reg["freeze"],
        sources=frozen["sources"],
        overall=dev_summary(rows, arms),
        by_state={s: dev_summary([r for r in rows if r["case"] == s], arms) for s in frozen["sources"]},
        by_state_form={
            s: {
                f: dev_summary([r for r in rows if (r["case"], r["form"]) == (s, f)], arms)
                for f in spec["development_forms"]
            }
            for s in frozen["sources"]
        },
        rows=rows,
    )


def checked_shard(root, folder, close, reg):
    seal = close["stages"][str(folder.relative_to(root))]
    if seal.get("status") == "not_started":
        require(not folder.exists(), "An unstarted shard has records")
        return None
    _, ctl, rows = cycle.bank.matched.sealed_rows(folder)
    require(
        matches_accounting_seal(folder, ctl["profile"]["guards"]["max_usd"], seal)
        and ctl["profile"]["registration_sha256"] == digest(canonical(reg["preflight"])),
        "Wrong shard registration or costs",
    )
    return rows


def grade_record(
    folder, row, case, registration, read_sha, first=None, bank=None, source_fields=(), documents=()
):
    result = row.get("result") or {}
    rec = dict(
        task=case["task_id"], arm=row["arm"], run=row.get("run_id"), success=None, status=row["status"]
    )
    if not result.get("log_sha256"):
        return rec
    path = folder / row["directory"]
    data = episode(
        path,
        row,
        case,
        registration,
        first["run_id"] if first is not None else None,
        bank,
        include_exposure=first is not None,
    )
    rec.update(
        status=data["status"],
        success=data["success"],
        log_sha256=data["log_sha256"],
        effort=data["effort"],
        opening=data.get("task_start_sha256"),
        error=result.get("error_code"),
        exposure=data.get("worker_exposure"),
        session=data["session_id"],
        source_request=data.get("source_worker_request_id"),
        first_request=data.get("first_worker_request_id"),
    )
    if rec["success"] is None:
        return rec
    histories = {}
    steps, _ = graded_episode(folder, row, case, read_sha, first, public_inputs=histories)
    log = RecordedDecisions(path, row["run_id"], data["log_sha256"])
    rec["native_action_checks"] = native_checks(log, rec["success"])
    evidence = decision_evidence(log, steps, histories, source_fields, set(documents))
    rec.update(evidence)
    first_step = next(s for s in steps if s.actor == "worker")
    rec["first_business_actions"] = [a.record() for a in first_step.actions]
    if first is not None:
        require(
            log.items == bank["items"]
            and log.start["source_run_ids"] == [bank["run_id"]]
            and "body" in rec["exposed_fields"],
            "Reader did not receive its assigned writer's bank",
        )
    return rec


def validation(workspace):
    spec, _, base, strict, _ = cycle.design(workspace)
    proposal, start = cycle.candidate_binding(workspace, spec, strict)
    return paired_validation_results(
        workspace, spec, proposal, start=start, task_ids=set(spec["heldout"].values()),
        read_sha=base["delivery_instruction"]["sha256"], ceiling=spec["total_ceiling_usd"],
        protocol_sha=digest((cycle.ROOT / cycle.PROFILE).read_bytes()),
        binding={"candidate_sha256": digest((cycle.ROOT / cycle.CANDIDATE).read_bytes())},
    )


def paired_validation_results(workspace, spec, proposal, *, start, task_ids, read_sha, ceiling, protocol_sha, binding):
    """Verify full banking consultations against their registered sources, banks, grades and costs."""
    repeats = cycle.PairedRepetitions.from_spec(spec)
    report_reasons = spec.get("adapter") == "account_opening"
    sr = workspace / "runs" / spec["source_study"]
    vr = workspace / "runs" / spec["validation_study"]
    regs, regv = [read(root / "registration.json") for root in (sr, vr)]
    sc, vc = [read(root / "completion.json") for root in (sr, vr)]
    for root, reg, close in ((sr, regs, sc), (vr, regv, vc)):
        require(
            close["registration_sha256"] == digest((root / "registration.json").read_bytes())
            and all(reg["preflight"].get(k) == v for k, v in binding.items()),
            "Sealed study or candidate mismatch",
        )
    require(
        regv["preflight"]["source_registration_sha256"] == sc["registration_sha256"],
        "Mismatched source series",
    )
    sources, writers, trials = {}, [], {"existing": [], "candidate": []}
    costs_by_policy = {
        p: {stage: {"reported": Decimal(0), "held": Decimal(0)} for stage in ("write", "solve")}
        for p in trials
    }
    docs = {
        c["task_id"]: read(
            workspace / "data/tau2-bench/data/tau2/domains/banking_knowledge/tasks" / (c["task_id"] + ".json")
        )["required_documents"]
        for c in regs["preflight"]["cases"]
    }
    for case in regs["preflight"]["cases"]:
        tid = case["task_id"]
        folder = sr / ("task-" + tid) / "source"
        source_rows = checked_shard(sr, folder, sc, regs)
        if source_rows is None:
            sources[tid] = dict(task=tid, success=None)
            if report_reasons:
                sources[tid].update(status="not_started", reason=sc["stages"][str(folder.relative_to(sr))].get("reason"))
            continue
        (r,) = source_rows
        sources[tid] = grade_record(
            folder, r, case, digest(canonical(regs["preflight"])), read_sha, documents=docs[tid]
        )
        if report_reasons and sources[tid]["success"] is None:
            sources[tid]["reason"] = r.get("reason") or (r.get("result") or {}).get("error_code")
    for case in regv["preflight"]["cases"]:
        policy, tid = case["writer_policy"], case["task_id"]
        root = vr / ("task-" + case["task"])
        bank_map = {}
        active = bool(case["scheduled_stages"])
        src_ref = case.get("imported_first_pass")
        first, source_fields = None, []
        if src_ref:
            source_dir = safe_path(workspace / "runs", src_ref["directory"])
            (first,) = cycle.bank.matched.sealed_rows(source_dir)[2]
            source = cycle.bank.matched.source_from(source_dir, tid)
            public = cycle.read_json(source.public_context.encode())
            source_fields = cycle.input_evidence(
                [public["task_context"], *public["messages"]], [], cycle.SEARCH_TOOLS
            )
        for stage_name, stage, reader_repeat in repeats.stages():
            folder = root / stage_name
            scheduled = cycle.bank.matched.jobs_for(case, stage)
            rows = checked_shard(vr, folder, vc, regv) if active else None
            if active:
                seal = vc["stages"][str(folder.relative_to(vr))]
                for key in costs_by_policy[policy][stage]:
                    costs_by_policy[policy][stage][key] += Decimal(seal[key])
            if rows is None:
                reason = (seal.get("reason") if active else case.get("missing"))
                rows = [dict(r, status="not_started", reason=reason) for r in scheduled]
            require(
                len(rows) == len(scheduled)
                and all(all(r[k] == v for k, v in j.items()) for j, r in zip(scheduled, rows)),
                "Changed writer or solver allocation",
            )
            for row in rows:
                result = row.get("result") or {}
                arm = row["arm"]
                if stage == "write":
                    rec = dict(task=tid, policy=policy, form=arm, status=row["status"], bank=None)
                    if report_reasons and row["status"] != "completed":
                        rec["reason"] = row.get("reason") or result.get("error_code")
                    if row["status"] == "completed":
                        b = read_form_bank(
                            folder / row["directory"], expected_log_sha256=result["log_sha256"]
                        )
                        wlog = RecordedDecisions(
                            folder / row["directory"], row["run_id"], result["log_sha256"]
                        )
                        require(
                            wlog.requests[0]["request_ref"]["sha256"] == case["first_writer_inputs"][arm]
                            and b["source_run_id"] == first["run_id"]
                            and b["items"]
                            and b["result_feedback"]["success"] is sources[tid]["success"],
                            "Unbound writer or feedback",
                        )
                        bank_map[arm] = b
                        rec.update(bank=b, writer_log_sha256=result["log_sha256"])
                    if "writer_draw" in case:
                        rec["writer_draw"] = case["writer_draw"]
                    writers.append(rec)
                else:
                    b = bank_map.get(arm.split("__")[0])
                    rec = grade_record(
                        folder,
                        row,
                        case,
                        digest(canonical(regv["preflight"])),
                        read_sha,
                        first,
                        b,
                        source_fields,
                        docs[tid],
                    )
                    if rec["success"] is not None:
                        require(
                            rec["opening"] == sources[tid]["opening"]
                            and rec["opening"]
                            and rec["source_request"] == sources[tid]["first_request"],
                            "Changed opening",
                        )
                    if "writer_draw" in case:
                        rec.update(writer_draw=case["writer_draw"], reader_repeat=reader_repeat)
                    if report_reasons and rec["success"] is None:
                        rec["reason"] = row.get("reason") or result.get("error_code")
                    trials[policy].append(rec)
    require(
        set(sources) == task_ids and len(writers) == spec["validation"]["write"]
        and sum(map(len, trials.values())) == spec["validation"]["solve"]
        and all(len(rows) == spec["validation"]["solve"] // 2 for rows in trials.values()),
        "Missing registered allocations",
    )
    sessions = [
        r["session"] for group in (list(sources.values()), *trials.values()) for r in group if "session" in r
    ]
    require(len(sessions) == len(set(sessions)), "Consultation session reused")
    contrast = feedback_contrasts(trials["candidate"], trials["existing"])

    def preload(group):
        group = group["preload"]
        return dict(
            pairs=group["outcomes"],
            existing_success=group["none_success"],
            candidate_success=group["result_only_success"],
            joint_success_effort={
                m: dict(pairs=v["pairs"], existing=v["none"], candidate=v["result_only"])
                for m, v in group["joint_success_effort"].items()
            },
        )

    costs = {}
    for root, reg, close, expected in (
        (sr, regs, sc, start),
        (vr, regv, vc, Decimal(sc["total_covered_usd"])),
    ):
        require(Decimal(reg["preflight"]["prior_covered_usd"]) == expected, "Budget carry differs")
        totals = {}
        for phase in sorted({p.split("/")[-1] for p in close["stages"]}):
            stages = [v for k, v in close["stages"].items() if k.endswith("/" + phase)]
            totals[phase] = {
                k: str(sum(Decimal(str(s[k])) for s in stages)) for k in ("reported", "held", "http")
            }
        require(
            expected + sum(Decimal(v["reported"]) + Decimal(v["held"]) for v in totals.values())
            == Decimal(close["total_covered_usd"]),
            "Unreconciled cumulative costs",
        )
        costs.update(totals)
    require(Decimal(vc["total_covered_usd"]) <= Decimal(ceiling), "Cumulative cap exceeded")
    if repeats.readers_per_bank > 1:
        waves = [costs.pop(name) for name, kind, _ in repeats.stages() if kind == "solve" and name in costs]
        costs["solve"] = {key: str(sum((Decimal(v[key]) for v in waves), Decimal(0)))
                          for key in ("reported", "held", "http")}
    return dict(
        protocol_sha256=protocol_sha,
        candidate=proposal,
        source_registration=sc["registration_sha256"],
        validation_registration=vc["registration_sha256"],
        costs=costs,
        total_covered_usd=vc["total_covered_usd"],
        overall=preload(contrast["by_delivery"]),
        policy_costs={
            p: {s: {k: str(v) for k, v in bill.items()} for s, bill in stages.items()}
            for p, stages in costs_by_policy.items()
        },
        by_task={t: preload(v) for t, v in contrast["by_task"].items()},
        by_form={f: preload(v) for f, v in contrast["by_form"].items()},
        versus_first={
            p: paired_counts((r["success"], sources[r["task"]]["success"]) for r in rows)
            for p, rows in trials.items()
        },
        sources=sources,
        writers=writers,
        trials=trials,
    )


def first_wire_proof(log, req, checksum, stage, policy, instruction):
    wire = log.blobs.get(req["request_ref"])
    if stage == "write":
        mark = "\n" + instruction
        system = wire["messages"][0]["content"]
        require(system.count(mark) == int(policy == "candidate"), "Unexpected writer treatment")
        wire["messages"][0]["content"] = system.replace(mark, "", 1)
    else:
        spans = log.visible(req, wire)
        require(
            wire["messages"][1] == snapshot_message(log.items)
            and {s["field"] for s in spans} == {"title", "body"},
            "First worker request lacks its assigned visible note",
        )
        wire["messages"].pop(1)
    return dict(
        run=log.run,
        journal_sha256=checksum,
        request=req["request_id"],
        wire_sha256=req["request_ref"]["sha256"],
        normalized_sha256=digest(canonical(wire)),
    )


def reported_native_actions(log, arguments):
    actions = []
    for q in log.requests:
        finish = log.finishes.get(q["request_id"], {})
        if finish.get("status") != "response":
            continue
        for call in log.reply(q)["message"].get("tool_calls") or []:
            f = call["function"]
            if f["name"] in arguments:
                arg = arguments[f["name"]]
                values = Action.from_function(f).record().get("arguments")
                actions.append(
                    dict(
                        role=q["actor_role"],
                        tool=f["name"],
                        argument=arg,
                        value=values.get(arg) if isinstance(values, dict) else None,
                        request=q["request_id"],
                        response_sha256=finish["response_ref"]["sha256"],
                    )
                )
    return actions


def input_audit(workspace):
    """Reopen actual paired inputs and index the two native actions discussed in the report."""
    spec, _, _, strict, _ = cycle.design(workspace)
    proposal, _ = cycle.candidate_binding(workspace, spec, strict)
    root = workspace / "runs" / spec["validation_study"]
    reg, close = read(root / "registration.json"), read(root / "completion.json")
    require(
        close["registration_sha256"] == digest((root / "registration.json").read_bytes())
        and reg["preflight"]["candidate_sha256"] == digest((cycle.ROOT / cycle.CANDIDATE).read_bytes()),
        "Input audit requires the registered writer comparison",
    )
    pairs = {}
    role = {"write": "writer", "solve": "worker"}
    arguments = {"transfer_to_human_agents": "reason", "apply_for_credit_card": "card_type"}
    for case in reg["preflight"]["cases"]:
        for stage in role:
            policy = case["writer_policy"]
            folder = root / ("task-" + case["task"]) / stage
            rows = checked_shard(root, folder, close, reg) if stage in case["scheduled_stages"] else None
            by_arm = {r["arm"]: r for r in rows or []}
            for job in cycle.bank.matched.jobs_for(case, stage):
                row = by_arm.get(job["arm"], {})
                form = job["arm"].split("__")[0]
                arms = pairs.setdefault((stage, case["task_id"], form), {})
                arms[policy] = None
                checksum = (row.get("result") or {}).get("log_sha256")
                if not checksum:
                    continue
                log = RecordedDecisions(folder / row["directory"], row["run_id"], checksum)
                req = next((q for q in log.requests if q["actor_role"] == role[stage]), None)
                if not req:
                    continue
                proof = first_wire_proof(log, req, checksum, stage, policy, proposal["add"])
                arms[policy] = proof
                if stage == "solve":
                    proof["reported_native_actions"] = reported_native_actions(log, arguments)
    records = []
    for (stage, task, form), arms in sorted(pairs.items()):
        require(set(arms) == {"existing", "candidate"}, "Missing allocated policy")
        matched = all(arms.values())
        if matched:
            require(
                arms["candidate"]["normalized_sha256"] == arms["existing"]["normalized_sha256"],
                "The actual paired inputs differ outside the intervention",
            )
        records.append(dict(stage=stage, task=task, form=form, both_checked=matched, arms=arms))
    return dict(
        registration_sha256=close["registration_sha256"],
        checked={stage: sum(r["stage"] == stage and r["both_checked"] for r in records) for stage in role},
        native_arguments=arguments,
        pairs=records,
    )


def referral_search_reference(domain, search, corpus_sha256, top_k):
    from agent_memory_eval.adapters.tau_banking_results import grep_ranking
    # A previously observed source-to-action citation serves only as a development reference.
    ref_path = cycle.ROOT / "artifacts/analyses/20261124-banking-required-results.json"
    ref = cycle.read(ref_path)
    token = "get_referral_link"
    receipts = {}
    for arm, proof in ref["exploratory_referral_search_citations"].items():
        w, b = proof["writing"], proof["decision"]
        require(any(token in c["quote"] for c in w["source_citations"]) and token in w["note"]["quote"]
                and b["exposure"] and any(token in c["quote"] for c in b["action_citations"]),
                "Development search lacks a cited public source, note, exposure or action")
        trial, = [r for r in ref["trials"] if r["task"] == "task_015" and r["arm"] == arm]
        receipts[arm] = dict(version=w["version"], decision_sha256=b["request_sha256"], official_success=trial["success"])
    targets = cycle.read(domain / "tasks/task_015.json")["required_documents"]
    return dict(task="task_015", reference_sha256=digest(ref_path.read_bytes()),
        corpus_sha256=corpus_sha256, top_k=top_k, receipts=receipts,
        exact_literal_search=grep_ranking(search, token, {doc: doc for doc in targets}))


def common_measurements(workspace):
    from agent_memory_eval.adapters.tau_banking_results import BANKING_SUCCESS, grep_ranking, known_customer_groups, native_grep, policy_table
    from agent_memory_eval.analysis.paired_trials import Trial, compare_trials
    from agent_memory_eval.analysis.action_grounding import literal_citations
    from scripts import analyze_banking_replacement as replacement

    def published_v1(comparison):
        # The v1 bank table reports fully graded sources. New reports name the inclusion rule explicitly.
        require(comparison["sources"].pop("included") == comparison["sources"]["complete"], "Changed v1 source denominator")
        del comparison["metric"], comparison["cohort"]
        return comparison

    domain = workspace / "data/tau2-bench/data/tau2/domains/banking_knowledge"
    studies = {}
    validation_data = validation(workspace)
    for name, profile, results in (
        ("writer_cycle", cycle.PROFILE, validation_data),
        ("replacement_036", replacement.study.PROFILE, replacement.observed(workspace)),
    ):
        spec = cycle.read(cycle.ROOT / profile)
        table = policy_table(results, workspace / "runs" / spec["validation_study"], domain)
        table["comparison"] = published_v1(table["comparison"])
        table["customer_split"] = known_customer_groups(domain, ["task_001", "task_008", "task_010", "task_015"],
                                                       results["sources"])
        table["protocol_sha256"] = results["protocol_sha256"]
        studies[name] = table

    # Verify the actual first inputs and every worker choice before designating an unexposed calibration.
    arms, first_wires, first_actions = [], [], []
    for run in ("tau-banking-required-01", "tau-banking-result-feedback-01"):
        rows, wires, actions = [], {}, {}
        root = workspace / "runs" / run
        for folder in sorted(root.glob("task-*/solve")):
            for row in cycle.bank.matched.sealed_rows(folder)[2]:
                if not row["arm"].endswith("__on_demand"):
                    continue
                result = row["result"]
                log = RecordedDecisions(safe_path(folder, row["directory"]), row["run_id"], result["log_sha256"])
                worker = [r for r in log.requests if r["actor_role"] == "worker"]
                require(not log.of_type("memory.input_included") and not any(
                    c["function"]["name"] in {"memory_list", "memory_get"} for req in worker
                    for c in log.reply(req)["message"].get("tool_calls") or []
                ), "The calibration includes a read or exposure")
                task = row["task_id"]
                key = (task, row["arm"])
                require(key not in wires, "Duplicate calibration allocation")
                wires[key] = digest(canonical(log.blobs.get(worker[0]["request_ref"])))
                actions[key] = [Action.from_function(c["function"]).record() for c in
                                log.reply(worker[0])["message"].get("tool_calls") or []]
                require([a["name"] for a in actions[key]] == ["grep"], "Calibration first action is not one search")
                receipt, = [e["payload"]["receipt_ref"] for e in log.of_type("business.transition")
                            if e["payload"].get("origin") == "matched_trial.native_control"]
                source_run = log.blobs.get(receipt)["source_run_id"]
                scored, = log.of_type("outcome.scored")
                require(scored["payload"]["official_success"] is result["official_success"], "Changed calibration grade")
                rows.append(Trial(task, row["arm"], source_run, True, result["official_success"],
                                  metric=BANKING_SUCCESS, cohort="banking-writer-feedback-unexposed-pairs"))
        arms.append(rows)
        first_wires.append(wires)
        first_actions.append(actions)
    require(len(first_wires[0]) == 20 and first_wires[0] == first_wires[1], "Different calibration inputs")
    aa = published_v1(compare_trials(*arms, metrics=()))

    design = cycle.read(cycle.ROOT / "protocols/drafts/tau-banking-scoped-writer-development-v1.json")
    decisions = {(s["task"], s["worker_ordinal"]): s for s in design["states"]}
    source_root = workspace / "runs" / cycle.read(cycle.ROOT / cycle.PROFILE)["source_study"]

    diagnostic_path = cycle.ROOT / "artifacts/analyses/20261125-banking-policy-cycle-diagnosis.json"
    diagnostic = cycle.read(diagnostic_path)
    cycle.bank.matched.modules_for(workspace)
    search, _, top_k = native_grep(diagnostic["corpus_sha256"])
    opportunities = []
    for task, ordinal, term, use in (
        ("task_017", 1, "Silver Rewards", "candidate_product_hint_requires_current_verification"),
        ("task_016", 1, "IN_PROGRESS", "historical_status_requires_current_verification"),
        ("task_002", 6, "$200", "current_information_control"),
    ):
        state = decisions[task, ordinal]
        folder = source_root / ("task-" + task) / "source"
        row, = cycle.bank.matched.sealed_rows(folder)[2]
        log = RecordedDecisions(safe_path(folder, row["directory"]), state["source_run"], validation_data["sources"][task]["log_sha256"])
        req = [r for r in log.requests if r["actor_role"] == "worker"][ordinal - 1]
        wire = log.blobs.get(req["request_ref"])
        require(row["run_id"] == state["source_run"] and req["request_id"] == state["request_id"]
                and req["request_ref"]["sha256"] == state["request_sha256"]
                and not log.of_type("memory.input_included"), "Wrong memory-free decision")
        experience = cycle.bank.matched.source_from(folder, task)
        public = cycle.read_json(experience.public_context.encode())
        source_hits = literal_citations(public["messages"], term)
        current_hits = literal_citations(dict(messages=wire["messages"], tools=wire.get("tools", [])), term)
        notes = {}
        for w in validation_data["writers"]:
            if w["task"] == task:
                item, = w["bank"]["items"]
                require(w["bank"]["source_run_id"] == state["source_run"], "Different writer source")
                notes[w["policy"] + "/" + w["form"]] = dict(version=item["version"],
                    citations=literal_citations({k: item[k] for k in ("title", "body")}, term))
        opportunity = dict(task=task, ordinal=ordinal, proposed_use=use, term=term,
            decision_request=req["request_ref"]["sha256"], source_public_sha256=experience.public_context_sha256,
            current=current_hits, source=source_hits, notes=notes, memory_free_observations=1,
            positive_control_tested=False)
        if task == "task_017":
            # The search term is selected from public experience. Witnesses are used only to score the rankings.
            archived, = [e for e in diagnostic["episodes"] if e["policy"] == "source" and e["task"] == task]
            original = archived["searches"][0]
            worker_requests = [r for r in log.requests if r["actor_role"] == "worker"]
            grep_index, = [i for i, r in enumerate(worker_requests) if r["request_id"] == original["request"]]
            actions = [Action.from_function(c["function"]).record() for c in
                       log.reply(worker_requests[grep_index])["message"]["tool_calls"]]
            require(any(a["name"] == "grep" and a["arguments"]["pattern"] == original["pattern"] for a in actions)
                    and archived["first_worker"]["request"] == req["request_id"] and source_hits, "Unbound search inputs")
            witnesses = diagnostic["witnesses"][task]
            rankings = {label: grep_ranking(search, pattern, witnesses) for label, pattern in
                        (("archived", original["pattern"]), ("source_literal", term))}
            require(rankings["archived"]["ranking_sha256"] == original["ranking_sha256"], "Archived ranking changed")
            opportunity["native_search_check"] = dict(corpus_sha256=diagnostic["corpus_sha256"], top_k=top_k,
                archived_first_search_ordinal=grep_index + 1, archived_first_search_request=original["request"],
                witness_documents=witnesses, diagnostic_sha256=digest(diagnostic_path.read_bytes()), rankings=rankings)
        opportunities.append(opportunity)
    development_search = referral_search_reference(domain, search, diagnostic["corpus_sha256"], top_k)
    return dict(format="agent-memory-policy-measurements/v1", studies=studies,
                unexposed_calibration=dict(scope="four separate calibration tasks and archived periods",
                    verified_equal_first_inputs=len(first_wires[0]),
                    changed_first_business_calls=sum(first_actions[0][k] != first_actions[1][k] for k in first_wires[0]),
                    pairs=aa["pairs"], sources=aa["sources"]),
                retrospective_decision_screen=opportunities, development_search_reference=development_search, new_provider_requests=0)


def clustered_results(result, customers):
    from agent_memory_eval.analysis.clustered_outcomes import clustered_contrast

    def summaries(slots):
        def evaluate(selected):
            return clustered_contrast(((customers[t], t, f, slot, arms["existing"], arms["candidate"])
                                       for (t, f, slot), arms in selected), cluster_unit="customer")
        forms = sorted({f for _, f, _ in slots})
        return dict(overall=evaluate(slots.items()),
                    by_form={f: evaluate((k, a) for k, a in slots.items() if k[1] == f) for f in forms})

    official, opened, writing = {}, {}, {}
    for policy, rows in result["trials"].items():
        for row in rows:
            key = row["task"], row["arm"].split("__")[0], (row["writer_draw"], row["reader_repeat"])
            official.setdefault(key, {})[policy] = row["success"]
            opened.setdefault(key, {})[policy] = (row["account_opening"]["effects"]["all_target_accounts_opened"]
                                                  if row["success"] is not None else None)
    for row in result["writing_outcomes"]["rows"]:
        key = row["task"], row["form"], row["writer_draw"]
        writing.setdefault(key, {})[row["policy"]] = (row["rating"]["unsupported_or_contradicted"]
                                                      if row["rating"] is not None else None)
    unit = "banks" if result["writing_outcomes"].get("unit") == "writer_bank" else "items"
    return {"official_success": summaries(official), "all_requested_accounts_opened": summaries(opened),
            unit + "_with_unsupported_or_contradicted": summaries(writing)}


def repetition_results(result, inputs, repeats):
    from collections import defaultdict

    counts = {}
    for stage, expected in (("write", repeats.writer_draws), ("solve", repeats.readers_per_bank)):
        groups = [hashes for (s, *_), hashes in inputs.items() if s == stage]
        require(all(len(hashes) <= expected and len(set(hashes)) == 1 for hashes in groups),
                "Actual first input changed between repeated writers or readers of one bank")
        assigned = (len(result["writers"]) // repeats.writer_draws if stage == "write" else len(result["writers"]))
        counts[stage] = dict(assigned_groups=assigned, groups_with_input=len(groups),
                             complete_identical_groups=sum(len(g) == expected for g in groups))
    contents = defaultdict(list)
    writer_ids = [row["bank"]["run_id"] for row in result["writers"] if row["bank"]]
    require(len(writer_ids) == len(set(writer_ids)), "Separate writing assignments reused a recorded writer")
    for row in result["writers"]:
        if row["bank"]:
            contents[row["task"], row["policy"], row["form"]].append(
                digest(canonical([{k: item[k] for k in ("title", "body")} for item in row["bank"]["items"]])))
    complete = [group for group in contents.values() if len(group) == repeats.writer_draws]
    writer_copies = dict(assigned_groups=len(result["writers"]) // repeats.writer_draws,
                         all_draws_stored=len(complete), identical_contents=sum(len(set(g)) == 1 for g in complete))
    outcomes = {}
    for policy, rows in result["trials"].items():
        banks = defaultdict(list)
        for row in rows:
            banks[row["task"], row["arm"], row["writer_draw"]].append(row["success"])
        require(all(len(v) == repeats.readers_per_bank for v in banks.values()), "Incomplete reader registration")
        graded = [values for values in banks.values() if None not in values]
        outcomes[policy] = dict(assigned_banks=len(banks), all_readers_graded=len(graded),
                                banks_with_both_success_and_failure=sum(len(set(v)) > 1 for v in graded))
    return dict(first_inputs=counts, writer_contents=writer_copies, official_outcome_within_bank=outcomes)


def registered_account_results(workspace, profile, *, packets_only=False, ratings=None):
    from dataclasses import replace
    from agent_memory_eval.adapters.tau_banking_opening import FIELDS, OPENING_OUTCOMES, observed_accounts, opening_receipts
    from agent_memory_eval.adapters.tau_banking_results import BANKING_SUCCESS, banking_trials
    from agent_memory_eval.analysis.paired_trials import compare_trials
    from agent_memory_eval.analysis.receipt_review import join_bank_reviews, join_outcome_reviews, outcome_packet
    from agent_memory_eval.recording.assets import asset_text
    from agent_memory_eval.recording.journal import BlobStore
    from tau2.domains.banking_knowledge.environment import get_tasks

    spec = read(cycle.ROOT / profile)
    repeats = cycle.PairedRepetitions.from_spec(spec)
    repeated = "repetitions" in spec
    approval = read(cycle.ROOT / spec["authorization_file"])
    protocol_sha = digest((cycle.ROOT / profile).read_bytes())
    binding = dict(protocol_sha256=protocol_sha, preflight_sha256=approval["preflight_sha256"], approval=approval)
    base = read(cycle.ROOT / spec["banking_profile"]["path"])
    read_sha = read(cycle.ROOT / base["base"]["path"])["delivery_instruction"]["sha256"]
    result = paired_validation_results(workspace, spec, spec, start=Decimal(0), task_ids=set(spec["tasks"]),
                                       read_sha=read_sha, ceiling=approval["maximum_usd"], protocol_sha=protocol_sha,
                                       binding=binding)
    tasks = {t.id: t for t in get_tasks()}
    keys, packets, public_sources, normalized, token_usage = [], {}, {}, {}, {}
    by_slot = {(p, r["task"], r["arm"], r.get("writer_draw", 1), r.get("reader_repeat", 1)): r
               for p, rows in result["trials"].items() for r in rows}
    require(len(by_slot) == spec["validation"]["solve"], "Duplicate reader slot")
    customer_ids = {}
    consistent_inputs = {}
    for phase, run in (("source", spec["source_study"]), ("validation", spec["validation_study"])):
        root = workspace / "runs" / run
        reg, close = read(root / "registration.json"), read(root / "completion.json")
        for case in reg["preflight"]["cases"]:
            tid = case["task_id"]
            policy = case.get("writer_policy")
            require(customer_ids.setdefault(tid, case["customer"]) == case["customer"], "Customer identity changed")
            draw = case.get("writer_draw", 1)
            for stage_name, stage, reader_repeat in ([("source", "source", 0)] if phase == "source" else repeats.stages()):
                folder = root / ("task-" + case["task"]) / stage_name
                if str(folder.relative_to(root)) not in close["stages"]:
                    require(not case["scheduled_stages"], "Active registered shard is unaccounted")
                    continue
                for row in checked_shard(root, folder, close, reg) or []:
                    raw = row.get("result") or {}
                    form = row["arm"].split("__")[0]
                    usage = token_usage.setdefault(stage, {}).setdefault(policy or "no_memory", Counter())
                    if raw:
                        usage["recorded_assignments"] += 1
                        bills = raw.get("billing_by_role", {}).values()
                        usage["reported_tokens"] += sum(b["total_tokens"] for b in bills)
                        usage["http_requests_with_unknown_cost"] += sum(b["unknown_count"] for b in bills)
                    if stage != "source" and raw.get("log_sha256"):
                        log = RecordedDecisions(folder / row["directory"], row["run_id"], raw["log_sha256"])
                        role = "writer" if stage == "write" else "worker"
                        req = next((r for r in log.requests if r["actor_role"] == role), None)
                        if req:
                            clause = asset_text(cycle.ROOT, spec["clause"]).strip()
                            proof = first_wire_proof(log, req, raw["log_sha256"], stage, policy, clause)
                            normalized.setdefault((stage, tid, form, draw, reader_repeat), {})[policy] = proof
                            if repeated:
                                group = (stage, tid, policy, form, draw if stage == "solve" else 0)
                                consistent_inputs.setdefault(group, []).append(proof["wire_sha256"])
                    if stage == "write" or raw.get("grading_status") != "graded":
                        continue
                    messages = BlobStore(folder / row["directory"]).get(raw["report_ref"])["messages"]
                    business = observed_accounts(tasks[tid], messages, raw["final_db_hash"])
                    if stage == "source":
                        src = cycle.bank.matched.source_from(folder, tid)
                        public = cycle.read_json(src.public_context.encode())["messages"]
                        receipts = opening_receipts(public)
                        provenance = lambda rr: [(r["call_id"], r["arguments"], r["outcome"], r["receipt_sha256"]) for r in rr]
                        require(provenance(receipts) == provenance(business["receipts"]),
                                "The writer's public projection and the agent's recorded opening receipts disagree")
                        public_sources[tid] = (receipts, next(m["content"] for m in public if m["role"] == "user"))
                        target = result["sources"][tid]
                    else:
                        target = by_slot[policy, tid, row["arm"], draw, reader_repeat]
                    target["account_opening"] = business

    bank_review = "ratings_schema" in spec or any(
        len(w["bank"]["items"]) > 1 for w in result["writers"] if w["bank"])
    version = "v2" if bank_review else "v1"
    for writer in result["writers"]:
        items = []
        if writer["bank"]:
            receipts, opening = public_sources[writer["task"]]
            for item in writer["bank"]["items"]:
                key, packet = outcome_packet(item, receipts, opening, claim_outcomes=OPENING_OUTCOMES,
                                             subject_fields=FIELDS if bank_review else (),
                                             required_subject_fields=("account_class",) if bank_review else ())
                packets[key] = packet
                items.append(dict(id=item["id"], version=item["version"], packet=key))
        assignment = dict(policy=writer["policy"], task=writer["task"], form=writer["form"])
        if bank_review:
            assignment.update(items=items, status=writer["status"], reason=writer.get("reason"))
        else:
            assignment["packet"] = items[0]["packet"] if items else None
        if repeated:
            assignment["writer_draw"] = writer["writer_draw"]
        keys.append(assignment)
    if packets_only:
        return dict(format="receipt-review-packets/" + version, packets={k: packets[k] for k in sorted(packets)})

    wire_checks = []
    for (stage, tid, form, draw, repeat), pair in sorted(normalized.items()):
        complete = set(pair) == {"candidate", "existing"}
        if complete:
            require(pair["existing"]["normalized_sha256"] == pair["candidate"]["normalized_sha256"],
                    "Paired actual requests differ outside the treatment")
        check = dict(stage=stage, task=tid, form=form, paired=complete, proofs=pair)
        if repeated:
            check.update(writer_draw=draw, reader_repeat=repeat)
        wire_checks.append(check)
    if ratings is None:
        votes = {}
    else:
        if "ratings_schema" in spec:
            from jsonschema import Draft202012Validator
            Draft202012Validator(cycle.read_json(asset_text(cycle.ROOT, spec["ratings_schema"]).encode())).validate(ratings)
        rated_keys = set(ratings["ratings"])
        require(ratings["format"] == "opening-outcome-ratings/" + version and ratings["reviewer"]
                and (rated_keys <= set(packets) if bank_review else rated_keys == set(packets)),
                "Invalid named outcome review or masked packet keys")
        votes = ratings["ratings"]
    result["token_usage"] = token_usage
    result["wire_checks"] = wire_checks
    join = join_bank_reviews if bank_review else join_outcome_reviews
    result["writing_outcomes"] = join(packets, keys, votes)
    if bank_review:
        result["missing_readers_by_reason"] = {
            policy: Counter(row.get("reason") or row.get("error") or row["status"] for row in rows
                            if row["success"] is None) for policy, rows in result["trials"].items()}
    result["reviewer"] = ratings["reviewer"] if ratings else None
    arms = [banking_trials(result, p, BANKING_SUCCESS) for p in ("existing", "candidate")]
    result["official_paired"] = compare_trials(*arms, test_signs=not repeated)
    metric = replace(BANKING_SUCCESS, name="banking.all_target_accounts_opened",
                     definition="Every registered owner, account type and class produced a new native account row.")
    process = []
    for group, policy in zip(arms, ("existing", "candidate")):
        rows = result["trials"][policy]
        process.append([replace(t, metric=metric, success=None if t.success is None else
                                r["account_opening"]["effects"]["all_target_accounts_opened"])
                        for t, r in zip(group, rows, strict=True)])
    result["all_targets_paired"] = compare_trials(*process, test_signs=not repeated)
    if repeated:
        result["customer_weighted"] = clustered_results(result, customer_ids)
        result["repetitions"] = repetition_results(result, consistent_inputs, repeats)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    analyzers = {"development": development, "validation": validation, "input-audit": input_audit,
                 "common-measurements": common_measurements}
    parser.add_argument("phase", choices=[*analyzers, "account-outcomes", "account-review"])
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--ratings", type=Path)
    parser.add_argument("--workspace", type=Path, default=cycle.ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.phase in {"account-outcomes", "account-review"}:
        require(args.profile is not None, "The receipt audit needs its registered protocol")
        result = registered_account_results(args.workspace.resolve(), args.profile,
                                            packets_only=args.phase == "account-review",
                                            ratings=read(args.ratings) if args.ratings else None)
    else:
        result = analyzers[args.phase](args.workspace.resolve())
    cycle.save(args.output, result)
