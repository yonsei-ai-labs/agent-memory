"""Audit preregistered clause decisions and paired, held-out official banking outcomes."""

import argparse
from collections import Counter
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.claim_evidence import paired_counts
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.analysis.tau2_attribution import graded_episode
from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.experiments.forms_write import read_form_bank
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
    spec, _, _, strict, _ = cycle.design(workspace)
    proposal, start = cycle.candidate_binding(workspace, spec, strict)
    sr = workspace / "runs" / spec["source_study"]
    vr = workspace / "runs" / spec["validation_study"]
    regs, regv = [read(root / "registration.json") for root in (sr, vr)]
    sc, vc = [read(root / "completion.json") for root in (sr, vr)]
    for root, reg, close in ((sr, regs, sc), (vr, regv, vc)):
        require(
            close["registration_sha256"] == digest((root / "registration.json").read_bytes())
            and reg["preflight"]["candidate_sha256"] == digest((cycle.ROOT / cycle.CANDIDATE).read_bytes()),
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
    _, _, base, _, _ = cycle.design(workspace)
    read_sha = base["delivery_instruction"]["sha256"]
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
            continue
        (r,) = source_rows
        sources[tid] = grade_record(
            folder, r, case, digest(canonical(regs["preflight"])), read_sha, documents=docs[tid]
        )
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
            source_fields = cycle.input_evidence([public["task_context"], *public["messages"]], [], {"grep"})
        for stage in ("write", "solve"):
            folder = root / stage
            scheduled = cycle.bank.matched.jobs_for(case, stage)
            rows = checked_shard(vr, folder, vc, regv) if active else None
            if active:
                seal = vc["stages"][str(folder.relative_to(vr))]
                for key in costs_by_policy[policy][stage]:
                    costs_by_policy[policy][stage][key] += Decimal(seal[key])
            rows = rows if rows is not None else [dict(r, status="not_started") for r in scheduled]
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
                    trials[policy].append(rec)
    require(
        len(sources) == 4 and len(writers) == 40 and all(len(c) == 20 for c in trials.values()),
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
    require(Decimal(vc["total_covered_usd"]) <= Decimal(spec["total_ceiling_usd"]), "Cumulative cap exceeded")
    return dict(
        protocol_sha256=digest((cycle.ROOT / cycle.PROFILE).read_bytes()),
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("development", "validation"))
    parser.add_argument("--workspace", type=Path, default=cycle.ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cycle.save(
        args.output, (development if args.phase == "development" else validation)(args.workspace.resolve())
    )
