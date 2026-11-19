"""Reconstruct the retail writing and two-pass decisions from the registered journals."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import (
    ContractError,
    MemoryPolicy,
    canonical,
    digest,
)
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import endpoint_terms, write_json
from agent_memory_eval.experiments.forms_write import (
    read_form_bank,
    read_form_experience,
)
from agent_memory_eval.memory.context import (
    memory_system_suffix,
    past_memory_instruction,
    snapshot_message,
)
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path

if __package__:
    from .regrade_two_pass_forms import regrade
    from .run_recheck_rounds import (
        PROFILE,
        classify,
        design_for,
        prepare,
        read,
        sealed_rows,
        source_from,
        writer_texts,
    )
else:
    from regrade_two_pass_forms import regrade
    from run_recheck_rounds import (
        PROFILE,
        classify,
        design_for,
        prepare,
        read,
        sealed_rows,
        source_from,
        writer_texts,
    )

MUTATIONS = {
    "cancel_pending_order",
    "exchange_delivered_order_items",
    "modify_pending_order_address",
    "modify_pending_order_items",
    "modify_pending_order_payment",
    "modify_user_address",
    "return_delivered_order_items",
}


def mutation_key(name, args):
    args = dict(args)
    old, new = args.get("item_ids"), args.get("new_item_ids")
    if isinstance(old, list):
        if isinstance(new, list) and len(old) == len(new):
            args["item_pairs"] = sorted(zip(args.pop("item_ids"), args.pop("new_item_ids"), strict=True))
        elif new is None:
            args["item_ids"] = sorted(old)
    return canonical([name, args])


def inspect_episode(report, criteria):
    messages = report["messages"]
    replies = {m["id"]: (i, m) for i, m in enumerate(messages) if m["role"] == "tool"}
    reads, writes = [], []
    for i, m in enumerate(messages):
        if m["role"] != "assistant":
            continue
        for c in m.get("tool_calls") or []:
            j, r = replies.get(c["id"], (None, {}))
            ok = j is not None and not r.get("error") and r.get("execution_status") != "rejected"
            if c["name"] == "get_order_details":
                reads.append(dict(turn=i, returned=j, success=ok, order=c["arguments"].get("order_id")))
            if c["name"] in MUTATIONS:
                writes.append(dict(turn=i, returned=j, success=ok, name=c["name"], args=c["arguments"]))
    expected = [dict(name=a["name"], args=a["arguments"]) for a in criteria["actions"] if a["name"] in MUTATIONS]
    exact = all(w["success"] for w in writes) and Counter(
        mutation_key(w["name"], w["args"]) for w in writes
    ) == Counter(mutation_key(w["name"], w["args"]) for w in expected)
    before = []
    for w in writes:
        if "order_id" in w["args"]:
            before.append(
                any(
                    r["success"] and r["order"] == w["args"]["order_id"] and r["returned"] < w["turn"]
                    for r in reads
                )
            )
    lead = []
    if writes:
        for i, m in enumerate(messages[: writes[0]["turn"]]):
            if m["role"] in {"assistant", "user"} and m.get("content"):
                lead.append(dict(turn=i, role=m["role"], text=m["content"]))
    return dict(
        reads=reads,
        writes=writes,
        expected=expected,
        exact_mutations=exact,
        observed_order_before_first_write=before[0] if before else None,
        observed_before_each_order_write=before,
        first_write_lead=lead[-4:],
    )


def audit_case(args):
    workspace, root, cohort, cfg, base, row, task, models, registration, packet, index, labels, completion = args
    tid = row["task"]
    parent = root / cohort / ("task-" + tid)
    source = source_from(parent / "source", tid)
    banks, first_wire, ids, entries, stages = {}, set(), [], [], {}
    for stage in cfg["stages"]:
        path = parent / stage
        sha = digest((path / "completion.json").read_bytes())
        if completion["stages"][str(path.relative_to(root / cohort))] != sha:
            raise ContractError("Stage differs from the sealed round")
        close, ctl, rows = sealed_rows(path)
        design, jobs = design_for(cfg, base, cohort, row, stage, None if stage == "source" else source)
        if (
            ctl["design"] != design
            or ctl["implementation"] != registration["implementation"]
            or endpoint_terms(ctl["model_catalog_entries"]) != models
            or ctl["profile"]["config_sha256"] != registration["protocol_sha256"]
            or ctl["profile"]["guards"] != cfg["guards"][stage]
            or ctl["profile"]["stage"] != stage
            or any(any(r[k] != v for k, v in j.items()) for r, j in zip(rows, jobs, strict=True))
        ):
            raise ContractError("Stage settings or allocations differ")
        grades = {}
        if stage != "write":
            replay = regrade(path, workspace, workspace / "data/tau2-bench")
            if replay["new_model_or_network_attempts"]:
                raise ContractError("Offline native regrade attempted network access")
            grades = {g["index"]: g for g in replay["grades"]}
        cost = Decimal(0)
        reserve = Decimal(0)
        http = 0
        for r in rows:
            arm, run = r["arm"], r["run_id"]
            if r["directory"] != f"jobs/{r['index']:04d}":
                raise ContractError("Allocation journal is outside its scheduled slot")
            folder = safe_path(path, r["directory"])
            entry = dict(
                round=cohort,
                task=tid,
                customer=row["customer"],
                family=row["family"],
                stage=stage,
                arm=arm,
                run=run,
                status=r["status"],
                official=None,
                reason=r.get("reason"),
            )
            entries.append(entry)
            if r["status"] == "not_started":
                if folder.exists() or "result" in r:
                    raise ContractError("An unstarted allocation produced a journal")
                continue
            res = r["result"]
            if (
                r["status"] != res["status"]
                or run != res["run_id"]
                or digest((folder / "log.jsonl").read_bytes()) != res["log_sha256"]
            ):
                raise ContractError("Unsealed journal or wrong allocated run")
            evidence = read_experiment(folder)
            scan, state, _ = evidence
            blobs = BlobStore(folder)
            audit = audit_requests(folder, evidence=evidence)
            events = [e for e in scan["events"].values() if e["run_id"] == run]
            starts = [e["payload"] for e in events if e["type"] == "model.request_started"]
            ends = [e["payload"] for e in events if e["type"] == "model.request_finished"]
            received = {e["request_id"] for e in ends if e["status"] == "response"}
            billed = res["billing_by_role"]
            observed = Decimal(audit["observed_cost_usd_decimal"])
            if observed != sum(Decimal(b["reported_usd"]) for b in billed.values()):
                raise ContractError("Cost differs from provider evidence")
            charges = [e for e in ends if e["cost_status"] == "unknown"]
            reserved = res.get("unpriced_reservation")
            if reserved:
                from agent_memory_eval.models.openrouter import request_reservation_usd

                request = next(rq for rq in starts if rq["request_id"] == reserved["request_id"])
                role = request["actor_role"]
                estimate = request_reservation_usd(
                    blobs.get(request["request_ref"]),
                    ctl["model_catalog_entries"]["worker" if role == "writer" else role],
                )
                if (
                    len(charges) != 1
                    or charges[0]["request_id"] != request["request_id"]
                    or estimate != Decimal(reserved["usd"])
                    or blobs.get(charges[0]["response_ref"])["error"]["code"] != reserved["code"]
                    or reserved["code"] not in cfg["unpriced_http"]["codes"]
                ):
                    raise ContractError("Unknown cost is not tied to the eligible wire and error")
                reserve += estimate
            calls = sum(b["http_calls"] for b in billed.values())
            if calls != sum(e["delivery_status"] != "not_submitted" for e in ends):
                raise ContractError("HTTP and billing ledgers disagree")
            for e in ends:
                if e["status"] == "response":
                    ids.append(blobs.get(e["response_ref"])["id"])
            http += calls
            cost += observed
            entry.update(
                journal=str((folder / "log.jsonl").relative_to(workspace)),
                journal_sha256=res["log_sha256"],
                costs=billed,
                usd=str(observed),
                calls=calls,
                unpriced=len(charges),
                reservation=reserved,
                session=res["session_id"],
                error=res.get("error_code"),
            )
            if stage == "write":
                writing = state["memory_writes"][run]
                capsule = blobs.get(writing["experience_ref"])
                read_form_experience(blobs, scan, capsule)
                imp = blobs.get(capsule["source_ref"])
                expected = (
                    asset_text(ROOT, design["writing"]["common_instruction_ref"]).rstrip("\r\n")
                    + "\n"
                    + design["writing"]["lines"][arm]
                )
                if (
                    not source
                    or imp["source_run_id"] != source.source_run_id
                    or imp["source_log_sha256"] != source.source_log_sha256
                    or imp["source_end_event_id"] != source.source_end_event_id
                    or imp["public_context_sha256"] != source.public_context_sha256
                    or expected != writing["instruction"]
                    or writing["feedback"]["level"] != "none"
                ):
                    raise ContractError("Writer saw an unexpected public source or instruction")
                messages = [
                    dict(role="system", content=expected),
                    dict(role="user", content=source.public_context),
                    dict(role="user", content=canonical(dict(kind="evaluation_feedback", level="none")).decode()),
                ]
                for i, rq in enumerate(starts):
                    wire = blobs.get(rq["request_ref"])
                    if (
                        rq["actor_role"] != "writer"
                        or wire["messages"][:3] != messages
                        or wire["seed"] != cfg["writer_seed"]
                        or [t["function"]["name"] for t in wire["tools"]] != ["memory_store"]
                    ):
                        raise ContractError("Actual writer wire differs from its assigned treatment")
                    if i == 0:
                        if wire["messages"] != messages:
                            raise ContractError("Extra messages reached the first writer input")
                        first_wire.add(digest(canonical(dict(wire, messages=messages[1:]))))
                code = index[tid, arm]
                entry.update(
                    source_run=source.source_run_id,
                    source_public_sha256=source.public_context_sha256,
                    writer_instruction_sha256=digest(expected.encode()),
                    masked_id=code,
                    quote=labels["quotes"][code],
                    notes=packet[code],
                )
                if r["status"] in {"completed", "empty"}:
                    bank = read_form_bank(folder, expected_log_sha256=res["log_sha256"])
                    banks[arm] = bank
                    entry.update(snapshot=bank["snapshot_id"], items_sha256=digest(canonical(bank["items"])))
                continue
            bank = banks.get(arm)
            assignment = state["assignments"][run]
            start = scan["starts"][run]["payload"]
            native = blobs.get(assignment["native_config_ref"])
            seed = design["model"]["seed_pairs"][0]["native_and_solver"]
            policy = MemoryPolicy(**assignment["policy"])
            items = snapshot_items(blobs, state, start["start_snapshot_id"]) if policy.enabled else []
            if (
                not start["fresh_session"]
                or native["domain"] != "retail"
                or native["task_split_name"] != row["split"]
                or native["task_ids"] != [tid]
                or native["seed"] != seed
                or arm != assignment["arm"]
                or policy.read_mode != ("none" if arm == "no_memory" else "preload")
                or items != (bank["items"] if bank else [])
                or (arm not in cfg["controls"]) != (bank is not None)
                or start["source_run_ids"] != ([bank["run_id"]] if bank else [])
                or assignment["source_writer_snapshot_id"] != (bank["snapshot_id"] if bank else None)
                or assignment["public_context_sha256"] != (source.public_context_sha256 if bank else None)
            ):
                raise ContractError("Native episode differs from the selected source, seed, task or items")
            system = blobs.get(start["task_ref"])["content"]
            instruction = blobs.get(assignment["instruction_ref"])
            if policy.enabled:
                if digest(instruction.encode()) != design["second_pass"]["solve_instruction_ref"]["sha256"]:
                    raise ContractError("The common solve instruction changed")
                system += memory_system_suffix(policy, past_memory_instruction(instruction))
            for rq in starts:
                wire = blobs.get(rq["request_ref"])
                if rq["actor_role"] == "worker":
                    if (
                        wire["seed"] != seed
                        or wire["messages"][0] != dict(role="system", content=system)
                        or (policy.enabled and wire["messages"][1] != snapshot_message(items))
                        or (not policy.enabled and snapshot_message(items) in wire["messages"])
                        or any(t["function"]["name"].startswith("memory_") for t in wire.get("tools", []))
                    ):
                        raise ContractError("Worker wire has a different memory or policy")
                elif (
                    rq["actor_role"] != "user_simulator"
                    or "seed" in wire
                    or snapshot_message(items) in wire["messages"]
                ):
                    raise ContractError("Customer saw the assigned memory or seed")
            fields = Counter(
                e["payload"]["field"]
                for e in events
                if e["type"] == "memory.input_included" and e["payload"]["request_id"] in received
            )
            if sum(fields.values()) != audit["normal_response_memory_spans"]:
                raise ContractError("Input exposure differs from journal audit")
            entry.update(
                seed=seed,
                writer_run=bank["run_id"] if bank else None,
                snapshot=bank["snapshot_id"] if bank else None,
                items_sha256=digest(canonical(items)),
                title_spans=fields["title"],
                body_spans=fields["body"],
                source_run=source.source_run_id if bank else None,
                grading=res["grading_status"],
            )
            if r["index"] in grades:
                grade = grades[r["index"]]
                report = blobs.get(res["report_ref"])
                if report["seed"] != seed or res["official_success"] != (grade["official_score"] == 1):
                    raise ContractError("Native report or replayed grade differs")
                entry.update(
                    official=res["official_success"],
                    grade=grade,
                    report=str((folder / res["report_ref"]["path"]).relative_to(workspace)),
                    behavior=inspect_episode(report, task["evaluation_criteria"]),
                )
        if (
            http != close["http_calls"]
            or reserve != Decimal(close["reserved_usd"])
            or abs(cost - Decimal(str(close["reported_usd"]))) > Decimal("0.00000001")
            or cost + reserve > Decimal(str(cfg["guards"][stage]["max_usd"]))
        ):
            raise ContractError("Per-stage aggregate exceeds or differs from native accounting")
        stages[stage] = dict(
            completion_sha256=sha, calls=http, usd=str(cost), reserved_usd=str(reserve), regraded=len(grades)
        )
    if len(first_wire) > 1:
        raise ContractError("Paired writer inputs differ beyond their one system instruction")
    return dict(task=tid, stages=stages, entries=entries, generation_ids=ids)


def status(treatment, control):
    if None in (treatment, control):
        return "missing"
    return {(True, False): "gain", (False, True): "loss", (True, True): "both_pass", (False, False): "both_fail"}[
        treatment, control
    ]


def summarize(entries, cfg):
    by_slot = {(r["task"], r["stage"], r["arm"]): r for r in entries}
    if len(by_slot) != len(entries):
        raise ContractError("Duplicated task, stage and treatment slot")
    pairs = []
    for r in entries:
        if r["stage"] not in {"solve1", "solve2"} or r["arm"] in cfg["controls"]:
            continue
        form, variant = r["arm"].split("_")
        controls = [*cfg["controls"]]
        if variant == "recheck":
            controls.insert(0, form + "_original")
        source = by_slot[r["task"], "source", "no_memory"]
        for name in controls:
            ctrl = by_slot[r["task"], r["stage"], name]

            def exact(x):
                return x.get("behavior", {}).get("exact_mutations")

            pairs.append(
                dict(
                    task=r["task"],
                    customer=r["customer"],
                    family=r["family"],
                    wave=r["stage"],
                    form=form,
                    variant=variant,
                    treatment_run=r["run"],
                    control=name,
                    control_run=ctrl["run"],
                    outcome=status(r["official"], ctrl["official"]),
                    exact_mutations=status(exact(r), exact(ctrl)),
                    first_pass=source["official"],
                    vs_first_pass=status(r["official"], source["official"]),
                )
            )
    data = defaultdict(Counter)
    primary = [p for p in pairs if p["control"].endswith("_original")]
    for p in primary:
        a, b = (by_slot[p["task"], "write", p["form"] + "_" + v] for v in ("recheck", "original"))

        def has_recheck(writer):
            return None if writer.get("notes") is None else writer["quote"] is not None

        p["writing_change"] = status(has_recheck(a), has_recheck(b))
        for key in [
            "all",
            "family/" + p["family"],
            "form/" + p["form"],
            "task/" + p["task"],
            "writing_change/" + p["writing_change"],
        ]:
            data[key][p["outcome"]] += 1
    sums = defaultdict(Counter)
    first_pass = defaultdict(Counter)
    control_pairs = []
    for e in entries:
        if e["stage"] not in {"solve1", "solve2"}:
            continue
        s = sums[e["arm"]]
        s["planned"] += 1
        first = by_slot[e["task"], "source", "no_memory"]
        first_pass[e["arm"]][status(e["official"], first["official"])] += 1
        if e["arm"] == "empty_preload":
            baseline = by_slot[e["task"], e["stage"], "no_memory"]
            control_pairs.append(
                dict(
                    task=e["task"],
                    wave=e["stage"],
                    empty_run=e["run"],
                    no_memory_run=baseline["run"],
                    outcome=status(e["official"], baseline["official"]),
                )
            )
        if e["official"] is not None:
            s["graded"] += 1
            s["success"] += e["official"]
            s["exact_mutations"] += e["behavior"]["exact_mutations"]
    return dict(
        primary_counts=dict(data),
        arms=dict(sums),
        versus_first_pass=dict(first_pass),
        empty_vs_no_memory=control_pairs,
        pairs=pairs,
    )


def analyze(workspace, cohort):
    cfg, base, _, endpoints, rounds, tasks = prepare(workspace)
    root = workspace / "runs" / cfg["name"]
    registration = read(root / "registration.json")
    if (
        registration["rounds"] != rounds
        or registration["endpoints"] != endpoints
        or registration["protocol_sha256"] != digest((ROOT / PROFILE).read_bytes())
    ):
        raise ContractError("Global registration differs from the immutable selection")
    folder = root / cohort
    completion = read(folder / "round-completion.json")
    gate = read(folder / "writer-gate.json")
    packet, index = writer_texts(root, cfg, cohort, rounds[cohort])
    if gate != classify(packet, index, cfg, rounds[cohort], gate["labels"]) or completion["gate_sha256"] != digest(
        (folder / "writer-gate.json").read_bytes()
    ):
        raise ContractError("Writing manipulation check differs from its pre-outcome seal")
    args = [
        (
            workspace,
            root,
            cohort,
            cfg,
            base,
            t,
            tasks[t["task"]],
            endpoints,
            registration,
            packet,
            index,
            gate["labels"],
            completion,
        )
        for t in rounds[cohort]
    ]
    with ProcessPoolExecutor(max_workers=len(args)) as pool:
        cases = list(pool.map(audit_case, args))
    entries = [r for c in cases for r in c["entries"]]
    ids = [i for c in cases for i in c["generation_ids"]]
    if len(set(ids)) != len(ids) or len({r["session"] for r in entries if "session" in r}) != sum(
        "session" in r for r in entries
    ):
        raise ContractError("A provider generation or session was reused")
    totals = {}
    for stage in cfg["stages"]:
        group = [c["stages"][stage] for c in cases]
        totals[stage] = dict(
            calls=sum(s["calls"] for s in group),
            regraded=sum(s["regraded"] for s in group),
            usd=str(sum(Decimal(s["usd"]) for s in group)),
            reserved_usd=str(sum(Decimal(s["reserved_usd"]) for s in group)),
        )
    return dict(
        study=cfg["name"],
        round=cohort,
        implementation=registration["implementation"],
        protocol_sha256=registration["protocol_sha256"],
        round_completion_sha256=digest((folder / "round-completion.json").read_bytes()),
        writer_gate=gate["counts"],
        gate_pass=gate["content_changed"],
        stages=totals,
        distinct_generation_ids=len(ids),
        generation_ids=sorted(ids),
        cells=entries,
        **summarize(entries, cfg),
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("round", choices=["development", "confirmation"])
    p.add_argument("--workspace", type=Path, default=ROOT)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        p.error("Use a new evidence path")
    data = analyze(a.workspace.resolve(), a.round)
    write_json(a.output, data)
    print(data["primary_counts"]["all"], data["stages"])
