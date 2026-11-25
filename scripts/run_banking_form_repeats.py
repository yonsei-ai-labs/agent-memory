"""Run registered banking form comparisons with repeated readers and a public source evidence gate."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.adapters.banking_dispute_evidence import dispute_targets, public_opportunity
from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from scripts import run_banking_memory as bank
from scripts.run_recheck_rounds import fixed

PROFILE = "protocols/pilots/tau-banking-multitarget-forms-v1.json"
PREFLIGHT = "artifacts/pilots/20261125-banking-multitarget-forms-preflight.json"
APPROVAL = "artifacts/pilots/20261125-banking-multitarget-forms-authorization.json"
matched = bank.matched


def prepare(workspace):
    spec = matched.read(ROOT / PROFILE)
    _, base, conn, _, _, original = bank.prepare(workspace)
    if (tuple(spec["forms"]) != matched.FORMS or spec["delivery"] != "preload" or
            spec["feedback"] != "result_only" or spec["repetitions"] < 2):
        raise ContractError("Unsupported form comparison")
    cfg = bank.required_design(base, matched.read(ROOT / bank.REQUIRED_PROFILE))
    waves = ["solve"] + [f"solve-repeat-{i}" for i in range(2, spec["repetitions"] + 1)]
    cfg.update(name=spec["name"], stages=["source", "write", *waves], stage_kinds={s: "solve" for s in waves},
               writer_feedback_level=spec["feedback"], record_unfunded_shards=True)
    roles = ("worker", "user_simulator")
    cfg["expected_endpoint_terms"] = {r: cfg["endpoint_terms"][r] for r in roles}
    catalog = json.loads(matched.asset_text(ROOT, cfg["endpoints"]))["models"]
    cfg["expected_model_revisions"] = {r: catalog[r]["canonical_slug"] for r in roles}
    from tau2.domains.banking_knowledge.environment import get_tasks

    population = {t.id: t for t in get_tasks()}
    cases, tasks, schedule = [], {}, {}
    for tid, required_count in spec["tasks"].items():
        native = population[tid]
        if bank.stratum(native) != "discoverable_db":
            raise ContractError("Expected a native DB-only discoverable banking task")
        case, task = bank.native_case(cfg, conn, matched.modules_for(workspace), native, "multiple_disputes")
        owner, ids = dispute_targets(task)
        if len(ids) != required_count:
            raise ContractError("Native dispute task changed")
        case.update(customer=digest(owner.encode()), reader_modes=[spec["delivery"]], scheduled_stages=cfg["stages"])
        schedule[tid] = {stage: matched.jobs_for(case, cfg["stage_kinds"].get(stage, stage)) for stage in cfg["stages"]}
        cases.append(case)
        tasks[tid] = task
    assigned = {stage: sum(len(s[wave]) for s in schedule.values() for wave in cfg["stages"]
                           if cfg["stage_kinds"].get(wave, wave) == stage) for stage in matched.STAGES}
    assigned["second_pass_no_memory"] = 0
    if len({case["customer"] for case in cases}) != len(cases) or assigned != spec["assigned"]:
        raise ContractError("Customer independence or allocation counts changed")
    record = dict(series=spec["name"], protocol_sha256=digest((ROOT / PROFILE).read_bytes()),
                  banking_base_sha256=original["protocol_sha256"], required_profile_sha256=digest((ROOT / bank.REQUIRED_PROFILE).read_bytes()),
                  revision=original["native_revision"], corpus_sha256=original["corpus_sha256"], assigned=assigned,
                  cases=cases, schedule_sha256=digest(canonical(schedule)), expected_models=cfg["expected_model_revisions"])
    return spec, cfg, conn, cases, tasks, record


def admission(root, stage, case, task):
    if not stage.startswith("solve"):
        return None
    directory = root / ("task-" + case["task"])
    source = matched.source_from(directory / "source", case["task_id"])
    receipt = public_opportunity(task, source)
    fixed(directory / "reader-admission.json", receipt)
    return None if receipt["eligible"] else receipt["reason"]


def resolve_models(cfg, conn):
    models, judge = matched.study_models(cfg, conn)
    if {r: m["canonical_slug"] for r, m in models.items()} != cfg["expected_model_revisions"]:
        raise ContractError("Resolved banking model revisions changed")
    return models, judge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run"))
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    spec, cfg, conn, cases, tasks, record = prepare(workspace)
    if args.mode == "preflight":
        fixed(ROOT / PREFLIGHT, record)
        return
    approval = matched.read(ROOT / APPROVAL)
    expected = dict(series=spec["name"], protocol_sha256=record["protocol_sha256"],
                    preflight_sha256=digest(canonical(record)), assigned=record["assigned"], total_usd=spec["maximum_usd"])
    if (record != matched.read(ROOT / PREFLIGHT) or not approval.get("user_authorization")
            or any(approval.get(k) != v for k, v in expected.items())):
        raise ContractError("Published registration or study-specific authorization does not match")
    record["authorization"] = approval
    cfg.update(total_usd_limit=approval["total_usd"], registration_sha256=digest(canonical(record)))
    matched.run_study(workspace, cfg, {}, conn, cases, tasks, record, stage_runner=matched.stage_job,
                      model_resolver=resolve_models, stage_admission=admission)


if __name__ == "__main__":
    main()
