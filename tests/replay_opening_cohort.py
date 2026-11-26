"""Offline full-cohort integration fixture. Run with the benchmark workspace and a new output directory."""

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from functools import partial
import json
import os
from pathlib import Path
import sys

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
            product = json.loads(actions[-1]["arguments"]["arguments"])["account_class"]
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
    models = cycle.read(cycle.ROOT / cycle.read(cycle.ROOT / spec["banking_profile"]["path"])["endpoints"]["path"])["models"]
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
    cycle.save(folder / "verification.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.workspace.resolve(), args.output.resolve())
