"""Exercise the BFCL on-demand path against a copy of the original source journals."""

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess

from agent_memory_eval.adapters.bfcl_task_start import open_relevance
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require, sealed_bytes
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.models.local_qwen import LocalQwen
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner.bfcl_relevance import load_inputs, solve


def run(args, cfg, spec, archive):
    cases, is_empty = load_inputs(args.checkout, cfg)
    require([r["id"] for _, r in cases] == archive["registration"]["tasks"]
            and len(cases) == spec["limits"]["allocated"]
            and cfg["limits"]["on_demand_calls"] == spec["limits"]["worker_calls_per_question"],
            "Wrong scope for read-path check")
    require(args.checkpoint.name == cfg["model_revision"], "Wrong checkpoint revision")
    for name, checksum in cfg["checkpoint_sha256"].items():
        with (args.checkpoint / name).open("rb") as stream:
            require(hashlib.file_digest(stream, "sha256").hexdigest() == checksum, "Changed local checkpoint")
    args.control.mkdir(parents=True, exist_ok=False)
    reg = dict(spec_sha256=digest(args.spec.read_bytes()), original_archive_sha256=digest(args.archive.read_bytes()),
               implementation=subprocess.check_output(["git", "-C", str(Path(__file__).parent), "rev-parse", "HEAD"],
                                                      text=True).strip(), tasks=archive["registration"]["tasks"])
    (args.control / "registration.json").write_bytes(canonical(reg) + b"\n")
    client = LocalQwen(args.model_python, args.checkpoint, cfg["model"], cfg["generation"]["seed"])
    try:
        require(client.runtime == read_json((args.parent / "runtime.json").read_bytes()), "Different inference runtime")
        (args.control / "runtime.json").write_bytes(canonical(client.runtime) + b"\n")
        guided = dict(cfg, memory_notice=cfg["memory_notice"] + "\n" + spec["append_to_neutral_notice"])
        controls = dict(model=cfg["model"], revision=cfg["model_revision"], generation=cfg["generation"])
        for case in cases:
            category, row = case
            task = row["id"]
            parent = archive["tasks"][task]
            source_id = task + "-source"
            source = RecordedDecisions(args.parent / task, source_id, parent["journal_sha256"])
            source_req, = source.requests
            baseline = dict(start=open_relevance(row, category, controls), run=source_id,
                            business=source.blobs.get(source_req["request_ref"]))
            writer, = [r for r in parent["allocations"] if r["phase"] == "write" and r["form"] == spec["parent_form"]]
            require(writer["items"] == 1, "The parent must have one real stored note")
            folder = args.control / task
            shutil.copytree(args.parent / task, folder)
            sealed_bytes(folder / "log.jsonl", parent["journal_sha256"])
            with Journal(folder, cfg["name"], contract_version="1.2") as journal:
                solve(journal, guided, case, client, is_empty, source=baseline, form=spec["control_label"],
                      writer=dict(run_id=writer["run"], snapshot_id=writer["snapshot"]), mode="on_demand")
            print(task, flush=True)
    finally:
        client.close()


def inputs(args):
    spec = read_json(args.spec.read_bytes())
    cfg = read_json(sealed_bytes(args.parent_profile, spec["parent_profile_sha256"]))
    archive = read_json(sealed_bytes(args.archive, spec["parent_archive_sha256"]))
    require(read_json((args.parent / "registration.json").read_bytes()) == archive["registration"]
            and archive["registration"]["profile_sha256"] == spec["parent_profile_sha256"],
            "Original archive does not belong to registration")
    return cfg, spec, archive


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parent", type=Path, required=True)
    p.add_argument("--control", type=Path, required=True)
    p.add_argument("--parent-profile", type=Path, required=True)
    p.add_argument("--spec", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    return p


if __name__ == "__main__":
    parser = arguments()
    for arg in ("checkout", "checkpoint", "model-python"):
        parser.add_argument("--" + arg, type=Path, required=True)
    args = parser.parse_args()
    run(args, *inputs(args))
