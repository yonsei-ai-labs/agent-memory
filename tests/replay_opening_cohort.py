"""Offline full-cohort integration fixture. Run with the benchmark workspace and a new output directory."""

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from functools import partial
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.experiments import forms_batch
from agent_memory_eval.models.openrouter import OpenRouterClient
from scripts import analyze_banking_policy_cycle as analysis, run_banking_policy_cycle as cycle, run_full_retail
from test_openrouter import Reply, response


PROFILE = "protocols/pilots/tau-banking-opening-writer-pairs-v3.json"
real_stage = cycle.policy_stage_job
cell = None
actions = ()
lines = cycle.read(cycle.ROOT / cycle.bank.REQUIRED_PROFILE)["lines"]


def block_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise AssertionError("This fixture only allows in-process transport")


def call(name, arguments, number):
    return dict(id=f"fixture-{number}", type="function", function=dict(name=name, arguments=canonical(arguments).decode()))


def local_reply(request, timeout):
    wire = json.loads(request.data)
    messages = wire["messages"]
    raw = response(cost=0)
    raw["model"] = wire["model"]
    if wire["model"].startswith("deepseek/"):
        raw["provider"] = "Fireworks"
        msg = dict(content="Please help with my account." if len(messages) == 2 else "###STOP###")
    elif [t["function"]["name"] for t in wire.get("tools", [])] == ["memory_store"]:
        form, = [k for k, line in lines.items() if messages[0]["content"].endswith("\n" + line)]
        setting = cell["writer_policy"], cell["writer_draw"], form
        if cell["task_id"] == "task_076" and setting == ("existing", 2, "fact"):
            msg = dict(content="No stored memory.")
        else:
            args = actions[-1]["arguments"]["arguments"]
            product = (json.loads(args) if isinstance(args, str) else args)["account_class"]
            item = dict(title="Opening result", body=f"The call opened {product}.")
            calls = [call("memory_store", item, 1)]
            if setting == ("candidate", 1, "episode"):
                calls.append(call("memory_store", dict(title="Additional result", body="The call opened Imaginary Product."), 2))
            msg = dict(content=None, tool_calls=calls)
    else:
        seen = sum(len(m.get("tool_calls") or []) for m in messages if m["role"] == "assistant")
        if seen < len(actions):
            a = actions[seen]
            msg = dict(content=None, tool_calls=[call(a["name"], a["arguments"], seen + 1)])
        else:
            msg = dict(content="I have finished checking.")
    raw["choices"][0].update(message=dict(role="assistant", **msg), finish_reason="tool_calls" if msg.get("tool_calls") else "stop")
    return Reply(raw)


def init_transport():
    sys.addaudithook(block_network)
    forms_batch.OpenRouterClient = partial(OpenRouterClient, opener=local_reply)


def fixture_stage(args):
    global cell, actions
    cell = args[5]
    actions = [a for a in args[6]["evaluation_criteria"]["actions"] if a["requestor"] == "assistant" and
               (a["name"] == "log_verification" or a["arguments"].get("agent_tool_name") == "open_bank_account_4821")]
    return real_stage(args)


def verify_interruption(folder, spec, destination):
    """Emulate a crash after reader four's journal close but before its allocation update."""
    from tau2.domains.banking_knowledge.environment import get_tasks

    root = folder / "runs" / spec["validation_study"]
    reg = cycle.read(root / "registration.json")
    case = next(c for c in reg["preflight"]["cases"] if c["task"] == "task_076__candidate__draw-2")
    task_dir = "task-" + case["task"]
    source = root / task_dir
    dest = destination / task_dir
    dest.mkdir(parents=True)
    original = cycle.bank.matched.sealed_rows(source / "solve")[2]
    assert len(original) == 5
    last_job = Path(original[-1]["directory"]).name

    def omit(path, names):
        if Path(path) == source / "solve":
            return ["completion.json"]
        return [last_job] if Path(path) == source / "solve/jobs" else []

    shutil.copytree(source / "write", dest / "write", copy_function=os.link)
    shutil.copytree(source / "solve", dest / "solve", copy_function=os.link, ignore=omit)
    ledger = dest / "solve/allocations.json"
    rows = cycle.read(ledger)
    for row, state in zip(rows[-2:], ("started", "planned"), strict=True):
        row.pop("result")
        row["status"] = state
    forms_batch.write_json(ledger, rows)
    ctl = cycle.read(dest / "solve/control.json")
    _, base, conn, _, _, _ = cycle.bank.prepare(folder)
    cfg = cycle.bank.required_design(base, cycle.read(cycle.ROOT / cycle.bank.REQUIRED_PROFILE))
    cfg.update(name=spec["validation_study"], writer_feedback_level=spec["feedback"],
               reader_admission=spec["reader_admission"], interruption_policy=spec["interruption_policy"],
               registration_sha256=ctl["profile"]["registration_sha256"])
    task = next(t for t in get_tasks() if t.id == case["task_id"])
    _, raw = cycle.bank.native_case(cfg, conn, cycle.bank.matched.modules_for(folder), task, spec["tasks"][task.id])
    args = (folder, destination, cfg, {}, conn, case, raw, "solve", reg["implementation"], ctl["model_catalog_entries"],
            None, str(ctl["profile"]["guards"]["max_usd"]))
    calls = []

    def counted(request, timeout):
        calls.append(None)
        return local_reply(request, timeout)

    with patch.object(forms_batch, "OpenRouterClient", partial(OpenRouterClient, opener=counted)):
        fixture_stage(args)
        submitted = len(calls)
        fixture_stage(args)
    closed = cycle.bank.matched.sealed_rows(dest / "solve")[2]
    assert submitted == len(calls) and submitted > 0
    assert closed[:-1] == original[:-1] and [r["run_id"] for r in closed] == [r["run_id"] for r in original]
    assert closed[-1]["result"]["grading_status"] == "graded"
    assert submitted == sum(b["http_calls"] for b in closed[-1]["result"]["billing_by_role"].values())
    return dict(already_finished=3, recovered_from_closed_journal=1, newly_dispatched_readers=1,
                new_local_transport_requests=submitted, extra_requests_on_second_resume=0)


def check(workspace, folder):
    from agent_memory_eval.adapters.tau_banking_opening import FIELDS

    init_transport()
    os.environ["OPENROUTER_API_KEY"] = "local-test-transport-only"
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "data").mkdir()
    (folder / "data/tau2-bench").symlink_to(workspace / "data/tau2-bench", target_is_directory=True)
    spec = dict(cycle.read(cycle.ROOT / PROFILE), source_study="fixture-sources", validation_study="fixture-readers",
                preflight_file=str(folder / "fixture-preflight.json"), authorization_file=str(folder / "fixture-control.json"))
    profile = folder / "fixture-profile.json"
    cycle.save(profile, spec)
    plan = cycle.registered_pairs(folder, profile, "preflight")
    cycle.save(Path(spec["preflight_file"]), plan)
    cycle.save(Path(spec["authorization_file"]), dict(scope="full", assigned=plan["assigned"],
               maximum_usd="10", protocol_sha256=plan["protocol_sha256"], preflight_sha256=digest(canonical(plan)),
               user_authorization="Isolated zero-cost test fixture with network disabled"))
    bank = cycle.read(cycle.ROOT / spec["banking_profile"]["path"])
    cfg = cycle.read(cycle.ROOT / bank["base"]["path"])
    models = cycle.read(cycle.ROOT / cfg["endpoints"]["path"])["models"]
    cycle.bank.matched.study_models = lambda *args: ({r: models[r] for r in ("worker", "user_simulator")}, None)
    run_full_retail.ProcessPoolExecutor = partial(ProcessPoolExecutor, initializer=init_transport)
    cycle.policy_stage_job = fixture_stage
    for phase in ("sources", "validation"):
        cycle.registered_pairs(folder, profile, phase)
    packets = analysis.registered_account_results(folder, profile, packets_only=True)["packets"]
    votes = {}
    for key, packet in packets.items():
        quote = packet["memory"]["body"]
        product = quote.removeprefix("The call opened ").removesuffix(".")
        receipt = packet["receipts"][-1]
        correct = receipt["outcome"] == "opened" and receipt["arguments"]["account_class"] == product
        claim = dict(field="body", quote=quote, outcome="opened", judgment="supported" if correct else "contradicted",
                     subject=dict(dict.fromkeys(FIELDS), account_class=product), call_ids=[receipt["call_id"]])
        votes[key] = dict(claims=[claim], no_outcome_reason=None)
    report = analysis.registered_account_results(folder, profile, ratings=dict(
        format="opening-outcome-ratings/v2", reviewer="scripted fixture labels", ratings=votes))
    writers = report["writing_outcomes"]["policy_totals"]
    pairs = report["customer_weighted"]["official_success"]["overall"]
    sessions = [r["session"] for rs in report["trials"].values() for r in rs if r.get("session")]
    missing = report["missing_readers_by_reason"]
    assert writers["candidate"]["stored_items"] == 209 and writers["existing"]["stored_items"] == 189
    assert writers["candidate"]["banks_with_unsupported_or_contradicted"] >= 19
    assert missing == {"existing": {"required_bank_incomplete": 3}, "candidate": {}}
    assert len(sessions) == len(set(sessions)) == 1137
    assert pairs["assigned_pairs"] == 570 and pairs["observed_pairs"] == 567 and pairs["observed_clusters"] == 13
    summary = dict(fixture="scripted model replies in native banking, no provider network", external_calls=0,
                   source_count=len(report["sources"]), assigned=plan["assigned"], scored_reader_sessions=len(sessions),
                   writing=writers, missing=missing, first_inputs=report["repetitions"]["first_inputs"],
                   paired_readers={k:pairs[k] for k in ("assigned_pairs", "observed_pairs", "observed_clusters")},
                   reader_status={p:Counter(r["status"] for r in rows) for p, rows in report["trials"].items()})
    summary["interruption"] = verify_interruption(folder, spec, folder / "interruption-copy")
    cycle.save(folder / "verification.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.workspace.resolve(), args.output.resolve())
