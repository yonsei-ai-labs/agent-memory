"""Continue the full registered study after the two sealed input-validation failures."""

import argparse
from decimal import Decimal
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory

from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.journal import safe_path
from scripts.run_fresh_matched import (
    PREFLIGHT, STAGES, check_published_cases, prepare, read, sealed_rows, stage_job, study_models,
)
from scripts.run_full_retail import accounting, run_study, stage_order


AMENDMENT = "protocols/pilots/tau2-fresh-matched-continuation-v1.json"


def verify_prior_stage(folder, case, pin, grant):
    end, ctl, rows = sealed_rows(folder)
    row, = rows
    result = row["result"]
    if (digest((folder/"completion.json").read_bytes()) != pin["completion_sha256"]
            or ctl["profile"]["case"] != case or ctl["design"]["stage"] != "source"
            or row["run_id"] != pin["run_id"] or row["arm"] != "no_memory"
            or row["status"] != "error" or result["error_code"] != "FIRST_PASS_MISMATCH"
            or result["grading_status"] != "incomplete" or result["official_success"] is not None
            or digest(safe_path(folder, row["directory"] + "/log.jsonl").read_bytes()) != pin["log_sha256"]
            or result["log_sha256"] != pin["log_sha256"] or end["unknown_cost"]
            or end["stop_reason"] != "first_pass_mismatch"):
        raise ContractError("The sealed first-pass validation failure has changed")
    cost = accounting(folder, grant)
    if cost["http"] != 3 or Decimal(cost["reported"]) != Decimal(pin["reported_usd"]) or Decimal(cost["held"]) != 0:
        raise ContractError("Carried charges must match the original six observed requests")


def continue_stage(args):
    workspace, output, cfg, _, _, case, _, stage, _, _, _, grant = args
    prior = cfg["continued_from"]
    if stage != "initial-source" or case["task"] not in prior["original_sources"]:
        return stage_job(args)
    pin = prior["original_sources"][case["task"]]
    relative = "task-" + case["task"] + "/" + stage
    source = workspace/"runs"/prior["parent"]/relative
    dest = output/relative
    verify_prior_stage(source, case["task"], pin, grant)
    if not dest.exists():
        dest.parent.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=dest.parent) as tmp:
            staged = Path(tmp)/"stage"
            copytree(source, staged)
            verify_prior_stage(staged, case["task"], pin, grant)
            staged.rename(dest)
    verify_prior_stage(dest, case["task"], pin, grant)


def prepare_continuation(workspace):
    amendment = read(ROOT/AMENDMENT)
    original = workspace/"runs"/amendment["parent"]
    regfile = original/"registration.json"
    reg = read(regfile)
    if (digest(regfile.read_bytes()) != amendment["parent_registration_sha256"]
            or reg["implementation"]["implementation_commit"] != amendment["parent_implementation"]
            or reg["preflight"]["protocol_sha256"] != amendment["parent_protocol_sha256"]):
        raise ContractError("Original approval and registration are required")
    cfg, conn, cases, tasks, record = prepare(ROOT, workspace, "full")
    check_published_cases(record, read(ROOT/PREFLIGHT))
    auth = reg["preflight"]["authorization"]
    if (dict(record, authorization=auth) != reg["preflight"] or auth["scope"] != "full"
            or auth["maximum_usd"] != cfg["authorization_options"]["full"]["maximum_usd"]):
        raise ContractError("The continuation differs from the approved full design")
    expected = {original/("task-" + task)/"initial-source" for task in amendment["original_sources"]}
    if (set(original.glob("task-*/*")) != expected or set(original.glob("*-wave-*.json")) != {original/"initial-source-wave-1.json"}
            or (original/"completion.json").exists()):
        raise ContractError("The original execution contains other allocations")
    grant = read(original/"initial-source-wave-1.json")["per_shard_grant"]
    for task, pin in amendment["original_sources"].items():
        verify_prior_stage(original/("task-" + task)/"initial-source", task, pin, grant)
    record = reg["preflight"]
    cfg = dict(cfg, run_directory=amendment["directory"], registration_sha256=digest(canonical(record)),
               total_usd_limit=auth["maximum_usd"], expected_endpoint_terms=cfg["endpoint_terms"],
               record_unfunded_shards=True, stages=["initial-" + s for s in STAGES] + list(STAGES),
               continued_from=amendment, first_remaining_tasks=amendment["first_remaining_tasks"])
    order = stage_order(cfg, cases, "source")
    first = [next(c["task"] for c in order if c["domain"] == domain) for domain in ("airline", "retail")]
    if first != cfg["first_remaining_tasks"]:
        raise ContractError("Verification tasks must follow the original within-domain order")
    return cfg, conn, cases, tasks, record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    args = parser.parse_args()
    ws = args.workspace.resolve()
    cfg, conn, cases, tasks, reg = prepare_continuation(ws)
    if args.mode == "preflight":
        print("Verified", len(cases), "original cases and", len(cfg["continued_from"]["original_sources"]), "carried sources")
    else:
        run_study(ws, cfg, {}, conn, cases, tasks, reg, stage_runner=continue_stage, model_resolver=study_models)
