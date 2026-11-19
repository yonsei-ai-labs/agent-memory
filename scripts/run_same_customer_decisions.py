"""Freeze, execute or re-audit the paired historical-cue decisions."""

import argparse
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.experiments.multi_workflow import arm_id
from agent_memory_eval.experiments.replay_comparison import audited_allocations, live, preflight
from agent_memory_eval.experiments.same_customer_decisions import PROFILE, STUDY


def check_pairs(cfg, result, wires):
    for case, rule in cfg["cases"].items():
        source = rule["source"]
        arms = {v: arm_id(source, v, 0) for v in ("bound", "unbound")}
        left, right = (canonical(wires[case, arm]) for arm in arms.values())
        anchor = cfg["anchors"][source]
        old, new = (("Linked list entry: " + anchor[v] + ".").encode() for v in arms)
        if (
            len(left) != len(right)
            or left.count(old) != 1
            or right.count(new) != 1
            or left.replace(old, new, 1) != right
            or len({v["common_wire_sha256"] for v in result["cells"][case].values()}) != 1
        ):
            raise ContractError("Matched requests differ outside the intended text intervention")
    if len(result["slots"]) != 96 or len(wires) != 48:
        raise ContractError("Expected 96 allocations and 48 case-arm pairs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["preflight", "live", "audit"])
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cfg = read_json((ROOT / PROFILE).read_bytes())
    workspace = args.workspace.resolve()
    if args.output and args.output.exists():
        parser.error("Choose a new output")
    if args.mode == "audit":
        result = audited_allocations(workspace, cfg, STUDY)
    else:
        result, wires = preflight(workspace, cfg, STUDY)
        check_pairs(cfg, result, wires)
        if args.mode == "live":
            result = live(workspace, cfg, STUDY)
    if args.output:
        write_json(args.output, result)
    print(
        {k: result[k] for k in ("scored_replies", "http_calls", "stop_reason", "reported_usd") if k in result}
        or "Preflight passed"
    )
