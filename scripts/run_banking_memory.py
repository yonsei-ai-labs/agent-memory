"""Run the pinned English τ-Knowledge pilot with the matched-trial writer and readers."""

import argparse
from decimal import Decimal
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
FEEDBACK_PROFILE = "protocols/pilots/tau-banking-result-feedback-v1.json"
FEEDBACK_PREFLIGHT = "artifacts/pilots/20261125-banking-result-feedback-preflight.json"
FEEDBACK_WRITING_PREFLIGHT = "artifacts/pilots/20261125-banking-result-feedback-writing-preflight.json"
FEEDBACK_APPROVAL = "artifacts/pilots/20261125-banking-result-feedback-authorization.json"
SCREEN_PROFILE = "protocols/pilots/tau-banking-source-screen-v1.json"
SCREEN_PREFLIGHT = "artifacts/pilots/20261127-banking-source-screen-preflight.json"
SCREEN_APPROVAL = "artifacts/pilots/20261127-banking-source-screen-authorization.json"
COHORT_PROFILE = "protocols/pilots/tau-banking-memory-cohort-v1.json"
COHORT_PREFLIGHT = "artifacts/pilots/20261127-banking-memory-cohort-preflight.json"
COHORT_APPROVAL = "artifacts/pilots/20261127-banking-memory-cohort-authorization.json"
COHORT_FIRST_APPROVAL = "artifacts/pilots/20261127-banking-memory-cohort-first-authorization.json"
OUTAGE_PROFILE = "protocols/pilots/tau-banking-cohort-outage-recovery-v1.json"
OUTAGE_APPROVAL = "artifacts/pilots/20261127-banking-cohort-outage-authorization.json"


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


def native_case(cfg, conn, modules, registered, group):
    """Bind the actual English task, native state, tools, opening and grading configuration."""
    from tau2.multilingual.runtime import load_run_language
    from tau2.runner.helpers import select_run_tasks

    tid = registered.id
    domain = conn["benchmark"]["domain"]
    case = dict(task=tid, task_id=tid, domain=domain, split=None, customer="source-" + tid,
                stratum=group, task_start_mode=START_FORMAT, scheduled_stages=list(matched.STAGES))
    conf = solve_connection(conn, matched.design_for(cfg, case, "source"),
                            matched.jobs_for(case, "source")[0], cfg["episode_cap_usd"])
    native = native_config(conf)
    task, = select_run_tasks(native, load_run_language(native))
    if task.id != registered.id or task.evaluation_criteria != registered.evaluation_criteria:
        raise ContractError("Runtime task differs from the registered selection or grading")
    task_data = task.model_dump(mode="json")
    orch, native = build_baseline_orchestrator(conf, task_data)
    control = native_control(orch, native, conf, cfg["limits"], cfg["http_timeout_seconds"], None)
    opening = opening_hash(orch, role_settings(control)["user_simulator"], modules[2])
    names = {t.name for t in orch.agent.tools}
    if "grep" not in names or any(n.startswith("memory_") for n in names):
        raise ContractError("Expected native document search before memory assignment")
    case.update(native_sha256=digest(canonical(control)), task_sha256=control["task_sha256"],
                customer_opening_sha256=opening, initial_db=control["initial_db"],
                official_criteria_sha256=digest(canonical(task.evaluation_criteria.model_dump(mode="json"))))
    if "initialized_databases" in control:
        observed = dict(agent=orch.environment.get_db_hash(), user=orch.environment.get_user_db_hash())
        if observed != control["initialized_databases"]:
            raise ContractError("Native opening differs from the registered initialized databases")
        case["initialized_databases"] = observed
    return case, task_data


def prepare(workspace, *, heldout_tasks=None):
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
    from tau2.runner.helpers import load_task_splits

    population = sorted(get_tasks(), key=lambda task: task.id)
    candidates = {}
    for task in population:
        group = stratum(task)
        if group:
            candidates.setdefault(group, []).append(task.id)
    first = {group: ids[0] for group, ids in candidates.items()}
    if first != spec["tasks"] or load_task_splits(domain) is not None:
        raise ContractError("Official population, splits or prespecified task selection changed")
    selected = spec["tasks"]
    if heldout_tasks is not None:
        if (any(len(ids) < 2 for ids in candidates.values()) or
                heldout_tasks != {group: ids[1] for group, ids in candidates.items()}):
            raise ContractError("The held-out tasks must be the second eligible IDs in the registered strata")
        selected = heldout_tasks
    knowledge = get_knowledge_base()
    corpus_sha = digest(canonical(knowledge.model_dump(mode="json")))
    cases, tasks = [], {}
    for group, tid in selected.items():
        registered = next(t for t in population if t.id == tid)
        if stratum(registered) != group:
            raise ContractError("Runtime task differs from the registered grading or selection")
        case, tasks[tid] = native_case(cfg, conn, modules, registered, group)
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
    from jsonschema import Draft202012Validator
    from agent_memory_eval.memory.writing import REQUIRED_STORE_CONTRACT

    if (spec["completion_contract"] != REQUIRED_STORE_CONTRACT or set(spec["lines"]) != set(matched.FORMS)
            or spec["forms_per_source"] != len(matched.FORMS)):
        raise ContractError("The banking pilot requires all five storage formats")
    cfg = deepcopy(cfg)
    cfg["name"] = spec["name"]
    if "writer_feedback_level" in spec:
        if spec["writer_feedback_level"] not in {"none", "result_only"}:
            raise ContractError("Unsupported banking writer feedback")
        cfg["writer_feedback_level"] = spec["writer_feedback_level"]
    cfg["writing"].update(completion_contract=REQUIRED_STORE_CONTRACT, lines=spec["lines"],
                          common_instruction_ref=spec["common_instruction_ref"])
    schema = matched.read(ROOT / "schemas/required-form-writing-v1.schema.json")
    Draft202012Validator(schema).validate(cfg["writing"])
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
    if "writer_feedback_level" in cfg:
        record["writer_feedback_level"] = cfg["writer_feedback_level"]
    return spec, cfg, conn, cases, tasks, record


def prepare_feedback(workspace, scope):
    from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
    from agent_memory_eval.contracts.connection import provider_route
    from agent_memory_eval.experiments.forms_write import feedback_pair
    from agent_memory_eval.memory.writing import writer_template
    from agent_memory_eval.recording.journal import safe_path

    spec = matched.read(ROOT / FEEDBACK_PROFILE)
    required, cfg, conn, cases, tasks, prior = prepare_required(workspace)
    require(json.loads(matched.asset_text(ROOT, spec["required_protocol"])) == required
            and spec["writer_feedback_level"] == "result_only", "Unexpected parent writer contract")
    previous = json.loads(matched.asset_text(ROOT, spec["comparison_results"]))
    audit = json.loads(matched.asset_text(ROOT, spec["feedback_audit"]))
    stages = spec["scopes"][scope]
    parent = workspace / "runs" / required["name"]
    close = matched.read(parent/"completion.json")
    previous_reg = matched.read(parent/"registration.json")["preflight"]
    require(digest((parent/"completion.json").read_bytes()) == spec["comparison_completion_sha256"]
            and close["registration_sha256"] == digest((parent/"registration.json").read_bytes())
            and previous_reg == dict(prior, authorization=matched.read(ROOT / REQUIRED_APPROVAL))
            and digest(canonical(previous_reg)) == previous["registration_sha256"]
            and close["total_covered_usd"] == previous["total_covered_usd"], "Prior comparison or cumulative cost changed")
    expected = {(w["task"], w["form"]): w for w in previous["writers"]}
    common = matched.asset_text(ROOT, cfg["writing"]["common_instruction_ref"]).rstrip("\r\n")
    model = dict(conn["worker"]["parameters"], model=conn["worker"]["model_id"],
                 provider=provider_route(conn, "worker"), stream=False, seed=cfg["writer_seed"])
    pairs = []
    for case in cases:
        tid = case["task_id"]
        source = matched.source_from(safe_path(workspace/"runs", case["imported_first_pass"]["directory"]), tid)
        folder = parent / ("task-" + tid) / "write"
        written = matched.sealed_rows(folder)[2]
        case["first_writer_inputs"] = {}
        for row in written:
            form, run = row["arm"], row["run_id"]
            checksum = row["result"]["log_sha256"]
            old = expected[tid, form]
            require((old["run"], old["log_sha256"], old["source_public_sha256"]) ==
                    (run, checksum, source.public_context_sha256), "Wrong historical writer or public experience")
            log = RecordedDecisions(safe_path(folder, row["directory"]), run, checksum)
            request = log.requests[0]
            require(request["actor_role"] == "writer", "Expected the first writer request")
            pair = feedback_pair(log.blobs.get(request["request_ref"]), source)
            planned = writer_template(model, common + "\n" + cfg["writing"]["lines"][form],
                                      completion_contract=cfg["writing"]["completion_contract"])
            planned["messages"] = [planned.pop("system"), dict(role="user", content=source.public_context),
                                   dict(role="user", content=canonical(source.feedback_message("result_only")).decode())]
            require(digest(canonical(planned)) == pair["result_only_input_sha256"],
                    "New runtime writer settings differ beyond the official result")
            pairs.append(dict(task=tid, form=form, source_run=source.source_run_id, source_log_sha256=source.source_log_sha256,
                              writer_run=run, writer_log_sha256=checksum, original_request=request["request_id"], **pair))
            case["first_writer_inputs"][form] = pair["result_only_input_sha256"]
        require(set(case["first_writer_inputs"]) == set(matched.FORMS), "Missing frozen writer format")
    require(digest(canonical(pairs)) == audit["feedback_pairs_sha256"], "Projected request pairs differ from the sealed audit")
    cfg.update(name=spec["name"], writer_feedback_level=spec["writer_feedback_level"], stages=stages)
    assigned = dict(prior["assigned"], solve=prior["assigned"]["solve"] if "solve" in stages else 0)
    schedule = {c["task"]: {s: matched.jobs_for(c, s) for s in stages} for c in cases}
    record = dict(prior, series=spec["name"], protocol_sha256=digest((ROOT / FEEDBACK_PROFILE).read_bytes()),
                  assigned=assigned, cases=cases, scope=scope, writer_feedback_level=cfg["writer_feedback_level"],
                  prior_covered_usd=close["total_covered_usd"], comparison_registration_sha256=previous["registration_sha256"],
                  feedback_schedule_sha256=digest(canonical(schedule)), writer_input_pairs_sha256=digest(canonical(pairs)))
    return spec, cfg, conn, cases, tasks, record


def verified_customer_tool_stop(folder, case, profile, design, identity, models):
    """Certify a terminal first-pass observation before the study advances its original schedule."""
    from agent_memory_eval.adapters.banking_source_screen import source_observation
    from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require

    close, ctl, rows = matched.sealed_rows(folder)
    expected = matched.jobs_for(case, "source")
    require(close["stop_reason"] == "task_start_mismatch" and len(rows) == len(expected) == 1
            and ctl["profile"] == profile and ctl["design"] == design and ctl["implementation"] == identity
            and matched.endpoint_terms(ctl["model_catalog_entries"]) == matched.endpoint_terms(models),
            "The terminal source is not bound to the same schedule, settings and implementation")
    row = rows[0]
    result = row["result"]
    require(all(row[k] == v for k, v in expected[0].items()) and row["status"] == "error"
            and result["error_code"] == "TASK_START_MISMATCH" and result["grading_status"] == "incomplete"
            and result["official_success"] is None and result["report_ref"] is None,
            "Expected an ungraded no-memory source")
    # Includes fresh-session, grade, native-control and first-customer wire checks.
    source_observation(folder, row, case, set())
    log = RecordedDecisions(folder / row["directory"], row["run_id"], result["log_sha256"])
    require(len(log.requests) == len(log.finishes) == 1 and result["http_calls"] == result["native_steps"] == 1,
            "An unsupported customer opening must precede all worker traffic")
    request, = log.requests
    finish = log.finishes[request["request_id"]]
    require(request["actor_role"] == "user_simulator" and finish["status"] == "response"
            and [e["payload"]["code"] for e in log.of_type("runtime.error")] == ["TASK_START_MISMATCH"],
            "The original customer opening must have one recorded provider response")
    wire = log.blobs.get(request["request_ref"])
    choice = log.reply(request)
    calls = choice["message"].get("tool_calls") or []
    trajectory = log.blobs.get(result["partial_trajectory_ref"])
    require(choice["finish_reason"] == "tool_calls" and calls
            and [m["role"] for m in trajectory] == ["assistant", "user"]
            and trajectory[0]["content"] == wire["messages"][-1]["content"],
            "Expected a native user tool action on the ordinary greeting")
    allowed = {tool["function"]["name"] for tool in wire["tools"]}
    native_calls = trajectory[1].get("tool_calls") or []
    require(len(native_calls) == len(calls) and all(c["function"]["name"] in allowed and
            (c["id"], c["function"]["name"], json.loads(c["function"]["arguments"]), "user")
            == (n["id"], n["name"], n["arguments"], n["requestor"]) for c, n in zip(calls, native_calls)),
            "Provider response and native customer tool actions disagree")
    return dict(reason="customer_tool_before_first_worker_input", allocation=case["task"], run=row["run_id"],
                shard_completion_sha256=digest((folder / "completion.json").read_bytes()),
                journal_sha256=result["log_sha256"], customer_request_sha256=request["request_ref"]["sha256"],
                customer_response_sha256=finish["response_ref"]["sha256"])


def checked_source_stage(args):
    """Dispatch using the original worker. A proven out-of-scope customer opening stays missing."""
    _, root, cfg, _, _, case, _, stage, identity, models, _, grant = args
    if stage != "source" or case["scheduled_stages"][0] != "source" or case.get("imported_first_pass"):
        raise ContractError("Only a scheduled native first pass can use this opening check")
    try:
        return matched.stage_job(args)
    except ContractError as error:
        if str(error) != "The sealed trial does not match its registration":
            raise
    folder = root / ("task-" + case["task"]) / stage
    profile = dict(name=cfg["name"], stage=stage, case=case["task"], registration_sha256=cfg["registration_sha256"],
                   guards=dict(max_usd=float(grant), max_calls=201, max_seconds=1500),
                   http_timeout_seconds=cfg["http_timeout_seconds"], task_start_mode=cfg["task_start_mode"],
                   interruption_policy=cfg["interruption_policy"])
    observation = verified_customer_tool_stop(folder, case, profile, matched.design_for(cfg, case, stage), identity, models)
    from scripts.run_recheck_rounds import fixed
    fixed(folder.parent / "customer-opening-stop.json", observation)


def record_source_resume(root, record):
    from agent_memory_eval.analysis.recorded_decisions import require
    from agent_memory_eval.reproduction.freeze import implementation_identity
    from scripts.run_recheck_rounds import fixed

    reg = matched.read(root / "registration.json")
    worker = implementation_identity(ROOT)
    controller_root = Path(__file__).resolve().parents[1]
    supervisor = implementation_identity(controller_root)
    require(reg["preflight"] == record and reg["implementation"] == worker,
            "Resume with the original paid implementation and the unchanged approval")
    path = root / "source-resume.json"
    known = {("task-" + case["task"]): case for case in record["cases"]}
    if path.exists():
        sealed = matched.read(path)["original_shard_seals"]
    else:
        sealed = {f.parent.parent.name: digest(f.read_bytes()) for f in sorted(root.glob("task-*/source/completion.json"))}
        require(sealed and set(sealed) == {d.name for d in root.glob("task-*") if d.is_dir()},
                "Close all original in-flight sources before resuming")
    require(set(sealed) <= known.keys() and all(digest((root / name / "source/completion.json").read_bytes()) == sha
            for name, sha in sealed.items()), "Original source seals changed")
    fixed(path, dict(format="banking-source-resume/v1", worker=worker, controller=supervisor,
                     registration_sha256=digest((root / "registration.json").read_bytes()), original_shard_seals=sealed,
                     rule="Keep the frozen worker and all submitted cells; a verified native customer tool before the first worker input is ungraded."))


def source_screen(workspace, mode):
    from collections import Counter
    from decimal import Decimal
    from agent_memory_eval.adapters.banking_source_screen import partition, screen_report
    from agent_memory_eval.analysis.recorded_decisions import require

    spec = matched.read(ROOT / SCREEN_PROFILE)
    protocol_sha = digest((ROOT / SCREEN_PROFILE).read_bytes())
    domain = workspace / "data/tau2-bench/data/tau2/domains/banking_knowledge"
    if mode == "results":
        return screen_report(workspace / "runs" / spec["name"], protocol_sha, matched.read(ROOT / SCREEN_PREFLIGHT),
                             domain, spec["source_draws_per_task"])
    require(mode in {"preflight", "run", "resume"} and spec["banking_setup"]["path"] == PROFILE, "Unknown screening mode or setup")
    matched.asset_text(ROOT, spec["banking_setup"])
    history = json.loads(matched.asset_text(ROOT, spec["previous_task_table"]))
    _, cfg, conn, _, _, base = prepare(workspace)
    cfg["limits"]["max_input_bytes"] = spec["max_input_bytes"]
    modules = matched.modules_for(workspace)
    from tau2.domains.banking_knowledge.environment import get_tasks

    native = {t.id: t for t in get_tasks()}
    files = {tid: (domain / "tasks" / (tid + ".json")).read_bytes() for tid in native}
    source_tasks = {tid: json.loads(data) for tid, data in files.items()}
    split = partition(history["tasks"], source_tasks, matched.read(domain / "db.json")["users"]["data"])
    require(dict(Counter(r["group"] for r in split.values())) == spec["expected_split"], "Prespecified split changed")
    develops = [tid for tid, r in split.items() if r["group"] == "development"]
    tasks, cases = {}, []
    for tid in develops:
        group = native[tid].evaluation_criteria.reward_basis[0].value
        case, task = native_case(cfg, conn, modules, native[tid], group)
        case["customer"] = split[tid]["customer"] or ("unresolved-" + tid)
        for draw in range(1, spec["source_draws_per_task"] + 1):
            name = f"{tid}__source-{draw}"
            tasks[name] = task
            cases.append(dict(case, source_draw=draw, task=name, scheduled_stages=["source"]))
    assigned = dict(source=len(cases), write=0, solve=0, second_pass_no_memory=0)
    require(assigned == spec["assigned"], "Source draw count changed")
    frozen = dict(series=spec["name"], protocol_sha256=protocol_sha, native_revision=PIN, corpus_sha256=base["corpus_sha256"],
                  max_input_bytes=cfg["limits"]["max_input_bytes"],
                  split=split, cases_sha256=digest(canonical(cases)), assigned=assigned,
                  task_files_sha256={tid: digest(files[tid]) for tid in develops})
    if mode == "preflight":
        return frozen
    require(frozen == matched.read(ROOT / SCREEN_PREFLIGHT), "Source screen differs from frozen preflight")
    approval = matched.read(ROOT / SCREEN_APPROVAL)
    limit = Decimal(approval["total_usd"])
    require(approval["series"] == spec["name"] and approval["protocol_sha256"] == protocol_sha
            and approval["preflight_sha256"] == digest(canonical(frozen)) and approval["assigned"] == assigned
            and limit.is_finite() and 0 < limit <= Decimal(spec["maximum_usd"]), "Source screen funding scope changed")
    record = dict(frozen, cases=cases, authorization=approval)
    cfg.update(name=spec["name"], stages=["source"], registration_sha256=digest(canonical(record)),
               total_usd_limit=approval["total_usd"], record_unfunded_shards=True, interruption_policy=matched.RESUME_UNSTARTED,
               expected_endpoint_terms={r: cfg["endpoint_terms"][r] for r in ("worker", "user_simulator")})
    endpoints = json.loads(matched.asset_text(ROOT, cfg["endpoints"]))["models"]
    cfg["expected_model_revisions"] = {r: endpoints[r]["canonical_slug"] for r in cfg["expected_endpoint_terms"]}
    if mode == "resume":
        record_source_resume(workspace / "runs" / cfg["name"], record)
    matched.run_study(workspace, cfg, {}, conn, cases, tasks, record, stage_runner=checked_source_stage,
                      model_resolver=matched.study_models)


def prepare_memory_cohort(workspace):
    from agent_memory_eval.adapters.banking_memory_cohort import allocation_counts, cohort_slots, first_release, historical_registration
    from agent_memory_eval.analysis.recorded_decisions import require
    from agent_memory_eval.experiments.forms_batch import preflight_sizes

    spec = matched.read(ROOT / COHORT_PROFILE)
    require(spec["native_setup"]["path"] == PROFILE, "Cohort must use the pinned banking setup")
    matched.asset_text(ROOT, spec["native_setup"])
    census = json.loads(matched.asset_text(ROOT, spec["population"]))
    history = json.loads(matched.asset_text(ROOT, spec["previous_results"]))
    parent, close = historical_registration(workspace, history)
    require(all(parent[k] == v for k, v in census.items()), "Original split differs from its sealed record")
    slots, missing = cohort_slots(spec, census["split"], history, parent)
    _, base, conn, _, _, native_record = prepare(workspace)
    cfg = required_design(base, json.loads(matched.asset_text(ROOT, spec["writer"])))
    require(spec["writer_bank_items"] == 1, "Each form requires exactly one stored memory")
    cfg["writing"]["proposed_limits"].update(input_bytes=spec["writer_max_input_bytes"], bank_items=spec["writer_bank_items"])
    cfg["limits"]["max_input_bytes"] = census["max_input_bytes"]
    query = json.loads(matched.asset_text(ROOT, spec["query_interface"]))["arms"]["contextual"]["read_tool_descriptions"]
    cfg.update(name=spec["name"], writer_feedback_level=spec["writer_feedback_level"], reader_admission="sealed_bank",
               query_tool_descriptions=query, max_concurrent=spec["max_concurrent"], record_unfunded_shards=True,
               interruption_policy=matched.RESUME_UNSTARTED)
    roles = ("worker", "user_simulator")
    endpoints = json.loads(matched.asset_text(ROOT, cfg["endpoints"]))["models"]
    cfg["expected_endpoint_terms"] = {r: cfg["endpoint_terms"][r] for r in roles}
    cfg["expected_model_revisions"] = {r: endpoints[r]["canonical_slug"] for r in roles}
    require(native_record["corpus_sha256"] == census["corpus_sha256"] and native_record["native_revision"] == census["native_revision"],
            "Banking corpus or native version differs")
    from tau2.domains.banking_knowledge.environment import get_tasks
    from agent_memory_eval.adapters.tau2_task_start import greeting_start

    modules = matched.modules_for(workspace)
    population = {t.id: t for t in get_tasks()}
    require(population.keys() == census["split"].keys(), "Native population changed")
    chosen = {c["task_id"] for c in slots + missing}
    native, data = {}, {}
    for tid in sorted(chosen):
        task = population[tid]
        basis = [r.value for r in task.evaluation_criteria.reward_basis]
        require(basis in (["DB"], ["ACTION"]) and not task.evaluation_criteria.nl_assertions
                and greeting_start(task.initial_state), "Unregistered grading or initial action")
        native[tid], data[tid] = native_case(cfg, conn, modules, task, basis[0])
    original = {c["task"]: c for c in parent["cases"]}
    source_sizes = []
    tasks, cases = {}, []
    common = matched.asset_text(ROOT, cfg["writing"]["common_instruction_ref"]).rstrip("\r\n")
    for slot in slots:
        tid, name = slot["task_id"], slot["task"]
        case = dict(native[tid], **slot)
        tasks[name] = data[tid]
        ref = slot.get("imported_first_pass")
        if ref:
            require(all(case[k] == original[name][k] for k in ("task_sha256", "native_sha256", "customer_opening_sha256",
                    "official_criteria_sha256", "initial_db")), "Imported native case changed")
            folder = matched.safe_path(workspace / "runs", ref["directory"])
            key = f"task-{name}/source"
            require(digest((folder / "completion.json").read_bytes()) == close["stages"][key]["seal"], "Source shard seal changed")
            source = matched.source_from(folder, tid)
            require(source and (source.source_run_id, source.source_log_sha256, source.public_context_sha256)
                    == (ref["run_id"], ref["log_sha256"], ref["public_context_sha256"]), "Source experience changed")
            design = matched.design_for(cfg, case, "write", source)
            sizes = preflight_sizes(matched.jobs_for(case, "write"), design, {tid: source}, {tid: data[tid]}, conn, "", common)
            source_sizes.extend(sizes["writer_initial_wire_bytes"])
        cases.append(case)
    first = first_release(cases)
    first_options, options = {}, {}
    for repetitions, option in spec["reader_options"].items():
        assigned = allocation_counts(cases, int(repetitions))
        require(assigned["solve"] == option["second_pass_max"], "Reader option no longer matches the cohort")
        options[repetitions] = assigned
        first_options[repetitions] = allocation_counts([c for c in cases if c["task"] in first], int(repetitions))
    record = dict(series=spec["name"], protocol_sha256=digest((ROOT / COHORT_PROFILE).read_bytes()),
                  native_revision=PIN, corpus_sha256=census["corpus_sha256"], historical_binding=history["binding"],
                  groups=spec["task_groups"], distinct_tasks=len(chosen),
                  identifiable_customers=len({c["customer"] for c in slots + missing if c["identity_basis"] != "unresolved"}),
                  confirmation_customers=len({c["customer"] for c in slots if c["cohort"] == "confirmation"}),
                  maximum_reader_tasks=len({c["task_id"] for c in slots}), historical_missing=missing,
                  new_cost_excludes_historical_screen=True, archived_writer_input_bytes=[min(source_sizes), max(source_sizes)],
                  first_release=sorted(first), first_release_options=first_options,
                  allocation_options=options, cases_sha256=digest(canonical(cases)),
                  task_sha256={tid: native[tid]["task_sha256"] for tid in sorted(native)})
    return spec, cfg, conn, cases, tasks, record


def cohort_stage(args):
    if args[7] == "source":
        return checked_source_stage(args)
    return matched.stage_job(args)


def cohort_admission(root, stage, case, task):
    if stage == "source":
        return None
    task_dir = root / ("task-" + case["task"])
    if "imported_first_pass" not in case:
        source_dir = task_dir / "source"
        if not source_dir.exists():
            return "source_not_started"
        _, _, (row,) = matched.sealed_rows(source_dir)
        if (row.get("result") or {}).get("grading_status") != "graded":
            return "source_ungraded"
    if stage != "write" and not (task_dir / "write").exists():
        return "writer_not_started"
    return None


def memory_cohort(workspace, mode, scope):
    from agent_memory_eval.adapters.banking_memory_cohort import allocation_counts, cohort_results, first_source_evidence
    from agent_memory_eval.analysis.recorded_decisions import require
    from agent_memory_eval.experiments.paired_repetitions import PairedRepetitions
    require(scope in {"first", "full"}, "Choose the first release or the full cohort")
    first = scope == "first"
    if mode == "source-evidence":
        require(first, "Archived source evidence covers the initial release")
        spec = matched.read(ROOT / COHORT_PROFILE)
        frozen = matched.read(ROOT / COHORT_PREFLIGHT)
        require(frozen["protocol_sha256"] == digest((ROOT / COHORT_PROFILE).read_bytes()), "Different source plan")
        return first_source_evidence(workspace, frozen, json.loads(matched.asset_text(ROOT, spec["previous_results"])))
    if mode == "results":
        spec = matched.read(ROOT / COHORT_PROFILE)
        frozen = matched.read(ROOT / COHORT_PREFLIGHT)
        require(frozen["protocol_sha256"] == digest((ROOT / COHORT_PROFILE).read_bytes()), "Different cohort protocol")
        native = json.loads(matched.asset_text(ROOT, spec["native_setup"]))
        config = json.loads(matched.asset_text(ROOT, native["base"]))
        query = json.loads(matched.asset_text(ROOT, spec["query_interface"]))["arms"]["contextual"]
        interface = dict(read_instruction_sha=config["delivery_instruction"]["sha256"],
                         query_tool_descriptions=query["read_tool_descriptions"])
        return cohort_results(workspace, frozen, spec["release_check"], full=not first, decision_interface=interface)
    require(mode in {"preflight", "run", "resume"}, "Unknown cohort operation")
    approval_path = ROOT / (COHORT_FIRST_APPROVAL if first else COHORT_APPROVAL)
    if mode != "preflight":
        require(approval_path.is_file(), "This release has no registered spending authorization")
    spec, cfg, conn, cases, tasks, record = prepare_memory_cohort(workspace)
    if mode == "preflight":
        return record
    require(record == matched.read(ROOT / COHORT_PREFLIGHT), "Fresh memory cohort differs from frozen preflight")
    approval = matched.read(approval_path)
    repeat = approval["readers_per_delivery"]
    require(type(repeat) is int and str(repeat) in spec["reader_options"], "Unregistered repeat count")
    cap = Decimal(approval["total_usd"])
    option = spec["reader_options"][str(repeat)]
    authorized_counts = record["first_release_options" if first else "allocation_options"][str(repeat)]
    require(cap.is_finite() and 0 < cap <= Decimal(option["first_release_maximum_usd" if first else "maximum_usd"])
            and approval["series"] == spec["name"] and approval["scope"] == scope and approval.get("user_authorization")
            and approval["assigned"] == authorized_counts
            and approval["protocol_sha256"] == record["protocol_sha256"]
            and approval["preflight_sha256"] == digest(canonical(record)), "Cohort funding scope changed")
    label = "first" if first else "remainder"
    initial_cost = "0"
    prior_binding = None
    prior_check = None
    if not first:
        prior = cohort_results(workspace, record, spec["release_check"], full=False)
        prior_check = prior["first_release_check"]
        first_data = prior["releases"]["first"]
        require(len(first_data["readers"]) == record["first_release_options"][str(repeat)]["solve"], "Reader repetition changed")
        initial_cost = first_data["spent"]
        prior_binding = first_data["binding"]
        require(Decimal(initial_cost) <= cap, "First release already exceeds the new cumulative ceiling")
    first_names = set(record["first_release"])
    cases = [c for c in cases if (c["task"] in first_names) == first]
    stages = {stage: kind for stage, kind, _ in PairedRepetitions(readers_per_bank=repeat).stages()}
    for case in cases:
        case["scheduled_stages"] = list(stages) if "imported_first_pass" in case else ["source", *stages]
    record = dict(record, cases=cases, authorization=approval, assigned=allocation_counts(cases, repeat),
                  release=label, prior_covered_usd=initial_cost, prior_release=prior_binding, prior_readiness=prior_check)
    cfg.update(stages=["source", *stages], stage_kinds=stages, total_usd_limit=approval["total_usd"],
               run_directory=spec["name"] + "-" + label, prior_covered_usd=initial_cost,
               registration_sha256=digest(canonical(record)))
    matched.run_study(workspace, cfg, {}, conn, cases, tasks, record, stage_runner=cohort_stage,
                      model_resolver=matched.study_models, stage_admission=cohort_admission)


def outage_recovery(workspace, mode):
    from agent_memory_eval.adapters.banking_memory_cohort import interrupted_reader_cases
    from agent_memory_eval.adapters.banking_trajectories import _study
    from agent_memory_eval.analysis.recorded_decisions import require
    require(mode in {"preflight", "run", "resume"}, "Unknown reader recovery operation")
    spec = matched.read(ROOT / OUTAGE_PROFILE)
    parent_root = matched.safe_path(workspace / "runs", spec["parent_recording"])
    original, close, binding = _study(parent_root, spec["parent_protocol_sha256"])
    frozen = matched.read(ROOT / COHORT_PREFLIGHT)
    require(original["release"] == "first" and original["authorization"] == matched.read(ROOT / COHORT_FIRST_APPROVAL)
            and all(original.get(k) == v for k, v in frozen.items()), "Recovery parent is not the approved first release")
    manifest = json.loads(matched.asset_text(ROOT, spec["interruptions"]))
    cases = interrupted_reader_cases(parent_root, original, close, manifest, spec)
    ceiling = original["authorization"]["total_usd"]
    prior = close["total_covered_usd"]
    require(Decimal(prior) <= Decimal(ceiling), "Original ledger exceeds its authorized ceiling")
    record = dict(series=spec["name"], protocol_sha256=digest((ROOT / OUTAGE_PROFILE).read_bytes()), cases=cases,
                  original=binding, interruptions_sha256=spec["interruptions"]["sha256"], assigned=spec["assigned"],
                  prior_covered_usd=prior, cumulative_maximum_usd=ceiling)
    if mode == "preflight":
        return record
    approval = matched.read(ROOT / OUTAGE_APPROVAL)
    require(approval.get("user_authorization") and approval["series"] == spec["name"] and approval["assigned"] == spec["assigned"]
            and approval["interruptions_sha256"] == record["interruptions_sha256"]
            and approval["protocol_sha256"] == record["protocol_sha256"] and approval["total_usd"] == ceiling,
            "A new authorization must cover exactly the interrupted slots under the cumulative ceiling")
    record["authorization"] = approval
    _, cfg, conn, parent_cases, tasks, recomputed = prepare_memory_cohort(workspace)
    require(recomputed == frozen, "Native bank controls or original source registration changed")
    native = {c["task"]: c for c in parent_cases}
    for case in cases:
        require(all(case.get(k) == v for k, v in native[case["task"]].items() if k != "scheduled_stages"),
                "Recovery changed a native control or original source")
    cfg.update(name=spec["name"], run_directory=spec["name"], stages=["solve"], total_usd_limit=ceiling,
               prior_covered_usd=prior, registration_sha256=digest(canonical(record)))
    matched.run_study(workspace, cfg, {}, conn, cases, tasks, record, stage_runner=matched.stage_job,
                      model_resolver=matched.study_models)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("source-preflight", "source-evidence", "preflight", "run", "results", "resume"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--series", choices=("required", "result-feedback", "source-screen", "memory-cohort",
                                            "memory-cohort-outage"), default="required")
    parser.add_argument("--scope", choices=("full", "writing", "first"), default=None)
    args = parser.parse_args()
    if args.mode == "source-evidence" and args.series != "memory-cohort":
        parser.error("source-evidence belongs to the banking memory cohort")
    workspace = args.workspace.resolve()
    if args.series == "memory-cohort-outage":
        if args.scope not in (None, "first"):
            parser.error("Only the first release has this registered provider outage")
        output = outage_recovery(workspace, args.mode)
        if output is not None:
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if args.series == "memory-cohort":
        output = memory_cohort(workspace, args.mode, args.scope or "first")
        if output is not None:
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return
    args.scope = args.scope or "full"
    if args.scope == "first":
        parser.error("The first release belongs to the broad memory cohort")
    if args.series == "source-screen":
        if args.scope != "full":
            parser.error("The source screen covers its complete development split")
        output = source_screen(workspace, args.mode)
        if output is not None:
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if args.mode in {"results", "resume"}:
        parser.error("This operation belongs to the source screen")
    if args.mode == "source-preflight":
        if args.series != "required" or args.scope != "full":
            parser.error("source-preflight belongs to the original four first passes")
        print(json.dumps(prepare(workspace)[-1], ensure_ascii=False, indent=2, sort_keys=True))
        return
    if args.series == "result-feedback":
        spec, cfg, conn, cases, tasks, record = prepare_feedback(workspace, args.scope)
        preflight_file = FEEDBACK_PREFLIGHT if args.scope == "full" else FEEDBACK_WRITING_PREFLIGHT
        approval_file = FEEDBACK_APPROVAL
        stages = spec["scopes"][args.scope]
    else:
        if args.scope != "full":
            parser.error("the original required-writing series has a fixed full scope")
        spec, cfg, conn, cases, tasks, record = prepare_required(workspace)
        preflight_file, approval_file = REQUIRED_PREFLIGHT, REQUIRED_APPROVAL
        stages = ["write", "solve"]
    if args.mode == "preflight":
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if not (ROOT / approval_file).is_file():
        raise ContractError("The registered banking allocations require their own spending authorization")
    approval = matched.read(ROOT / approval_file)
    if (record != matched.read(ROOT / preflight_file) or approval.get("series") != cfg["name"]
            or approval.get("protocol_sha256") != record["protocol_sha256"]
            or approval.get("preflight_sha256") != digest(canonical(record))
            or approval.get("total_usd") != spec["cumulative_limit_usd"] or approval.get("assigned") != record["assigned"]
            or approval.get("prior_covered_usd") != record["prior_covered_usd"]
            or not approval.get("user_authorization")):
        raise ContractError("Published registration or banking spending approval differs")
    record["authorization"] = approval
    cfg.update(registration_sha256=digest(canonical(record)), total_usd_limit=approval["total_usd"],
               expected_endpoint_terms={r: cfg["endpoint_terms"][r] for r in ("worker", "user_simulator")},
               prior_covered_usd=record["prior_covered_usd"], record_unfunded_shards=True, stages=stages)
    matched.run_study(workspace, cfg, {}, conn, cases, tasks, record, stage_runner=matched.stage_job,
                      model_resolver=matched.study_models)


if __name__ == "__main__":
    main()
