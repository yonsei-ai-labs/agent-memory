"""Run the pinned English τ-Knowledge pilot with the matched-trial writer and readers."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.adapters.tau2 import PIN
from agent_memory_eval.contracts.connection import validate_profile
from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import solve_connection
from agent_memory_eval.experiments.matched_trial import native_control, opening_hash, role_settings
from agent_memory_eval.runner.task_start import FORMAT as START_FORMAT
from agent_memory_eval.runner.tau2_baseline import build_baseline_orchestrator, native_config
from scripts import run_fresh_matched as matched


PROFILE = "protocols/pilots/tau-banking-memory-v1.json"
PREFLIGHT = "artifacts/pilots/20261124-banking-memory-preflight.json"
APPROVAL = "artifacts/pilots/20261124-banking-memory-authorization.json"
REQUIRED_PROFILE = "protocols/pilots/tau-banking-required-v1.json"
REQUIRED_PREFLIGHT = "artifacts/pilots/20261124-banking-required-preflight.json"
REQUIRED_APPROVAL = "artifacts/pilots/20261124-banking-required-authorization.json"


def stratum(task):
    criteria = task.evaluation_criteria
    if (task.initial_state is not None or len(task.required_documents or []) < 2
            or not criteria or criteria.nl_assertions or not criteria.actions):
        return None
    if set(criteria.reward_basis) == {"ACTION"}:
        return "required_action"
    if set(criteria.reward_basis) != {"DB"}:
        return None
    if any("discoverable" in a.name for a in criteria.actions):
        return "discoverable_db"
    return "single_db" if len(criteria.actions) == 1 else "multiple_db"


def prepare(workspace):
    spec = matched.read(ROOT / PROFILE)
    cfg = json.loads(matched.asset_text(ROOT, spec["base"]))
    if (spec["revision"] != cfg["benchmark_revision"] or spec["revision"] != PIN
            or spec["task_start_mode"] != START_FORMAT or spec["retrieval_config"] != "grep_only"):
        raise ContractError("The registered banking setup differs from its pinned implementation")
    cfg.update(name=spec["name"], task_start_mode=START_FORMAT, max_concurrent=spec["max_concurrent"])
    cfg["limits"]["max_input_bytes"] = spec["max_input_bytes"]
    conn = json.loads(matched.asset_text(ROOT, cfg["connection"]))
    validate_profile(conn)
    domain = spec["domain"]
    conn["benchmark"].update(domain=domain, split=None, retrieval_config=spec["retrieval_config"])
    modules = matched.modules_for(workspace)
    from tau2.domains.banking_knowledge.environment import get_knowledge_base, get_tasks
    from tau2.multilingual.runtime import load_run_language
    from tau2.runner.helpers import load_task_splits, select_run_tasks

    population = sorted(get_tasks(), key=lambda task: task.id)
    selected = {}
    for task in population:
        group = stratum(task)
        if group:
            selected.setdefault(group, task.id)
    if selected != spec["tasks"] or load_task_splits(domain) is not None:
        raise ContractError("Official population, splits or prespecified task selection changed")
    knowledge = get_knowledge_base()
    corpus_sha = digest(canonical(knowledge.model_dump(mode="json")))
    cases, tasks = [], {}
    for group, tid in spec["tasks"].items():
        case = dict(task=tid, task_id=tid, domain=domain, split=None, customer="source-" + tid,
                    stratum=group, task_start_mode=START_FORMAT, scheduled_stages=list(matched.STAGES))
        conf = solve_connection(conn, matched.design_for(cfg, case, "source"),
                                matched.jobs_for(case, "source")[0], cfg["episode_cap_usd"])
        native = native_config(conf)
        task, = select_run_tasks(native, load_run_language(native))
        registered = next(t for t in population if t.id == tid)
        if task.evaluation_criteria != registered.evaluation_criteria or stratum(task) != group:
            raise ContractError("Runtime task differs from the registered grading or selection")
        tasks[tid] = task.model_dump(mode="json")
        orch, native = build_baseline_orchestrator(conf, tasks[tid])
        control = native_control(orch, native, conf, cfg["limits"], cfg["http_timeout_seconds"], None)
        opening = opening_hash(orch, role_settings(control)["user_simulator"], modules[2])
        names = {t.name for t in orch.agent.tools}
        if "grep" not in names or any(n.startswith("memory_") for n in names):
            raise ContractError("Expected native document search before memory assignment")
        case.update(native_sha256=digest(canonical(control)), task_sha256=control["task_sha256"],
                    customer_opening_sha256=opening, initial_db=control["initial_db"],
                    official_criteria_sha256=digest(canonical(task.evaluation_criteria.model_dump(mode="json"))))
        cases.append(case)
    allocations = matched.allocation_record(cases)
    del allocations["unique_customers"]  # Anonymous source groups above represent tasks.
    if allocations["assigned"] != spec["assigned"]:
        raise ContractError("Unexpected first pass, writer or second pass allocation")
    record = dict(series=cfg["name"], protocol_sha256=digest((ROOT / PROFILE).read_bytes()),
                  native_revision=PIN, native_task_count=len(population), knowledge_documents=len(knowledge.documents),
                  corpus_sha256=corpus_sha, retrieval=spec["retrieval_config"], **allocations)
    return spec, cfg, conn, cases, tasks, record


def required_design(cfg, spec):
    from copy import deepcopy
    from agent_memory_eval.memory.writing import REQUIRED_STORE_CONTRACT

    if (spec["completion_contract"] != REQUIRED_STORE_CONTRACT or set(spec["lines"]) != set(matched.FORMS)
            or spec["forms_per_source"] != len(matched.FORMS)):
        raise ContractError("The banking pilot requires all five storage formats")
    cfg = deepcopy(cfg)
    cfg["name"] = spec["name"]
    cfg["writing"].update(completion_contract=REQUIRED_STORE_CONTRACT, lines=spec["lines"],
                          common_instruction_ref=spec["common_instruction_ref"])
    return cfg


def prepare_required(workspace):
    from scripts.close_banking_optional import close_optional
    spec = matched.read(ROOT / REQUIRED_PROFILE)
    if matched.asset_text(ROOT, spec["parent"]) != (ROOT / PROFILE).read_text():
        raise ContractError("Unexpected source protocol")
    _, base, conn, cases, tasks, original = prepare(workspace)
    closed = close_optional(workspace)
    if (closed != matched.read(ROOT / spec["source_close"]) or original != matched.read(ROOT / PREFLIGHT)):
        raise ContractError("Original first passes or previous spending changed")
    cfg = required_design(base, spec)
    for case in cases:
        case["imported_first_pass"] = closed["sources"][case["task_id"]]
    counts = matched.allocation_record(cases)["assigned"]
    counts.update(reused_first_pass=counts["source"], source=0)
    if counts != spec["assigned"]:
        raise ContractError("Unexpected required writer or reader count")
    record = dict(original, series=spec["name"], cases=cases, assigned=counts, prior_covered_usd=closed["covered_usd"],
                  protocol_sha256=digest((ROOT / REQUIRED_PROFILE).read_bytes()), source_close_sha256=digest(canonical(closed)))
    return spec, cfg, conn, cases, tasks, record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("source-preflight", "preflight", "run"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if args.mode == "source-preflight":
        print(json.dumps(prepare(workspace)[-1], ensure_ascii=False, indent=2, sort_keys=True))
        return
    spec, cfg, conn, cases, tasks, record = prepare_required(workspace)
    if args.mode == "preflight":
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
        return
    approval = matched.read(ROOT / REQUIRED_APPROVAL)
    if (record != matched.read(ROOT / REQUIRED_PREFLIGHT) or approval.get("series") != cfg["name"]
            or approval.get("protocol_sha256") != record["protocol_sha256"]
            or approval.get("preflight_sha256") != digest(canonical(record))
            or approval.get("total_usd") != spec["cumulative_limit_usd"] or approval.get("assigned") != record["assigned"]
            or not approval.get("user_authorization")):
        raise ContractError("Published registration or banking spending approval differs")
    record["authorization"] = approval
    cfg.update(registration_sha256=digest(canonical(record)), total_usd_limit=approval["total_usd"],
               expected_endpoint_terms={r: cfg["endpoint_terms"][r] for r in ("worker", "user_simulator")},
               prior_covered_usd=record["prior_covered_usd"], record_unfunded_shards=True, stages=["write", "solve"])
    matched.run_study(workspace, cfg, {}, conn, cases, tasks, record, stage_runner=matched.stage_job,
                      model_resolver=matched.study_models)


if __name__ == "__main__":
    main()
