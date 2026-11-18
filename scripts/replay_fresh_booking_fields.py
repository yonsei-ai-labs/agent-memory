"""Replay each differing argument of a split-payment booking, then all differing arguments together."""

import argparse
from pathlib import Path

from agent_memory_eval.adapters.tau2 import native_modules, verify_checkout
from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, safe_path
from review_fact_recheck_baggage import state_hashes


def sealed_allocations(run, config_hash):
    seal = read_json((run / "completion.json").read_bytes())
    control = read_json(asset_text(run, dict(path="control.json", sha256=seal["control_sha256"])).encode())
    if control["profile"]["config_sha256"] != config_hash or control["profile"]["name"] != run.name:
        raise ContractError("The stage differs from the frozen study")
    return read_json(asset_text(run, dict(path="allocations.json", sha256=seal["allocation_sha256"])).encode())


def edit_booking(factory, original, replacements, reference):
    results = []
    groups = [[key] for key in replacements]
    if len(replacements) > 1:
        groups.append(list(replacements))
    for keys in groups:
        args = dict(original.arguments, **{k: replacements[k] for k in keys})
        env = factory()
        reply = env.get_response(original.model_copy(update={"arguments": args}))
        hashes = state_hashes(env)
        results.append(dict(fields=keys, tool_error=bool(reply.error),
                            error=reply.content if reply.error else None,
                            final_db_sha256=hashes[0], matches_reference=hashes == reference))
    return results


def probe(workspace, config, *, continuation=False):
    checkout = verify_checkout(workspace / "data/tau2-bench")
    native_modules(checkout)
    from loguru import logger
    from tau2.data_model.simulation import SimulationRun
    from tau2.multilingual.runtime import language_environment_kwargs
    from tau2.registry import registry
    from tau2.runner.presets import reload_recorded_call

    logger.disable("tau2")
    config_hash = digest(config.read_bytes())
    cfg = read_json(config.read_bytes())
    tasks = read_json(asset_text(checkout / "data/tau2/domains/airline",
                                dict(path="tasks.json", sha256=cfg["native_assets"]["tasks.json"])).encode())
    targets = {}
    for t in tasks:
        if t["id"] not in cfg["tasks"]:
            continue
        writes = [a for a in t["evaluation_criteria"]["actions"] if a["name"] in cfg["write_tools"]]
        if len(writes) == 1 and writes[0]["name"] == "book_reservation" and len(writes[0]["arguments"]["payment_methods"]) > 1:
            if t["initial_state"] is not None or t["evaluation_criteria"]["communicate_info"]:
                raise ContractError("Unexpected booking initialization or required communication")
            targets[t["id"]] = t["evaluation_criteria"]["actions"], writes[0]["arguments"]
    stages, cells, first, experiments = {}, [], {}, []
    constructor = registry.get_env_constructor("airline")
    for stage in ("source", "comparison"):
        run = workspace / "runs" / cfg[stage + "_name"]
        if stage == "comparison" and continuation:
            from agent_memory_eval.experiments.fresh_continuation import AMENDMENT, verify_continuation
            amendment = read_json((ROOT / AMENDMENT).read_bytes())
            run = safe_path(workspace, "runs/" + amendment["destination"])
            verify_continuation(ROOT, workspace, run)
        rows = sealed_allocations(run, config_hash)
        stages[stage] = digest((run / "completion.json").read_bytes())
        for r in rows:
            task = r["task_id"]
            if task not in targets or r["phase"] != "solve":
                continue
            result = r.get("result", {})
            cell = dict(stage=stage, task_id=task, job=r["index"], arm=r["arm"], repetition=r["repetition"],
                        official_success=result.get("official_success"))
            cells.append(cell)
            if type(cell["official_success"]) is not bool:
                cell["eligibility"] = "ungraded"
                continue
            folder = safe_path(run, r["directory"])
            if digest((folder / "log.jsonl").read_bytes()) != result["log_sha256"]:
                raise ContractError("Booking journal differs from sealed allocation")
            sim = SimulationRun.model_validate(BlobStore(folder).get(result["report_ref"]))
            if sim.task_id != task:
                raise ContractError("Native task differs from its assignment")
            options = language_environment_kwargs(reload_recorded_call(sim.language_context, "airline")) or {}

            def factory(options=options):
                return constructor(**options)

            ref = factory()
            actions, expected = targets[task]
            for step in actions:
                ref.make_tool_call(step["name"], requestor=step.get("requestor", "assistant"), **step["arguments"])
            reference = state_hashes(ref)
            actual = factory()
            actual.set_state(initialization_data=None, initialization_actions=None, message_history=sim.messages, strict=True)
            db = state_hashes(actual)
            if db[0] != result["final_db_hash"] or (db == reference) is not cell["official_success"]:
                raise ContractError("Strict replay differs from the original grade")
            cell.update(native_report_sha256=result["report_ref"]["sha256"], actual_db_sha256=db[0],
                        reference_db_sha256=reference[0])
            replies = {m.id: m for m in sim.messages if m.role == "tool"}
            attempted = [(i, c) for i, m in enumerate(sim.messages) for c in getattr(m, "tool_calls", None) or []
                         if c.name in cfg["write_tools"] and replies[c.id].execution_status != "rejected"]
            if len(attempted) != 1 or attempted[0][1].name != "book_reservation":
                cell["eligibility"] = "requires_exactly_one_booking_write"
                continue
            turn, call = attempted[0]
            if replies[call.id].error or call.arguments.get("user_id") != expected["user_id"]:
                cell["eligibility"] = "error_or_different_customer"
                continue
            baseline = factory()
            if baseline.get_response(call).error or state_hashes(baseline) != db:
                raise ContractError("Isolated unedited booking differs from actual final state")
            cell["booking_turn"] = turn
            if stage == "source":
                if cell["official_success"]:
                    first[task] = factory, call, reference, r["index"]
                cell["eligibility"] = "first_pass_booking"
                continue
            if set(call.arguments) != set(expected):
                raise ContractError("An accepted booking has a different native argument schema")
            keys = [k for k, v in expected.items() if call.arguments[k] != v]
            if cell["official_success"] or not keys:
                cell["eligibility"] = "official_success" if cell["official_success"] else "no_booking_argument_difference"
                continue
            cell["eligibility"] = "observed_difference"
            exp = dict(task_id=task, job=r["index"], fields=keys,
                       observed={k: call.arguments[k] for k in keys}, expected={k: expected[k] for k in keys})
            exp["replace_in_failed_second_pass"] = edit_booking(factory, call, exp["expected"], reference)
            if task in first:
                source_factory, source_call, source_ref, source_job = first[task]
                exp["source_job"] = source_job
                exp["replace_in_successful_first_pass"] = edit_booking(source_factory, source_call, exp["observed"], source_ref)
            experiments.append(exp)
    return dict(format="agent-memory-new-customer-booking-field-replay/v1", config_sha256=config_hash,
                method_sha256=digest(Path(__file__).read_bytes()), stage_completion_sha256=stages,
                task_ids=sorted(targets, key=int), coverage=cells, comparisons=experiments,
                scope="Exploratory diagnosis developed from comparison jobs 69, 91 and 105. For every failed single-booking, split-payment task with one accepted booking for the intended customer, swap each differing native booking argument individually and all together, in both directions when a successful first pass exists. Original conversations, approvals and grades are preserved.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--continuation", action="store_true", help="Include only verified new assignments after the 503 stop")
    parser.add_argument("--config", type=Path, default=ROOT / "protocols/pilots/tau2-new-customer-forms-v1.json")
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve().is_relative_to((args.workspace / "runs").resolve()):
        raise ContractError("Choose a new output outside the sealed runs")
    result = probe(args.workspace.resolve(), args.config, continuation=args.continuation)
    write_json(args.output, result)
    print(len(result["coverage"]), "assigned episodes,", len(result["comparisons"]), "observed booking differences")


if __name__ == "__main__":
    main()
