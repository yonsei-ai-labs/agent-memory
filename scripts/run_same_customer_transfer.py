"""Run the registered same-customer pair from sealed first-pass notes in fresh native tasks."""

import argparse
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import FORMS, write_json
from agent_memory_eval.experiments.fresh_forms import scenario_customer
from agent_memory_eval.recording.assets import asset_text
from run_frozen_bank_repeats import prepare, run, schedule, sealed, shard_name

PROFILE = "protocols/pilots/tau2-same-customer-transfer-v1.json"


def prepare_same_customer(workspace):
    raw = (ROOT / PROFILE).read_bytes()
    cfg = read_json(raw)
    review = read_json(asset_text(ROOT, cfg["nomination_ref"]).encode())
    prior, state = prepare(workspace)
    inventory = read_json(asset_text(ROOT, cfg["inventory_ref"]).encode())
    pairs = {p["target"]: p["source"] for p in cfg["pairs"]}
    nominated = {p["target_task"]: p["source_task"] for p in review["candidate_diagnostics"]}
    if (prior != inventory or cfg["status"] != "ready" or nominated != pairs or cfg["tasks"] != list(pairs)
            or tuple(cfg["forms"]) != FORMS or cfg["controls"] != ["no_memory", "empty_preload"]
            or cfg["delivery"] != "preload" or cfg["writer_repetitions"] != [1, 2]
            or cfg["solver_seeds_by_wave_and_writer"] != [[8, 9], [9, 8]]
            or Decimal(str(cfg["per_task_wave_guards"]["max_usd"])) * len(pairs) * 2 != cfg["total_usd_limit"]):
        raise ContractError("Same-customer pairs, source inventory, allocation or budget differ from registration")
    airline = read_json(asset_text(ROOT, cfg["airline_ref"]).encode())
    base = state["checkout"] / "data/tau2/domains/airline"
    assets = {name: sealed(base, name, sha) for name, sha in airline["native_assets"].items()}
    tasks = {t["id"]: t for t in assets["tasks.json"]}
    sources = {t["task_id"]: t for t in prior["tasks"]}
    targets = []
    for target, source in pairs.items():
        customer = scenario_customer(tasks[target], assets["db.json"])
        split = assets["split_tasks.json"]
        if (customer != scenario_customer(tasks[source], assets["db.json"]) or target == source
                or source not in split["train"] or target not in split[cfg["split"]]):
            raise ContractError("Source and target must be distinct tasks for the same customer across official splits")
        targets.append({"task_id": target, "source_task_id": source, "customer_group": "customer-" + digest(customer.encode())[:12],
                        "native_task_sha256": digest(canonical(tasks[target]))})
    if len({r["customer_group"] for r in targets}) != len(pairs):
        raise ContractError("The two source-target pairs must have different customers")
    scheduled = {shard_name(t, wave): [dict(r, source_task_id=pairs[t]) for r in schedule(cfg, t, wave)]
                 for wave in (1, 2) for t in pairs}
    banks = [b for b in prior["banks"] if b["task"] in pairs.values()]
    if len(banks) != 16 or any(len(jobs) != 12 for jobs in scheduled.values()):
        raise ContractError("Expected sixteen authentic writer assignments and forty-eight native sessions")
    manifest = {"study": cfg["name"], "protocol_sha256": digest(raw), "parent": prior["parent"],
                "parent_completion_sha256": prior["parent_completion_sha256"], "benchmark_revision": prior["benchmark_revision"],
                "target_split": cfg["split"], "tasks": targets, "sources": [sources[t] for t in pairs.values()],
                "banks": banks, "guards_per_task_wave": cfg["per_task_wave_guards"], "scheduled": scheduled}
    state.update(cfg=cfg, tasks={t: tasks[t] for t in pairs}, source_for_target=pairs)
    return manifest, state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--preflight-output", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    manifest, state = prepare_same_customer(workspace)
    if args.preflight_output:
        if args.preflight_output.exists():
            parser.error("Choose a new output for the preflight")
        write_json(args.preflight_output, manifest)
    if args.live:
        print(run(workspace, manifest, state))
    else:
        print("Frozen writer assignments:", len(manifest["banks"]), "native assignments:", sum(map(len, manifest["scheduled"].values())))
