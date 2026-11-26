"""Freeze or run five independent writers and three payment requests on one archived airline task."""

import argparse
from copy import deepcopy
import json
from pathlib import Path

from agent_memory_eval.adapters.tau2 import native_modules, verify_checkout
from agent_memory_eval.contracts.connection import validate_profile
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import (
    BatchGuard, endpoint_terms, execute_prepared, live_models, preflight_sizes, solve_connection,
)
from agent_memory_eval.experiments.forms_write import load_form_sources
from agent_memory_eval.experiments.fresh_forms import CostReservations
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.runner.tau2_baseline import build_baseline_orchestrator


PROTOCOL = "protocols/pilots/tau2-airline-allocation-forms-v1.json"
CASES = ("past_only", "explicit_same", "explicit_swapped")
FORMS = ("fact", "episode", "procedure", "lesson", "free")


def variants(original, study):
    tasks = {case: deepcopy(original) for case in CASES}
    phrase = study["original_payment_phrase"]
    original_text = original["user_scenario"]["instructions"]["task_instructions"]
    if original_text.count(phrase) != 1 or original["initial_state"] is not None:
        raise ContractError("Expected the pinned split-payment request and unmodified initial database")
    past = tasks["past_only"]["user_scenario"]["instructions"]
    past["task_instructions"] = original_text.replace(phrase, study["past_payment_phrase"]) + "\n\n" + study["past_private_rule"]
    swapped = tasks["explicit_swapped"]
    swapped["user_scenario"]["instructions"]["task_instructions"] = original_text.replace(phrase, study["swapped_payment_phrase"])
    a, b = [act["arguments"] for act in swapped["evaluation_criteria"]["actions"][2:4]]
    first, second = a["payment_methods"][0], b["payment_methods"][0]
    if ([x["passengers"][0]["first_name"] for x in (a, b)] != ["Raj", "Liam"] or
            [(x["payment_id"], x["amount"]) for x in (first, second)] !=
            [("certificate_9984806", 250), ("certificate_2765295", 250)]):
        raise ContractError("Expected distinct, equally valued certificates for the two named passengers")
    first["payment_id"], second["payment_id"] = second["payment_id"], first["payment_id"]
    return tasks


def schedule(study):
    namespace = study["name"]
    def rank(slot):
        return digest(canonical([namespace, *slot]))
    forms = sorted(study["forms"], key=lambda arm: rank(["write", arm]))
    pairs = sorted(((case, arm) for case in CASES for arm in forms), key=lambda pair: rank(["solve", *pair]))
    rows = [dict(phase="write", arm=arm) for arm in forms]
    rows.extend(dict(phase="solve", arm=arm, task_variant=case) for case, arm in pairs)
    return [dict(row, index=i, repetition=1, task_id=study["task_id"]) for i, row in enumerate(rows, 1)]


def grade(task, sim, messages):
    from tau2.data_model.semantic_communication import CommunicateScoring
    from tau2.data_model.tasks import Task
    from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
    from tau2.multilingual.runtime import language_environment_kwargs
    from tau2.runner.presets import reload_recorded_call

    options = language_environment_kwargs(reload_recorded_call(sim.language_context, "airline")) or {}
    return evaluate_simulation(simulation=sim.model_copy(update={"messages": messages, "reward_info": None}),
        task=Task.model_validate(task), evaluation_type=EvaluationType.ALL, domain="airline", solo_mode=False,
        env_kwargs=options, strict_replay=True, customer_language=sim.language_context.provider_language,
        communicate_scoring=CommunicateScoring(scorer="substring"))


def check_grades(tasks, sim):
    from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage
    from tau2.domains.airline.environment import get_environment

    original = tasks["explicit_same"]
    if grade(original, sim, sim.messages).reward != sim.reward_info.reward or sim.reward_info.reward != 1:
        raise ContractError("Archived first pass does not reproduce its official success")
    expected = {case: {} for case in CASES}
    for allocation in ("explicit_same", "explicit_swapped"):
        env = get_environment()
        messages = [UserMessage(role="user", content="I approve the listed reservations and payment split.")]
        for i, step in enumerate(tasks[allocation]["evaluation_criteria"]["actions"]):
            call = ToolCall(id=f"check-{i}", name=step["name"], arguments=step["arguments"])
            response = env.get_response(call)
            if response.error:
                raise ContractError("Reference action failed in the real airline environment")
            messages.extend([AssistantMessage(role="assistant", tool_calls=[call]), response])
        messages.append(AssistantMessage(role="assistant", content="Gift cards $327, certificates $1000, Mastercard $1286."))
        for case, task in tasks.items():
            result = grade(task, sim, messages)
            match = (case == "explicit_swapped") == (allocation == "explicit_swapped")
            if (result.reward != int(match) or result.db_check.db_match != match
                    or not all(check.met for check in result.communicate_checks)):
                raise ContractError("The strict native score must distinguish only the crossed allocation")
            expected[case][allocation] = result.reward
    return expected


def prepare(root, workspace):
    from loguru import logger
    raw = (root / PROTOCOL).read_bytes()
    study = read_json(raw)
    if (study["format"] != "agent-memory-airline-allocation-forms/v1" or tuple(study["forms"]) != FORMS
            or tuple(study["variants"]) != CASES or study["read_mode"] != "preload" or study["feedback"] != "none"):
        raise ContractError("Unexpected study schedule, feedback or delivery")
    base = read_json(asset_text(root, study["base_design_ref"]).encode())
    task_id = study["task_id"]
    checkout = verify_checkout(workspace / "data/tau2-bench")
    modules = native_modules(checkout)
    logger.disable("tau2")
    from tau2.data_model.simulation import SimulationRun
    folder = checkout / "data/tau2/domains/airline"
    native_tasks = read_json(asset_text(folder, dict(path="tasks.json", sha256=study["native_tasks_sha256"])).encode())
    original, = [t for t in native_tasks if t["id"] == task_id]
    tasks = variants(original, study)

    source = deepcopy(base["source"])
    cohort, = [s for s in source["cohort"] if s["task_id"] == task_id]
    if cohort["public_context_sha256"] != study["source_public_sha256"]:
        raise ContractError("The common public writing experience has changed")
    source["excluded_tasks"] = sorted(set(source["excluded_tasks"]) | {s["task_id"] for s in source["cohort"] if s != cohort}, key=int)
    source.update(cohort=[cohort], cohort_sha256=digest(canonical([cohort])), selection=study["scope"])
    writing = deepcopy(base["writing"])
    lines = {arm: writing["lines"][arm] for arm in FORMS if arm != "free"}
    writing["lines"] = dict(lines, free=study["free_instruction"])
    jobs = schedule(study)
    design = dict(format=study["format"], source=source, writing=writing,
        model=dict(base["model"], seed_pairs=[study["seed_pair"]]),
        second_pass=dict(proposed_limits=base["second_pass"]["proposed_limits"], read_mode="preload", baseline=study["baseline"]),
        allocation=dict(planned={"eligible_source_episodes": 1},
            blocks=[dict(task_id=task_id, repetition=1, write=[r["arm"] for r in jobs if r["phase"] == "write"])]))
    connection = read_json(asset_text(root, base["model"]["connection_profile_ref"]).encode())
    validate_profile(connection)
    sources = load_form_sources(design, workspace)
    common = asset_text(root, writing["common_instruction_ref"])
    instruction = asset_text(root, base["second_pass"]["solve_instruction_ref"])
    native_checks = {}
    cfg = solve_connection(connection, design, jobs[0], 1)
    for case, task in tasks.items():
        orch, _ = build_baseline_orchestrator(cfg, task)
        rendered = " ".join(orch.user.system_prompt.split())
        requested = task["user_scenario"]["instructions"]["task_instructions"]
        if " ".join(requested.split()) not in rendered:
            raise ContractError("The native customer did not receive the assigned scenario")
        worker = dict(policy=orch.agent.system_prompt, tools=[t.openai_schema for t in orch.agent.tools])
        native_checks[case] = dict(worker_directions_sha256=digest(canonical(worker)),
            customer_directions_sha256=digest(orch.user.system_prompt.encode()), initial_db=orch.environment.get_db_hash(),
            task_sha256=digest(canonical(task)))
    if (len({v["worker_directions_sha256"] for v in native_checks.values()}) != 1
            or len({v["initial_db"] for v in native_checks.values()}) != 1
            or len({v["customer_directions_sha256"] for v in native_checks.values()}) != len(CASES)):
        raise ContractError("Only customer scenarios and the registered grading target may vary")
    archived = workspace / source["root"]
    baseline = read_json(asset_text(archived, dict(path="baseline-summary.json", sha256=source["summary_sha256"])).encode())
    record, = [r for r in baseline["runs"] if r["run_id"] == cohort["run_id"]]
    sim = SimulationRun.model_validate(BlobStore(archived).get(record["report_ref"]))
    manifest = dict(protocol_sha256=digest(raw), source_run=cohort["run_id"], public_sha256=study["source_public_sha256"],
        baseline_success=record["official_success"], jobs=jobs, writers=len(FORMS), consultations=len(FORMS)*len(CASES),
        second_pass_no_memory=0, native_checks=native_checks, strict_native_grade_checks=check_grades(tasks, sim),
        writer_prompt_sha256={arm: digest((common.rstrip("\r\n") + "\n" + line).encode()) for arm, line in writing["lines"].items()},
        **preflight_sizes(jobs, design, sources, {task_id: original}, connection, instruction, common))
    plan = dict(summary=manifest, jobs=jobs, tasks={task_id: original}, task_variants={task_id: tasks}, sources=sources,
        modules=modules, connection=connection, instruction=instruction)
    return study, design, plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--freeze", action="store_true", help="Print the offline manifest pinned to a clean implementation commit")
    mode.add_argument("--live", type=Path, metavar="FROZEN_MANIFEST")
    parser.add_argument("--max-usd", type=float, help="Explicitly authorized total for this execution")
    args = parser.parse_args()
    if bool(args.live) != (args.max_usd is not None):
        parser.error("--live requires its explicit --max-usd")
    workspace = args.workspace.resolve()
    study, design, plan = prepare(ROOT, workspace)
    output = plan["summary"]
    if args.freeze or args.live:
        identity = implementation_identity(ROOT)
        output = dict(output, implementation=identity)
    if args.live:
        if read_json(args.live.read_bytes()) != output:
            raise ContractError("Frozen source, implementation, native grading or allocations differ")
        expected = read_json(asset_text(ROOT, study["endpoint_ref"]).encode())["models"]
        guard = BatchGuard(max_calls=study["max_calls"], max_usd=args.max_usd, max_seconds=study["max_seconds"])
        models = live_models(plan["connection"])
        if endpoint_terms(models) != endpoint_terms(expected) or implementation_identity(ROOT) != identity:
            raise ContractError("The execution price, model provider or frozen checkout changed")
        out = safe_path(workspace, "runs/" + study["name"])
        profile = dict(study, total_usd_limit=args.max_usd, manifest_sha256=digest(args.live.read_bytes()),
                       implementation_commit=identity["implementation_commit"])
        reserve = CostReservations(out, study["unpriced_http"], guard, models)
        output = execute_prepared(profile, ROOT, out, identity, design, plan, models, guard, close_cost=reserve)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
