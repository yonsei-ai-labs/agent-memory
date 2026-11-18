"""Audit both frozen stages, independently regrade native episodes, and export the declared behavior comparisons."""

import argparse
from collections import Counter
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_memory_eval.analysis.forms_results import checked_rows
from agent_memory_eval.analysis.fresh_behavior import (
    both_controls,
    field_rates,
    inspect_mutations,
    tabulate_groups,
)
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.experiments.fresh_forms import prepare_study
from agent_memory_eval.experiments.forms_write import read_form_experience
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.models.openrouter import request_reservation_usd
from agent_memory_eval.recording.journal import safe_path
from regrade_two_pass_forms import regrade


def accounting(row, scan, blobs, models):
    events = [e for e in scan["events"].values() if e["run_id"] == row["run_id"]]
    starts = {e["payload"]["request_id"]: e["payload"] for e in events if e["type"] == "model.request_started"}
    unknown = [
        e["payload"]
        for e in events
        if e["type"] == "model.request_finished" and e["payload"]["cost_status"] == "unknown"
    ]
    receipt = row["result"].get("unpriced_reservation")
    if receipt:
        if len(unknown) != 1 or unknown[0]["request_id"] != receipt["request_id"]:
            raise ContractError("Cost reserve is not tied to exactly one unpriced request")
        if unknown[0]["delivery_status"] != "response_received" or unknown[0]["status"] != "error":
            raise ContractError("Reserved HTTP error was not received")
        req = starts[receipt["request_id"]]
        role = req["actor_role"]
        reserve = request_reservation_usd(
            blobs.get(req["request_ref"]), models["worker" if role == "writer" else role]
        )
        error = blobs.get(unknown[0]["response_ref"])["error"]
        if receipt != dict(
            request_id=req["request_id"],
            code=error["code"],
            role=role,
            usd=str(reserve),
            request_ref=req["request_ref"],
            response_ref=unknown[0]["response_ref"],
        ):
            raise ContractError("Reservation differs from the wire, response, or endpoint")
    return unknown, receipt


def inspect_stage(run, design, plan, cfg, checkout, workspace):
    ctl = read_json((run / "control.json").read_bytes())
    if ctl["design"] != design:
        raise ContractError("Actual allocation differs from the preregistered selection and sealed first pass")
    cells, writers, reserves = [], [], []

    def observe(row, folder, scan, state, blobs):
        res, run_id = row["result"], row["run_id"]
        _, receipt = accounting(row, scan, blobs, ctl["model_catalog_entries"])
        if receipt:
            if receipt["code"] not in cfg["unpriced_http"]["codes"]:
                raise ContractError("Unpriced code is outside the study rule")
            reserves.append(dict(job=row["index"], **receipt))
        if row["phase"] == "write":
            writer = state["memory_writes"][run_id]
            capsule = blobs.get(writer["experience_ref"])
            read_form_experience(blobs, scan, capsule)
            proof = blobs.get(capsule["source_ref"])
            src = plan["sources"][row["task_id"]]
            ctx = blobs.get(proof["public_context_ref"])
            if (
                canonical(ctx) != src.public_context.encode()
                or proof["source_run_id"] != src.source_run_id
                or proof["source_log_sha256"] != src.source_log_sha256
                or proof["source_end_event_id"] != src.source_end_event_id
            ):
                raise ContractError("Writer's imported public experience differs from the source journal")
            notes = snapshot_items(blobs, state, res["snapshot_id"]) if res.get("snapshot_id") else []
            writers.append(
                dict(
                    job=row["index"],
                    run_id=run_id,
                    task_id=row["task_id"],
                    repetition=row["repetition"],
                    arm=row["arm"],
                    status=row["status"],
                    source_run_id=src.source_run_id,
                    original_source_sha256=src.public_context_sha256,
                    snapshot_id=res.get("snapshot_id"),
                    notes=[dict(title=i["title"], body=i["body"]) for i in notes],
                )
            )
            return
        info = state["assignments"][run_id]
        r = dict(
            index=row["index"],
            run_id=run_id,
            task_id=row["task_id"],
            repetition=row["repetition"],
            arm=row["arm"],
            status=row["status"],
            official_success=res["official_success"],
            error_code=res.get("error_code"),
            log_sha256=res["log_sha256"],
            writer_run_id=info["source_writer_run_id"],
            items=(info["bank_sizes"] or {}).get("items", 0),
            normal_input_spans=res["normal_response_memory_spans"],
            mutations=None,
        )
        if res.get("report_ref"):
            report = blobs.get(res["report_ref"])
            if (
                report["reward_info"]["communicate_checks"]
                or res["official_success"] is not report["reward_info"]["db_check"]["db_match"]
            ):
                raise ContractError("Outcome in this cohort must agree with the final DB")
            r.update(
                report_sha256=res["report_ref"]["sha256"],
                db_hash=res["final_db_hash"],
                mutations=inspect_mutations(
                    report["messages"], plan["tasks"][row["task_id"]]["evaluation_criteria"], cfg["write_tools"]
                ),
            )
        cells.append(r)

    _, seal, rows, usage = checked_rows(run, observe_job=observe)
    for r in rows:
        if r["phase"] == "solve" and r["status"] == "not_started":
            cells.append(
                dict(
                    index=r["index"],
                    run_id=r["run_id"],
                    task_id=r["task_id"],
                    repetition=r["repetition"],
                    arm=r["arm"],
                    official_success=None,
                    status=r["status"],
                    reason=r["reason"],
                    mutations=None,
                )
            )
    reserved = sum((Decimal(r["usd"]) for r in reserves), Decimal(0))
    known = sum((Decimal(c["reported_usd"]) for c in usage.values()), Decimal(0))
    if (
        reserved != Decimal(seal["reserved_usd"])
        or len(reserves) > cfg["unpriced_http"]["maximum_per_stage"]
        or known + reserved > Decimal(str(ctl["profile"]["guards"]["max_usd"]))
    ):
        raise ContractError("Stage accounting does not reconcile")
    replay = regrade(run, workspace, checkout)
    if (
        replay["official_results_checked"] != sum(type(r["official_success"]) is bool for r in cells)
        or replay["new_model_or_network_attempts"]
    ):
        raise ContractError("Independent native grades differ")
    return dict(
        name=run.name,
        implementation_commit=ctl["implementation"]["implementation_commit"],
        config_sha256=ctl["profile"]["config_sha256"],
        completion_sha256=digest((run / "completion.json").read_bytes()),
        allocation_sha256=seal["allocation_sha256"],
        control_sha256=seal["control_sha256"],
        stop_reason=seal["stop_reason"],
        usage_by_role=usage,
        http_calls=seal["http_calls"],
        known_usd=str(known),
        reserved_usd=str(reserved),
        reservations=reserves,
        unpriced_requests=sum(v["unknown_count"] for v in usage.values()),
        cells=sorted(cells, key=lambda r: r["index"]),
        writers=writers,
        regrade=replay,
    )


def analyze(workspace, root, config_path, report, *, continuation=False):
    raw = config_path.read_bytes()
    cfg = read_json(raw)
    checkout = workspace / "data/tau2-bench"
    stages = {}
    for stage in ("source", "comparison"):
        d, plan = prepare_study(root, workspace, checkout, cfg, digest(raw), stage)
        run = safe_path(workspace, "runs/" + cfg[stage + "_name"])
        stage_cfg = cfg
        if stage == "comparison" and continuation:
            from agent_memory_eval.experiments.fresh_continuation import AMENDMENT, verify_continuation
            amendment = read_json((root / AMENDMENT).read_bytes())
            run = safe_path(workspace, "runs/" + amendment["destination"])
            maximum = verify_continuation(root, workspace, run)
            stage_cfg = dict(cfg, unpriced_http=dict(cfg["unpriced_http"], maximum_per_stage=maximum))
        inspected = inspect_stage(run, d, plan, stage_cfg, checkout, workspace)
        if stage == "comparison" and continuation:
            inspected["continuation"] = read_json((run / "control.json").read_bytes())["continuation"]
        if inspected["config_sha256"] != digest(raw):
            raise ContractError("Stage configuration hash differs from original")
        stages[stage] = inspected
    src, second = stages["source"], stages["comparison"]
    linked = read_json((report / "summary.json").read_bytes())
    if (
        linked["completion_sha256"] != second["completion_sha256"]
        or d["source"]["parent_completion"]["sha256"] != src["completion_sha256"]
    ):
        raise ContractError("Linked report or source completion differs")
    arms = [*cfg["forms"], *cfg["controls"]]
    groups = {"all": cfg["tasks"], **d["task_sets"]}
    cells = second["cells"]
    for r in cells:
        observed = linked["retrieval_by_job"].get(str(r["index"]))
        if observed:
            if (
                observed["list_calls"]
                or observed["get_calls"]
                or (observed["body_spans"] > 0) != (r["normal_input_spans"] > 0)
            ):
                raise ContractError("Unexpected retrieval or body exposure")
            r["body_exposed"] = observed["body_spans"] > 0

    def compact(c):
        return {
            k: c[k]
            for k in (
                "treatment",
                "control",
                "planned_pairs",
                "jointly_graded_pairs",
                "counts",
                "task_equal_difference_pp",
            )
        }

    spends = {k: sum((Decimal(s[k]) for s in stages.values()), Decimal(0)) for k in ("known_usd", "reserved_usd")}
    result = dict(
        format="agent-memory-new-customer-four-forms-results/v1",
        config_sha256=digest(raw),
        benchmark_revision=d["source"]["benchmark_revision"],
        analysis_sha256=digest(Path(__file__).read_bytes()),
        behavior_method_sha256=digest((root / "src/agent_memory_eval/analysis/fresh_behavior.py").read_bytes()),
        linked_summary_sha256=digest((report / "summary.json").read_bytes()),
        task_sets=groups,
        first_outcomes={r["task_id"]: r["official_success"] for r in src["cells"]},
        outcomes=tabulate_groups(cells, groups, arms),
        paired={s: [compact(c) for c in values] for s, values in linked["contrasts"].items()},
        paired_with_both_controls=both_controls(cells, cfg["forms"]),
        writer_status={
            arm: dict(Counter(w["status"] for w in second["writers"] if w["arm"] == arm)) for arm in cfg["forms"]
        },
        body_exposure={a: sum(bool(r.get("body_exposed")) for r in cells if r["arm"] == a) for a in arms},
        field_rates={k: field_rates([r for r in cells if r["task_id"] in tasks]) for k, tasks in groups.items()},
        known_usd=str(spends["known_usd"]),
        reserved_usd=str(spends["reserved_usd"]),
        http_calls=src["http_calls"] + second["http_calls"],
        unpriced_requests=src["unpriced_requests"] + second["unpriced_requests"],
    )
    behavior = {}
    for name, s in stages.items():
        behavior[name] = dict(cells=s.pop("cells"), writers=s.pop("writers"))
    return {
        "results.json": dict(
            result, stages={n: {k: v for k, v in s.items() if k != "regrade"} for n, s in stages.items()}
        ),
        "behavior.json": behavior,
        "native-regrade.json": {n: s["regrade"] for n, s in stages.items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--continuation", action="store_true", help="Verify the original prefix and include the untouched-slot continuation")
    parser.add_argument("--config", type=Path, default=ROOT / "protocols/pilots/tau2-new-customer-forms-v1.json")
    a = parser.parse_args()
    target = a.output.resolve()
    if target.exists() or target.is_relative_to((a.workspace / "runs").resolve()):
        raise ContractError("Use a new output outside the sealed runs")
    data = analyze(a.workspace.resolve(), ROOT, a.config, a.report.resolve(), continuation=a.continuation)
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=target.parent) as tmp:
        for n, v in data.items():
            write_json(Path(tmp) / n, v)
        Path(tmp).rename(target)
    print(target, data["results.json"]["known_usd"])


if __name__ == "__main__":
    main()
