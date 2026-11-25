"""Reconstruct the registered public writer inputs and run the scoped writing stage."""

import argparse
from pathlib import Path

from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.recording.assets import asset_text
from scripts import run_banking_policy_cycle as cycle

ROOT = cycle.ROOT
PROFILE = "protocols/pilots/tau-banking-scoped-writer-v1.json"
ORIGIN = "scoped_memory.decision"


def setup():
    p = cycle.read(ROOT / PROFILE)
    d = read_json(asset_text(ROOT, p["draft"]).encode())
    previous = read_json(asset_text(ROOT, d["previous_results"]).encode())
    require(p["status"] == "ready" and d["writer_scope"]["new_candidate_writes"] == p["gate"]["denominator"]
            and d["decision_scope"]["new_full_consultations"] == p["full_consultations_authorized"] == 0,
            "Invalid scope or review denominator")
    return p, d, previous


def prepare(workspace):
    p, d, previous = setup()
    prior = read_json(asset_text(ROOT, d["previous_protocol"]).encode())
    _, _, _, strict, _ = cycle.design(workspace)
    _, base, conn, cases, tasks, _ = cycle.bank.prepare(workspace, heldout_tasks=prior["heldout"])
    cfg = cycle.bank.required_design(base, strict)
    endpoints = read_json(asset_text(ROOT, base["endpoints"]).encode())["models"]
    cfg.update(name=p["writer_study"], stages=["write"], writer_feedback_level=d["feedback"],
               total_usd_limit=p["additional_limit_usd"], prior_covered_usd="0",
               max_concurrent=p["writer_concurrency"], record_unfunded_shards=True,
               expected_endpoint_terms={r: base["endpoint_terms"][r] for r in ("worker", "user_simulator")},
               expected_model_revisions={r: endpoints[r]["canonical_slug"] for r in ("worker", "user_simulator")})
    cfg["writing"]["common_instruction_ref"] = d["proposed_instruction"]
    before, after = [asset_text(ROOT, d[k]).rstrip("\r\n") for k in ("incumbent_instruction", "proposed_instruction")]
    source_root = workspace / "runs" / prior["source_study"]
    prior_root = workspace / "runs" / prior["validation_study"]
    registration = cycle.read(source_root / "registration.json")
    source_close = cycle.read(source_root / "completion.json")
    require(digest((source_root / "registration.json").read_bytes()) == source_close["registration_sha256"]
            == previous["source_registration"] and after.startswith(before + "\n"), "Source or writer changed")
    stored = {(w["task"], w["form"]): w for w in previous["writers"] if w["policy"] == "existing"}
    checked = []
    for case in cases:
        task = case["task_id"]
        folder = source_root / ("task-" + task) / "source"
        require(source_close["stages"][str(folder.relative_to(source_root))]["seal"]
                == digest((folder / "completion.json").read_bytes()), "Unsealed source shard")
        source = cycle.bank.matched.source_from(folder, task)
        require(source.source_log_sha256 == previous["sources"][task]["log_sha256"], "Wrong public experience")
        case.update(imported_first_pass=dict(directory=str(folder.relative_to(workspace / "runs")),
            registration_sha256=digest(canonical(registration["preflight"])), run_id=source.source_run_id,
            log_sha256=source.source_log_sha256, public_context_sha256=source.public_context_sha256),
            reader_modes=["preload"], first_writer_inputs=cycle.writer_hashes(conn, cfg, source, after))
        original = cycle.writer_hashes(conn, cfg, source, before)
        old_folder = prior_root / ("task-" + task + "__existing") / "write"
        _, _, rows = cycle.bank.matched.sealed_rows(old_folder)
        require(sorted(r["arm"] for r in rows) == sorted(d["forms"]), "Missing a comparison form")
        for row in rows:
            form = row["arm"]
            old = stored[task, form]
            log = RecordedDecisions(old_folder / row["directory"], row["run_id"], old["writer_log_sha256"])
            req = next(q for q in log.requests if q["actor_role"] == "writer")
            wire = log.blobs.get(req["request_ref"])
            line = cfg["writing"]["lines"][form]
            require(original[form] == req["request_ref"]["sha256"] == digest(canonical(wire))
                    and wire["messages"][0] == dict(role="system", content=before + "\n" + line),
                    "Registered input differs from the actual incumbent request")
            wire["messages"][0]["content"] = after + "\n" + line
            require(digest(canonical(wire)) == case["first_writer_inputs"][form], "Changed input outside treatment")
            bank = cycle.read_form_bank(old_folder / row["directory"], expected_log_sha256=old["writer_log_sha256"])
            require(bank == old["bank"] and bank["source_run_id"] == source.source_run_id, "Wrong incumbent item")
            checked.append(dict(task=task, form=form, incumbent_request=original[form],
                                candidate_request=case["first_writer_inputs"][form], incumbent_run=bank["run_id"]))
    require(len(checked) == len(stored) == p["gate"]["denominator"], "Wrong number of matched writer inputs")
    record = dict(protocol_sha256=digest((ROOT / PROFILE).read_bytes()), scope=p["authorization"],
                  cap_usd=p["additional_limit_usd"], cases=cases, inputs=checked)
    cfg["registration_sha256"] = digest(canonical(record))
    return cfg, conn, cases, tasks, record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "write"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    cfg, conn, cases, tasks, record = prepare(workspace)
    if args.phase == "preflight":
        print(f"Validated {len(record['inputs'])} old/new inputs across {len(cases)} public sources")
        return
    cycle.bank.matched.run_study(workspace, cfg, {}, conn, cases, tasks, record,
        stage_runner=cycle.bank.matched.stage_job, model_resolver=cycle.registered_models)


if __name__ == "__main__":
    main()
