"""Reproduce the fixed 20261115 no-memory screening review; never call models.

Default mode verifies the saved receipt and descriptive analysis.
--write-new creates both artifacts once. Public metrics and any manual annotations
are analysis-only; they must not become worker/writer inputs.
"""

import argparse
import ast
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from agent_memory_eval.analysis.tau2_baseline import audit_baseline
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.recording.journal import BlobStore


ROOT = Path(__file__).resolve().parents[1]
LOCK = Path("artifacts/pilots/20261115-tau2-baseline-screening-protocol.json")
RECEIPT = Path("artifacts/pilots/20261115-tau2-baseline-screening-results.json")
ANALYSIS = Path("artifacts/analyses/20261115-tau2-baseline-behavior-review.json")


def reference(path, root=ROOT):
    return dict(path=str(path.relative_to(root)), sha256=digest(path.read_bytes()))


def write_tool_names(source):
    """Read pinned native tool annotations rather than labeling arbitrary names as writes."""
    names = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name)
                    and decorator.func.id == "is_tool" and decorator.args
                    and isinstance(decorator.args[0], ast.Attribute) and decorator.args[0].attr == "WRITE"):
                names.append(node.name)
    return sorted(names)


def public_message(message):
    """Project only ordinary native message fields; exclude raw provider/hidden data."""
    selected = {k: deepcopy(message[k]) for k in ("role", "content", "id", "error") if k in message}
    if message.get("tool_calls"):
        selected["tool_calls"] = [{k: deepcopy(c[k]) for k in ("id", "name", "arguments") if k in c}
                                  for c in message["tool_calls"]]
    return selected


def public_metrics(report, writes):
    messages = report.get("messages", report.get("partial_trajectory", []))
    calls, identifiers, errors = [], Counter(), []
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                identity = (call["name"], digest(canonical(call["arguments"])))
                identifiers[identity] += 1
                calls.append(dict(message_index=index, call_id=call["id"], name=call["name"], arguments_sha256=identity[1]))
        if message.get("role") == "tool" and message.get("error"):
            errors.append(index)
    names = Counter(call["name"] for call in calls)
    return dict(public_message_count=len(messages), business_tool_calls=len(calls), tool_counts=dict(names),
        write_calls=sum(n for name, n in names.items() if name in writes),
        transfer_calls=names["transfer_to_human_agents"], tool_error_message_indices=errors,
        identical_call_repeats=sum(count - 1 for count in identifiers.values()), calls=calls)


def build_review(root, annotations, repo=ROOT):
    lock_path = repo / LOCK
    lock = read_json(lock_path.read_bytes())
    refs = lock["implementation_files"] + [lock[k] for k in ("profile_ref", "protocol_note_ref", "base_connection_ref")]
    for ref in refs:
        assert digest((repo / ref["path"]).read_bytes()) == ref["sha256"], ref["path"]
    audit = audit_baseline(root)
    assert audit["http_calls"] > 0
    blobs = BlobStore(root)
    raw_log = (root / "log.jsonl").read_bytes()
    assert digest(raw_log) == audit["log_sha256"], "Original log changed after the strict audit"
    records = [read_json(line) for line in raw_log.splitlines()]
    state = blobs.get(records[-1]["state_ref"])
    control = blobs.get(state["baseline_screening"]["control_ref"])
    assert digest(canonical([{k: v for k, v in r.items() if k not in {"run_id", "session_id"}}
                             for r in control["schedule"]])) == lock["schedule_sha256"]
    assert canonical(control["profile"]) == canonical(read_json((repo / lock["profile_ref"]["path"]).read_bytes()))
    assert len(control["schedule"]) == lock["planned_runs"]
    for path, sha in lock["benchmark_asset_hashes"].items():
        assert control["assets"][path]["sha256"] == sha
    source = blobs.get(control["assets"]["src/tau2/domains/airline/tools.py"]["text_ref"])
    writes = write_tool_names(source)
    task_path = "data/tau2/domains/airline/tasks.json"
    tasks = {t["id"]: t for t in read_json(blobs.get(control["assets"][task_path]["text_ref"]).encode())}
    annotations = deepcopy(annotations)
    assert len({a["run_id"] for a in annotations}) == len(annotations)
    assert {a["run_id"] for a in annotations} <= {r["run_id"] for r in audit["runs"]}
    manual = {a["run_id"]: a for a in annotations}
    rows, by_task = [], {}
    for run in audit["runs"]:
        task_id = run["task_id"]
        item = by_task.setdefault(task_id, dict(task_id=task_id, planned=0, started=0, graded=0,
            successful=0, skipped=0, ungraded=0, bounded=0, runs=[], reference_write_tools=[a["name"]
                for a in tasks[task_id]["evaluation_criteria"].get("actions", []) if a["name"] in writes]))
        item["planned"] += 1
        item["started"] += run["status"] != "skipped"
        item["graded"] += run["grading_status"] == "graded"
        item["successful"] += run["official_success"] is True
        item["skipped"] += run["status"] == "skipped"
        item["ungraded"] += run["status"] == "error"
        item["bounded"] += run["bounded"]
        item["runs"].append(run["run_id"])
        row = deepcopy(run)
        if run.get("report_ref") is not None:
            report = blobs.get(run["report_ref"])
            row["public_metrics"] = public_metrics(report, writes)
            row["reward_info"] = report.get("reward_info")
            if run["run_id"] in manual:
                note = manual[run["run_id"]]
                messages = report.get("messages", report.get("partial_trajectory", []))
                indices = note["support_message_indices"]
                assert len(set(indices)) == len(indices)
                assert all(type(i) is int and 0 <= i < len(messages) for i in indices)
                row["manual_review"] = dict(note, evidence=[dict(message_index=i, message_sha256=digest(canonical(messages[i])),
                    public_message=public_message(messages[i])) for i in indices])
        else:
            assert run["run_id"] not in manual
        rows.append(row)
    analysis = dict(format="agent-memory-tau2-baseline-behavior-review/v1", visibility="analysis_only_never_worker_or_writer_input",
        method="frozen_native_audit_descriptive_public_metrics_and_optional_single_unblinded_manual_review",
        method_ref=reference(Path(__file__).resolve(), repo), protocol_lock_ref=reference(lock_path, repo),
        source=dict(root=str(root.relative_to(repo)), log_sha256=audit["log_sha256"], summary_sha256=audit["summary_sha256"]),
        write_tool_names=writes, metric_limits=dict(identical_repeats="Descriptive; repeats can be legitimate rechecks.",
            write_calls="Native WRITE annotation, not proof of correct DB state or policy compliance.",
            official_score="Unchanged native grading; explanation and scenario coverage remain separate."),
        tasks=[by_task[k] for k in sorted(by_task, key=int)], runs=rows, manual_annotations=annotations)
    receipt = dict(format="agent-memory-tau2-baseline-results/v1", protocol_lock_ref=analysis["protocol_lock_ref"],
        audit=audit, tasks=analysis["tasks"], behavior_review_ref=dict(path=str(ANALYSIS), sha256=digest(canonical(analysis) + b"\n")),
        inference="Development screening with two repetitions; no task-difficulty certainty or memory causal effect.")
    return receipt, analysis


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-new", action="store_true")
    parser.add_argument("--annotations", type=Path)
    args = parser.parse_args()
    receipt_path, analysis_path = ROOT / RECEIPT, ROOT / ANALYSIS
    lock = read_json((ROOT / LOCK).read_bytes())
    root = ROOT / lock["planned_output"]
    if args.write_new:
        assert not receipt_path.exists() and not analysis_path.exists(), "Never overwrite a past analysis or receipt"
        annotations = read_json(args.annotations.read_bytes()) if args.annotations else []
    else:
        assert args.annotations is None, "Default verification uses the preserved annotations"
        annotations = read_json(analysis_path.read_bytes())["manual_annotations"]
    receipt, analysis = build_review(root, annotations)
    if args.write_new:
        for path, value in ((analysis_path, analysis), (receipt_path, receipt)):
            with path.open("xb") as stream:
                stream.write(canonical(value) + b"\n")
    else:
        assert canonical(receipt) == canonical(read_json(receipt_path.read_bytes())), "Receipt differs from original evidence"
        assert canonical(analysis) == canonical(read_json(analysis_path.read_bytes())), "Analysis differs from original evidence"
    print(json.dumps(dict(status="review_passed", counts=receipt["audit"]["counts"],
        cost_usd=receipt["audit"]["reported_cost_usd_decimal"], task_patterns=dict(Counter(
            f"{t['successful']}/{t['graded']}" for t in receipt["tasks"])),
        manual_reviews=len(annotations), benchmark_or_model_calls=0), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
