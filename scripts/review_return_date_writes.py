"""Locate the return date and flight number in every historical task-24 writer bank."""

import argparse
import json
from pathlib import Path
import re

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path


def review(root):
    previous = root / "artifacts/analyses/20261118-new-customer-forms-final/results.json"
    anchor = read_json(previous.read_bytes())["stages"]["comparison"]
    base = root / "runs" / anchor["name"]
    seal = json.loads(asset_text(base, dict(path="completion.json", sha256=anchor["completion_sha256"])))
    if seal["allocation_sha256"] != anchor["allocation_sha256"]:
        raise ContractError("Writer allocations differ from the prior final study")
    allocations = json.loads(asset_text(base, dict(path="allocations.json", sha256=seal["allocation_sha256"])))
    date = re.compile(r"\b(?:2024-05-25|May\s+(?:25|20\s*[-–—]\s*25))\b", re.I)
    flight = re.compile(r"\bHAT276\b", re.I)
    rows = []
    for row in allocations:
        if row["task_id"] != "24" or row["phase"] != "write":
            continue
        bank = read_form_bank(safe_path(base, row["directory"]), expected_log_sha256=row["result"]["log_sha256"])
        matches = []
        for item in bank["items"]:
            text = item["title"] + "\n" + item["body"]
            evidence = sorted([*date.finditer(text), *flight.finditer(text)], key=lambda m: m.start())
            matches.append(dict(item_id=item["id"], version=item["version"], item_sha256=digest(canonical(item)),
                date_literal=bool(date.search(text)), flight_literal=bool(flight.search(text)),
                excerpt=text[max(0, evidence[0].start()-36):evidence[-1].end()+36] if evidence else None))
        rows.append(dict(job=row["index"], arm=row["arm"], repeat=row["repetition"], log_sha256=row["result"]["log_sha256"],
                         source_public_context_sha256=bank["public_context_sha256"], snapshot=bank["snapshot_id"], items=matches))
    if (len(rows) != 8 or len({r["source_public_context_sha256"] for r in rows}) != 1
            or {(r["arm"], r["repeat"]) for r in rows}
                != {(arm, rep) for arm in ("fact", "episode", "lesson", "procedure") for rep in (1, 2)}):
        raise ContractError("Expected the eight original writer banks of one public experience")
    return dict(scope="Post hoc literal inventory of archived writing; no new generations or outcomes.",
                method_sha256=digest(Path(__file__).read_bytes()), comparison_completion_sha256=anchor["completion_sha256"],
                date_pattern=date.pattern, flight_pattern=flight.pattern, rows=rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    print(json.dumps(review(parser.parse_args().workspace.resolve()), ensure_ascii=False, indent=2))
