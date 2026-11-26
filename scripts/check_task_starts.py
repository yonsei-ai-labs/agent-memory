"""Exercise the shared first-decision boundary on native, pinned tasks without model calls."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.runner.task_start import FORMAT, TaskStart, TaskStartMismatch


def check_bfcl(checkout, requested):
    from agent_memory_eval.adapters.bfcl_task_start import open_simple_python, simple_python_tasks

    tasks = simple_python_tasks(checkout)
    selected = requested or sorted(tasks)
    controls = dict(worker="offline_input_check", metric="first_decision")
    sources = {key: open_simple_python(tasks[key], controls) for key in selected}
    fresh_tasks = simple_python_tasks(checkout)
    for key, source in sources.items():
        open_simple_python(fresh_tasks[key], controls, reference=source)
    return dict(benchmark="bfcl", category="simple_python", matched=len(selected))


def check_tau2(study_root, requested):
    from agent_memory_eval.adapters.tau2_task_start import business_input, matched_opening
    from agent_memory_eval.experiments.matched_trial import native_control, role_settings, source_receipt
    from agent_memory_eval.recording.journal import safe_path
    from scripts import run_fresh_matched as study

    modules = study.modules_for(ROOT)
    agent_module, user_module, utils = modules
    from tau2.data_model.message import AssistantMessage
    from tau2.multilingual.runtime import load_run_language
    from tau2.runner.helpers import select_run_tasks

    reg_bytes = (study_root / "registration.json").read_bytes()
    if digest(reg_bytes) != study.read(study_root / "completion.json")["registration_sha256"]:
        raise ContractError("Study registration seal differs")
    registration = read_json(reg_bytes)["preflight"]
    if registration["protocol_sha256"] != digest((ROOT / study.PROFILE).read_bytes()):
        raise ContractError("Unexpected source protocol")
    cases = {c["task"]: c for c in registration["cases"]}
    cfg = study.read(ROOT / study.PROFILE)
    base = read_json(study.asset_text(ROOT, cfg["connection"]).encode())
    matched, unavailable = [], []
    for key in requested or cases:
        case = cases[key]
        folder = safe_path(study_root, "task-" + key + "/" + case["scheduled_stages"][0])
        _, ctl, (row,) = study.sealed_rows(folder)
        source = study.source_from(folder, case["task_id"])
        if source is None:
            unavailable.append(key)
            continue
        spec = dict(case, registration_sha256=digest(canonical(registration)), task_start_mode=FORMAT)
        if ctl["profile"]["registration_sha256"] != spec["registration_sha256"]:
            raise ContractError("Source shard belongs to a different registration")
        receipt = source_receipt(safe_path(folder, row["directory"]), source, spec, reconstruct_archived_start=True)
        reference = TaskStart.from_record(receipt["task_start"])
        config = study.solve_connection(base, study.design_for(cfg, case, "source"),
                                        study.jobs_for(case, "source")[0], cfg["episode_cap_usd"])
        native = study.native_config(config)
        task, = select_run_tasks(native, load_run_language(native))
        orch, native = study.build_baseline_orchestrator(config, task.model_dump(mode="json"))
        control = native_control(orch, native, config, cfg["limits"], cfg["http_timeout_seconds"],
                                 study.judge_config(cfg, case["domain"]))
        observed = []

        def verify_opening(actor, wire, messages):
            if actor != "user_simulator" or digest(canonical(wire)) != case["customer_opening_sha256"]:
                raise TaskStartMismatch("The opening generator has different inputs")

        def first_action(*, messages, tools, **kwargs):
            public = business_input(utils.to_litellm_messages(messages), [t.openai_schema for t in tools], control)
            if canonical(public) != reference.public_input or observed != [reference]:
                raise TaskStartMismatch("Native worker did not receive the sealed public input")
            return AssistantMessage(role="assistant", content="Offline follow-up question.")

        def next_customer(*, messages, **kwargs):
            if utils.to_litellm_messages(messages)[-1]["content"] != "Offline follow-up question.":
                raise TaskStartMismatch("The customer did not receive the new worker action")
            return AssistantMessage(role="assistant", content="###STOP###")

        with (patch.object(utils, "completion", side_effect=AssertionError("Unexpected model call")),
              patch.object(agent_module, "generate", side_effect=first_action) as worker,
              patch.object(user_module, "generate", side_effect=next_customer) as customer,
              matched_opening(orch, modules, control, role_settings(control)["user_simulator"],
                              verify_opening, observed.append, reference=reference)):
            orch.initialize()
            orch.step()
            orch.step()
            orch.step()
        if (worker.call_count, customer.call_count) != (1, 1):
            raise TaskStartMismatch("Expected one new worker action and one subsequent customer response")
        matched.append(key)
    return dict(benchmark="tau2", matched=len(matched), source_unavailable=unavailable)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="benchmark", required=True)
    bfcl = sub.add_parser("bfcl")
    bfcl.add_argument("--checkout", type=Path, required=True)
    tau2 = sub.add_parser("tau2")
    tau2.add_argument("--study", type=Path, required=True)
    for command in (bfcl, tau2):
        command.add_argument("--task", action="append", help="Select a task ID (repeatable). Default: all.")
    args = parser.parse_args()
    result = check_bfcl(args.checkout, args.task) if args.benchmark == "bfcl" else check_tau2(args.study, args.task)
    print(json.dumps(dict(format=FORMAT, **result, provider_calls=0), sort_keys=True))


if __name__ == "__main__":
    main()
