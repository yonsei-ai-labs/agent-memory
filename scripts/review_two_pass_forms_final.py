"""Reproduce the three-root forms evaluation and its diagnostic tables from sealed input copies."""
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from export_two_pass_forms_cells import cells
from export_two_pass_forms_diagnostics import extract
from regrade_two_pass_forms import regrade
from summarize_two_pass_transitions import summarize as transitions
from trace_two_pass_handoffs import summarize as handoffs

from agent_memory_eval.analysis.forms_followon import audit
from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.assets import asset_text


ROOT = Path(__file__).resolve().parents[1]
LOCK = "artifacts/pilots/20261118-two-pass-forms-final-reproduction-lock.json"


def review(root):
    lock = read_json((root / LOCK).read_bytes())
    if lock["format"] != "agent-memory-two-pass-forms-final-reproduction-lock/v1":
        raise ContractError("Unexpected three-root reproduction lock")
    profile = lock["followon_profile_ref"]
    asset_text(root, profile)
    with TemporaryDirectory() as temporary:
        output = Path(temporary) / "audit"
        receipt = audit(root, root / profile["path"], output, expected_completion=lock["followon_completion_sha256"],
                        expected_profile=profile["sha256"])
        if {k: r["completion_sha256"] for k, r in receipt["sources"].items()} != lock["source_seals"]:
            raise ContractError("Study source seals differ from the reproduction lock")
        view = output / "combined-view"
        scores = regrade(view, root, root / "data/tau2-bench")
        if (scores["official_results_checked"] != receipt["combined"]["graded_episodes"]
                or scores["successes"] != receipt["combined"]["successful_episodes"]):
            raise ContractError("Strict replay and recorded scores differ")
        write_json(output / "native-regrade.json", scores)
        writing = read_json(asset_text(root, lock["writing_review_ref"]).encode())
        table, empty = cells(view, writing)
        with (output / "second-pass.csv").open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows(table)
        write_json(output / "empty-forms.json", empty)
        write_json(output / "transitions.json", transitions(output / "second-pass.csv"))
        write_json(output / "diagnostics.json", extract(view))
        asset_text(root, lock["handoff_plan_ref"])
        write_json(output / "handoffs.json", handoffs(view, root / lock["handoff_plan_ref"]["path"]))
        files = {"followon-results.json": "followon_results_ref", "combined-results.json": "combined_results_ref",
                 "audit.json": "audit_ref", "episode-origins.csv": "origins_ref", "native-regrade.json": "native_regrade_ref",
                 "second-pass.csv": "cells_ref", "empty-forms.json": "empty_ref", "transitions.json": "transitions_ref",
                 "diagnostics.json": "diagnostics_ref", "handoffs.json": "handoffs_ref"}
        verified = {}
        for name, label in files.items():
            actual = (output / name).read_bytes()
            if actual != asset_text(root, lock[label]).encode():
                raise ContractError("Reproduction differs from published " + name)
            verified[name] = digest(actual)
        return dict(status="passed", source_seals=receipt["sources"], files=verified,
                    verified_input_files=receipt["input_file_count"], combined=receipt["combined"],
                    strict_replay={k: scores[k] for k in ("official_results_checked", "successes", "failure_components",
                                                          "new_model_or_network_attempts", "benchmark_revision")})


if __name__ == "__main__":
    print(json.dumps(review(ROOT), indent=2, ensure_ascii=False))
