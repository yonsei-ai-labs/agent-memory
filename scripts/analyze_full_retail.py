"""Strict offline verification and matched recovery/loss estimates for the full retail split."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from decimal import Decimal
from multiprocessing import get_context
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.adapters.retail_grader import checked_answer, judge_wire
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
from agent_memory_eval.models.openrouter import request_reservation_usd
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path

if __package__:
    from .analyze_recheck_rounds import inspect_episode, status
    from .run_full_retail import (
        accounting,
        full_design,
        modules_for,
        preflight,
        prepare,
        read,
        recorded_source,
        sealed_rows,
    )
else:
    from analyze_recheck_rounds import inspect_episode, status
    from run_full_retail import (
        accounting,
        full_design,
        modules_for,
        preflight,
        prepare,
        read,
        recorded_source,
        sealed_rows,
    )


def require(test, message):
    if not test:
        raise ContractError(message)


def verify_grade(task, sim_data, before, expected, wires, config, model, native):
    from tau2.data_model.simulation import SimulationRun
    from tau2.data_model.semantic_communication import CommunicateScoring
    from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
    from tau2.multilingual.runtime import language_environment_kwargs, task_digest
    from tau2.runner.presets import reload_recorded_call

    sim = SimulationRun.model_validate(before)
    lang = sim.language_context
    require(
        sim.task_id == task.id
        and lang.task_sha256 == task_digest(task)
        and sim.seed == native["seed"]
        and sim.reward_info is None,
        "Stored before-grade simulation has a different task, seed or embedded outcome",
    )
    require(
        {k: v for k, v in before.items() if k != "reward_info"}
        == {k: v for k, v in sim_data.items() if k != "reward_info"},
        "Trajectory changed during native evaluation",
    )
    seen = []

    def recorded(*, model: str, messages, call_name, **kwargs):
        require(
            len(wires) == 1 and not seen, "Missing or multiple stored grader requests"
        )
        request, reply = wires[0]
        wire = judge_wire(config, model, messages, call_name, kwargs)
        require(wire == request, "Native assertion call does not match archived input")
        seen.append(True)
        return checked_answer(
            reply, task.evaluation_criteria.nl_assertions, config, judge_model
        )

    judge_model = model
    scoring = CommunicateScoring.model_validate(native["communicate_scoring"])
    env = language_environment_kwargs(reload_recorded_call(lang, "retail")) or None
    with patch("tau2.evaluator.evaluator_nl_assertions.generate", recorded):
        result = evaluate_simulation(
            simulation=sim,
            task=task,
            evaluation_type=EvaluationType.ALL,
            solo_mode=False,
            domain="retail",
            env_kwargs=env,
            strict_replay=True,
            communicate_scoring=scoring,
            customer_language=lang.provider_language,
        )
    require(
        canonical(result.model_dump(mode="json")) == canonical(sim_data["reward_info"])
        and result.reward == expected
        and len(seen) == len(wires),
        "Strict official replay differs from the archived report",
    )
    return dict(
        db_match=result.db_check.db_match if result.db_check is not None else None,
        nl=[
            dict(assertion=a.nl_assertion, met=a.met)
            for a in result.nl_assertions or []
        ],
        replayed_judge_answers=len(seen),
        reward_sha256=digest(canonical(sim_data["reward_info"])),
    )


def read_request_data(folder, run, res, workspace, models, judge):
    evidence = read_experiment(folder)
    scan, state, _ = evidence
    blobs = BlobStore(folder)
    audit = audit_requests(folder, evidence=evidence)
    events = [e for e in scan["events"].values() if e["run_id"] == run]
    starts = {
        e["payload"]["request_id"]: e["payload"]
        for e in events
        if e["type"] == "model.request_started"
    }
    finishes = {
        e["payload"]["request_id"]: e["payload"]
        for e in events
        if e["type"] == "model.request_finished"
    }
    require(set(starts) == set(finishes), "Incomplete role request span")
    records = []
    ids = []
    bills = defaultdict(
        lambda: dict(
            requests=0,
            http_calls=0,
            total_tokens=0,
            reported_usd=Decimal(0),
            unknown_count=0,
        )
    )
    reserved = res.get("unpriced_reservation")
    for rid, s in starts.items():
        end = finishes[rid]
        actor = s["actor_role"]
        raw = blobs.get(end["response_ref"]) if end["response_ref"] else None
        wire = blobs.get(s["request_ref"])
        m = (
            judge
            if actor == "grader"
            else models["worker" if actor == "writer" else actor]
        )
        require(
            wire["model"] == m["id"]
            and wire["provider"]
            == {
                "only": [m["selected_endpoint"]["tag"]],
                "require_parameters": True,
                "allow_fallbacks": False,
            },
            "Role model or route mismatch",
        )
        if end["status"] == "response":
            require(
                raw["model"] in {m["id"], m.get("canonical_slug")}
                and raw["provider"] == m["selected_endpoint"]["provider_name"],
                "Provider generation identity mismatch",
            )
            ids.append(raw["id"])
        if reserved and reserved["request_id"] == rid:
            require(
                request_reservation_usd(wire, m) == Decimal(reserved["usd"])
                and raw["error"]["code"] == reserved["code"]
                and end["cost_status"] == "unknown",
                "Unpriced reservation differs from request",
            )
        bill = bills[actor]
        bill["requests"] += 1
        bill["http_calls"] += end["delivery_status"] != "not_submitted"
        bill["total_tokens"] += (end.get("usage") or {}).get("total_tokens") or 0
        bill["unknown_count"] += end["cost_status"] == "unknown"
        if end["cost_status"] == "observed":
            bill["reported_usd"] += Decimal(str(end["cost_usd"]))
        records.append((actor, wire, raw, end["status"]))
    for bill in bills.values():
        bill["reported_usd"] = str(bill["reported_usd"])
    require(
        dict(bills) == res["billing_by_role"],
        "Billing ledger differs by role or token count",
    )
    require(
        digest((folder / "log.jsonl").read_bytes()) == res["log_sha256"],
        "Journal hash mismatch",
    )
    entry = dict(
        session=res["session_id"],
        journal=str((folder / "log.jsonl").relative_to(workspace)),
        journal_sha256=res["log_sha256"],
        costs=dict(bills),
        responses=len(ids),
        unpriced=reserved,
        error=res.get("error_code"),
        input_spans=audit["normal_response_memory_spans"],
    )
    return entry, ids, state, scan, blobs, records


def audit_task(args):
    workspace, cfg, base, row, task_data, reg, completion = args
    modules_for(workspace)
    from tau2.data_model.tasks import Task

    task = Task.model_validate(task_data)
    tid = row["task"]
    root = workspace / "runs" / cfg["name"]
    parent = root / ("task-" + tid)
    source = recorded_source(parent / "source", tid)
    banks = {}
    cells = []
    ids = []
    first_writer_wires = set()
    for stage in cfg["stages"]:
        p = parent / stage
        end, ctl, rows = sealed_rows(p)
        design, jobs = full_design(
            cfg, base, row, stage, None if stage == "source" else source
        )
        require(
            ctl["implementation"] == reg["implementation"]
            and ctl["design"] == design
            and len(rows) == len(jobs)
            and ctl["profile"]["config_sha256"] == reg["preflight"]["protocol_sha256"],
            "Frozen task settings differ",
        )
        grant = Decimal(str(ctl["profile"]["guards"]["max_usd"]))
        require(
            accounting(p, grant) == completion["stages"][str(p.relative_to(root))],
            "Stage seal or cost mismatch",
        )
        models = ctl["model_catalog_entries"]
        require(
            endpoint_terms(models)
            == {k: v for k, v in reg["endpoint_terms"].items() if k != "grader"},
            "Captured worker endpoint terms differ",
        )
        for slot, job in zip(rows, jobs, strict=True):
            require(
                all(slot[k] == v for k, v in job.items())
                and slot["directory"] == f"jobs/{slot['index']:04d}",
                "Allocation differs from registered order",
            )
            folder = safe_path(p, slot["directory"])
            arm = slot["arm"]
            run = slot["run_id"]
            cell = dict(
                task=tid,
                customer=row["customer"],
                stage=stage,
                arm=arm,
                run=run,
                status=slot["status"],
                official=None,
            )
            cells.append(cell)
            if slot["status"] == "not_started":
                require(
                    "result" not in slot and not folder.exists(),
                    "Unstarted cell has an execution",
                )
                cell["reason"] = slot.get("reason")
                continue
            res = slot["result"]
            require(
                run == res["run_id"] and slot["status"] == res["status"],
                "Run result identity differs",
            )
            scan, state, _ = read_experiment(folder)
            blobs = BlobStore(folder)
            judge_model = None
            if stage != "write":
                assignment = state["assignments"][run]
                g = assignment["native_grade_limits"]
                judge_model = blobs.get(g["model_ref"])
                judge_cfg = blobs.get(g["config_ref"])
                require(
                    endpoint_terms({"grader": judge_model})["grader"]
                    == reg["endpoint_terms"]["grader"]
                    and judge_cfg
                    == dict(cfg["judge"], timeout_seconds=cfg["http_timeout_seconds"]),
                    "Judge configuration mismatch",
                )
            evidence, gen, state, scan, blobs, records = read_request_data(
                folder, run, res, workspace, models, judge_model
            )
            ids.extend(gen)
            cell.update(evidence)
            if stage == "write":
                writing = state["memory_writes"][run]
                capsule = blobs.get(writing["experience_ref"])
                read_form_experience(blobs, scan, capsule)
                imp = blobs.get(capsule["source_ref"])
                instruction = (
                    asset_text(
                        ROOT, design["writing"]["common_instruction_ref"]
                    ).rstrip("\r\n")
                    + "\n"
                    + design["writing"]["lines"][arm]
                )
                require(
                    source
                    and imp["source_run_id"] == source.source_run_id
                    and imp["source_log_sha256"] == source.source_log_sha256
                    and imp["public_context_sha256"] == source.public_context_sha256
                    and writing["instruction"] == instruction
                    and writing["feedback"]["level"] == "none",
                    "Misbound historical writer experience",
                )
                msgs = [
                    dict(role="system", content=instruction),
                    dict(role="user", content=source.public_context),
                    dict(
                        role="user",
                        content=canonical(
                            dict(kind="evaluation_feedback", level="none")
                        ).decode(),
                    ),
                ]
                for i, (actor, wire, _, _) in enumerate(records):
                    require(
                        actor == "writer"
                        and wire["messages"][:3] == msgs
                        and wire["seed"] == cfg["writer_seed"]
                        and [t["function"]["name"] for t in wire["tools"]]
                        == ["memory_store"],
                        "Unexpected writer input",
                    )
                    if i == 0:
                        require(
                            wire["messages"] == msgs,
                            "Unexpected initial writer messages",
                        )
                        first_writer_wires.add(
                            digest(canonical(dict(wire, messages=msgs[1:])))
                        )
                cell.update(
                    source_run=source.source_run_id,
                    public_context_sha256=source.public_context_sha256,
                )
                if slot["status"] in {"completed", "empty"}:
                    b = read_form_bank(folder, expected_log_sha256=res["log_sha256"])
                    banks[arm] = b
                    cell.update(
                        snapshot=b["snapshot_id"],
                        notes=[
                            {k: i[k] for k in ("title", "body")} for i in b["items"]
                        ],
                    )
                continue
            bank = banks.get(arm)
            policy = MemoryPolicy(**assignment["policy"])
            start = scan["starts"][run]
            native = blobs.get(assignment["native_config_ref"])
            seed = cfg["source_seed"] if stage == "source" else cfg["solver_seeds"][0]
            items = (
                snapshot_items(blobs, state, start["payload"]["start_snapshot_id"])
                if policy.enabled
                else []
            )
            require(
                start["payload"]["fresh_session"]
                and start["payload"]["source_run_ids"]
                == ([bank["run_id"]] if bank else [])
                and items == (bank["items"] if bank else [])
                and (arm not in cfg["controls"]) == (bank is not None)
                and policy.read_mode == ("none" if arm == "no_memory" else "preload")
                and assignment["source_writer_snapshot_id"]
                == (bank["snapshot_id"] if bank else None)
                and assignment["public_context_sha256"]
                == (source.public_context_sha256 if bank else None)
                and native["seed"] == seed
                and native["domain"] == "retail"
                and native["task_split_name"] == "test"
                and native["task_ids"] == [tid],
                "Wrong seed, customer, memory or exposure assignment",
            )
            system = blobs.get(start["payload"]["task_ref"])["content"]
            instruction = blobs.get(assignment["instruction_ref"])
            if policy.enabled:
                require(
                    digest(instruction.encode())
                    == design["second_pass"]["solve_instruction_ref"]["sha256"],
                    "Solve instruction differs",
                )
                system += memory_system_suffix(
                    policy, past_memory_instruction(instruction)
                )
            grader = []
            for actor, wire, raw, response_status in records:
                if actor == "worker":
                    require(
                        wire["seed"] == seed
                        and wire["messages"][0] == dict(role="system", content=system)
                        and (
                            wire["messages"][1] == snapshot_message(items)
                            if policy.enabled
                            else snapshot_message([]) not in wire["messages"]
                        )
                        and not any(
                            t["function"]["name"].startswith("memory_")
                            for t in wire.get("tools", [])
                        ),
                        "Unexpected solver input",
                    )
                elif actor == "user_simulator":
                    require(
                        "seed" not in wire
                        and snapshot_message(items) not in wire["messages"],
                        "Simulator received treatment or a seed",
                    )
                else:
                    require(actor == "grader", "Unexpected actor")
                    grader.append((wire, raw))
            cell.update(
                grading=res["grading_status"],
                writer_run=bank["run_id"] if bank else None,
                snapshot=bank["snapshot_id"] if bank else None,
                termination=res["termination_reason"],
                steps=res["native_steps"],
                native_reward=res["native_reward"],
                official=res["official_success"],
            )
            if res["grading_status"] == "graded":
                report = blobs.get(res["report_ref"])
                before = blobs.get(res["before_grade_ref"])
                cell["grade"] = verify_grade(
                    task,
                    report,
                    before,
                    res["official_score"],
                    grader,
                    judge_cfg,
                    judge_model,
                    native,
                )
                require(
                    (report["reward_info"]["reward"] == 1) is res["official_success"],
                    "Recorded official success mismatches score",
                )
                cell["report"] = str(
                    (folder / res["report_ref"]["path"]).relative_to(workspace)
                )
                cell["report_sha256"] = res["report_ref"]["sha256"]
                cell["behavior"] = inspect_episode(
                    report, task_data["evaluation_criteria"]
                )
            else:
                require(res["official_success"] is None, "Ungraded episode has a score")
    require(
        len(first_writer_wires) <= 1,
        "The writer treatments differed beyond their assigned instruction",
    )
    return dict(task=tid, cells=cells, generation_ids=ids)


def blocked_audit(args):
    with ExitStack() as blocked:
        mocks = [
            blocked.enter_context(
                patch(
                    name,
                    side_effect=AssertionError(
                        "Unexpected network or new judge call during replay"
                    ),
                )
            )
            for name in ("socket.socket.connect", "socket.socket.connect_ex")
        ]
        result = audit_task(args)
        require(
            not any(m.call_count for m in mocks),
            "Offline replay used a network transport",
        )
        return result


def compare(cells, rows, cfg):
    index = {(c["task"], c["stage"], c["arm"]): c for c in cells}
    forms = [f + "_original" for f in cfg["forms"]]
    arms = forms + cfg["controls"]
    result = {}
    paired = []
    groups = {
        "all": rows,
        "previous_task": [r for r in rows if r["prior_task"]],
        "new_task": [r for r in rows if not r["prior_task"]],
        "previous_customer": [r for r in rows if r["prior_customer"]],
        "new_customer": [r for r in rows if not r["prior_customer"]],
        "active_nl": [r for r in rows if r["assertions"]],
        "no_active_nl": [r for r in rows if not r["assertions"]],
    }
    for row in rows:
        families = {op.split("_", 1)[0] for op in row["operations"]}
        family = (
            "mixed" if len(families) > 1 else next(iter(families), "no_reference_write")
        )
        groups.setdefault("workflow/" + family, []).append(row)
    for group, rs in groups.items():
        table = {}
        result[group] = dict(
            tasks=len(rs), customers=len({r["customer"] for r in rs}), forms=table
        )
        for arm in arms:
            outcome = Counter()
            first = Counter()
            controls = {c: Counter() for c in cfg["controls"]}
            clusters = {c: defaultdict(list) for c in cfg["controls"]}
            by_source = {c: defaultdict(Counter) for c in cfg["controls"]}
            both_controls = defaultdict(Counter)
            for r in rs:
                t = r["task"]
                s = index[t, "source", "no_memory"]
                a = index[t, "solve1", arm]
                o = a["official"]
                outcome["ungraded" if o is None else "success" if o else "failure"] += 1
                first[status(o, s["official"])] += 1
                source_class = {
                    True: "first_success",
                    False: "first_failure",
                    None: "first_missing",
                }[s["official"]]
                left, right = (
                    index[t, "solve1", c]["official"] for c in cfg["controls"]
                )
                if None in (left, right, o):
                    agreement = "missing"
                elif left == right:
                    agreement = status(o, left)
                else:
                    agreement = "split_controls"
                both_controls[source_class][agreement] += 1
                for c in cfg["controls"]:
                    control = index[t, "solve1", c]
                    state = status(o, control["official"])
                    controls[c][state] += 1
                    by_source[c][source_class][state] += 1
                    if state != "missing":
                        clusters[c][r["customer"]].append(
                            int(o) - int(control["official"])
                        )
                    if group == "all" and arm in forms:
                        paired.append(
                            dict(
                                task=t,
                                arm=arm,
                                control=c,
                                source=s["run"],
                                source_success=s["official"],
                                memory_run=a["run"],
                                control_run=control["run"],
                                state=state,
                                source_transition=status(o, s["official"]),
                            )
                        )
            table[arm] = dict(
                official=dict(outcome),
                vs_first=dict(first),
                vs_controls={c: dict(v) for c, v in controls.items()},
                vs_controls_by_first={
                    c: {k: dict(v) for k, v in states.items()}
                    for c, states in by_source.items()
                },
                both_controls_by_first={k: dict(v) for k, v in both_controls.items()},
                customer_equal_weight_difference={
                    c: sum(sum(x) / len(x) for x in values.values()) / len(values)
                    if values
                    else None
                    for c, values in clusters.items()
                },
                task_equal_weight_difference={
                    c: sum(sum(x) for x in values.values())
                    / sum(map(len, values.values()))
                    if values
                    else None
                    for c, values in clusters.items()
                },
            )
    return result, paired


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    cfg, base, _, rows, tasks = prepare(workspace)
    root = workspace / "runs" / cfg["name"]
    reg = read(root / "registration.json")
    end = read(root / "completion.json")
    require(
        not args.output.exists() and not args.summary.exists(),
        "Use new analysis destinations",
    )
    require(
        reg["preflight"] == preflight(cfg, base, rows)
        and end["registration_sha256"]
        == digest((root / "registration.json").read_bytes()),
        "Bad study seal",
    )
    total = Decimal(0)
    for stage in cfg["stages"]:
        ordered = sorted(
            rows, key=lambda r: digest(canonical([cfg["name"], stage, r["task"]]))
        )
        for n in range(0, len(rows), cfg["max_concurrent"]):
            batch = ordered[n : n + cfg["max_concurrent"]]
            wave = read(root / f"{stage}-wave-{n // cfg['max_concurrent'] + 1}.json")
            grant = Decimal(wave["per_shard_grant"])
            require(
                wave["tasks"] == [r["task"] for r in batch]
                and Decimal(wave["prior_covered"]) == total
                and total + grant * len(batch) <= Decimal(str(cfg["total_usd_limit"])),
                "Invalid concurrent reservation",
            )
            for row in batch:
                name = "task-" + row["task"] + "/" + stage
                _, ctl, _ = sealed_rows(root / name)
                require(
                    Decimal(str(ctl["profile"]["guards"]["max_usd"])) == grant,
                    "Shard grant differs from schedule",
                )
                total += Decimal(end["stages"][name]["spent"])
    require(
        str(total) == end["total_covered_usd"]
        and len(end["stages"]) == len(rows) * len(cfg["stages"]),
        "Invalid study accounting",
    )
    params = [(workspace, cfg, base, r, tasks[r["task"]], reg, end) for r in rows]
    with ProcessPoolExecutor(max_workers=5, mp_context=get_context("spawn")) as pool:
        cases = list(pool.map(blocked_audit, params))
    cells = [c for case in cases for c in case["cells"]]
    ids = [i for case in cases for i in case["generation_ids"]]
    sessions = [c["session"] for c in cells if c.get("session")]
    require(
        len(ids) == len(set(ids))
        and len(sessions) == len(set(sessions))
        and len({c["run"] for c in cells}) == 440
        and len(cells) == 440,
        "Reused run, session or generation",
    )
    groups, pairs = compare(cells, rows, cfg)
    by_role = defaultdict(lambda: dict(http=0, tokens=0, unknown=0, usd=Decimal(0)))
    for c in cells:
        for role, b in c.get("costs", {}).items():
            a = by_role[role]
            a["http"] += b["http_calls"]
            a["tokens"] += b["total_tokens"]
            a["unknown"] += b["unknown_count"]
            a["usd"] += Decimal(b["reported_usd"])
    money = sum(x["usd"] for x in by_role.values())
    require(
        money == sum(Decimal(b["reported"]) for b in end["stages"].values()),
        "Role and stage billing disagree",
    )
    for v in by_role.values():
        v["usd"] = str(v["usd"])
    raw = dict(
        study=cfg["name"],
        registration_sha256=end["registration_sha256"],
        completion_sha256=digest((root / "completion.json").read_bytes()),
        rows=rows,
        cells=cells,
        pairs=pairs,
        generation_ids=ids,
    )
    write_json(args.output, raw)
    stage_costs = defaultdict(Decimal)
    condition_costs = defaultdict(Decimal)
    for cell in cells:
        cost = sum(
            (Decimal(b["reported_usd"]) for b in cell.get("costs", {}).values()),
            Decimal(0),
        )
        stage_costs[cell["stage"]] += cost
        if cell["stage"] != "source":
            condition_costs[cell["arm"]] += cost
    summary = dict(
        stage_usd={k: str(v) for k, v in stage_costs.items()},
        writing_and_replay_usd={k: str(v) for k, v in condition_costs.items()},
        source_failure_customers=len(
            {
                c["customer"]
                for c in cells
                if c["stage"] == "source" and c["official"] is False
            }
        ),
        study=cfg["name"],
        detail_sha256=digest(args.output.read_bytes()),
        implementation=reg["implementation"]["implementation_commit"],
        protocol_sha256=reg["preflight"]["protocol_sha256"],
        source=Counter(
            "ungraded"
            if c["official"] is None
            else "success"
            if c["official"]
            else "failure"
            for c in cells
            if c["stage"] == "source"
        ),
        writers={
            f: dict(
                Counter(
                    c["status"]
                    for c in cells
                    if c["arm"] == f + "_original" and c["stage"] == "write"
                )
            )
            for f in cfg["forms"]
        },
        allocation_states={
            s: dict(Counter(c["status"] for c in cells if c["stage"] == s))
            for s in cfg["stages"]
        },
        sessions=len(sessions),
        generations=len(ids),
        reported_usd=str(money),
        held_usd=str(total - money),
        http=sum(x["http"] for x in by_role.values()),
        by_role=dict(by_role),
        regraded=sum("grade" in c for c in cells),
        replayed_judge_responses=sum(
            c.get("grade", {}).get("replayed_judge_answers", 0) for c in cells
        ),
        new_grader_calls=0,
        groups=groups,
    )
    write_json(args.summary, summary)
    print(
        "strict native regrades",
        summary["regraded"],
        "HTTP",
        summary["http"],
        "USD",
        summary["reported_usd"],
        flush=True,
    )


if __name__ == "__main__":
    main()
