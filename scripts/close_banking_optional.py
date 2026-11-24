"""Read-only accounting for the optional pilot stopped at the last empty-bank assignment."""

import argparse
from decimal import Decimal
import json
from pathlib import Path

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from scripts import run_fresh_matched as study


def close_optional(workspace):
    base = workspace / "runs/tau-banking-memory-01"
    reg = study.read(base / "registration.json")
    original = study.read(ROOT / "artifacts/pilots/20261124-banking-memory-preflight.json")
    require({k:v for k,v in reg["preflight"].items() if k != "authorization"} == original,
            "Unexpected optional registration")
    phases, source_refs = {}, {}
    for case in original["cases"]:
        tid = case["task_id"]
        for stage in study.STAGES:
            folder = base / ("task-" + tid) / stage
            rows = study.read(folder / "allocations.json")
            jobs = study.jobs_for(case, stage)
            require(len(rows) == len(jobs) and all(all(r[k] == v for k,v in j.items()) for r,j in zip(rows,jobs)),
                    "Optional allocations changed")
            sealed = (folder / "completion.json").is_file()
            if sealed:
                _, control, checked = study.sealed_rows(folder)
                require(rows == checked, "Optional seal changed")
            else:
                require((tid, stage) == ("task_008", "solve"), "Unexpected unsealed shard")
                control = study.read(folder / "control.json")
            paid, held, calls, terminal = Decimal(0), Decimal(0), 0, 0
            logs = {}
            for row in rows:
                path = folder / row["directory"] / "log.jsonl"
                events = []
                if path.is_file():
                    raw = path.read_bytes()
                    logs[row["arm"]] = digest(raw)
                    events = [e for line in raw.splitlines() for e in json.loads(line)["events"]]
                begin = {e["payload"]["request_id"] for e in events if e["type"] == "model.request_started"}
                end = [e["payload"] for e in events if e["type"] == "model.request_finished"]
                require(begin == {e["request_id"] for e in end} and len(begin) == len(end),
                        "Open request at the optional stopping boundary")
                if row["status"] in {"planned", "started", "not_started"}:
                    require(not begin and (tid, stage, row["arm"]) == ("task_008", "solve", "procedure__on_demand"),
                            "A supposedly unstarted consultation submitted a request")
                    continue
                result = row["result"]
                require(result["log_sha256"] == logs[row["arm"]], "Optional job journal seal changed")
                terminal += 1
                known = sum((Decimal(str(e["cost_usd"])) for e in end if e["cost_status"] == "observed"), Decimal(0))
                require(known == sum(Decimal(x["reported_usd"]) for x in result["billing_by_role"].values()),
                        "Optional provider cost differs from job billing")
                calls += sum(e["delivery_status"] != "not_submitted" for e in end)
                paid += known
            if sealed:
                close = study.read(folder / "completion.json")
                held = Decimal(str(close["reserved_usd"]))
                if any((row.get("result") or {}).get("unknown_cost") and
                       not row["result"].get("unpriced_reservation") for row in rows):
                    held = Decimal(str(control["profile"]["guards"]["max_usd"])) - paid
            elif any((row.get("result") or {}).get("unknown_cost") for row in rows):
                held = Decimal(str(control["profile"]["guards"]["max_usd"])) - paid
            phases[f"{tid}/{stage}"] = dict(terminal=terminal, http=calls, confirmed=str(paid), held=str(held),
                allocations_sha256=digest((folder/"allocations.json").read_bytes()), journals=logs,
                completion_sha256=digest((folder/"completion.json").read_bytes()) if sealed else None)
            if stage == "source":
                source = study.source_from(folder, tid)
                require(source is not None, "Required pilot lacks a graded first pass")
                source_refs[tid] = dict(directory=str(folder.relative_to(workspace/"runs")),
                    registration_sha256=digest(canonical(reg["preflight"])), run_id=source.source_run_id,
                    log_sha256=source.source_log_sha256, public_context_sha256=source.public_context_sha256)
    paid = sum(Decimal(p["confirmed"]) for p in phases.values())
    held = sum(Decimal(p["held"]) for p in phases.values())
    return dict(format="optional-banking-stop/v1", implementation=reg["implementation"]["implementation_commit"],
                original_registration_sha256=digest((base/"registration.json").read_bytes()),
                confirmed_usd=str(paid), held_usd=str(held), covered_usd=str(paid+held), phases=phases, sources=source_refs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    print(json.dumps(close_optional(parser.parse_args().workspace.resolve()), indent=2, sort_keys=True))
