"""Check both preassigned blocks of task 36 and preserve the reasons for all unavailable official grades."""
import argparse
from collections import Counter, defaultdict
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.journal import BlobStore, safe_path


def handoff_trace(row, blobs, phrase):
    result = row["result"]
    ref = result.get("native_report_ref") or result.get("partial_trajectory_ref")
    report = blobs.get(ref) if ref else []
    messages = report["messages"] if isinstance(report, dict) else report
    public = [m for m in messages if m.get("role") in {"assistant", "tool", "user"}]
    events = []
    for index, msg in enumerate(public):
        if msg["role"] != "tool" or msg.get("content") != "Transfer successful":
            continue
        later = [(j, m) for j, m in enumerate(public[index+1:], index + 1)
                 if m["role"] in {"assistant", "user"} and isinstance(m.get("content"), str) and m["content"]]
        worker = next(((i, m["content"]) for i, m in later if m["role"] == "assistant"), None)
        user = next(((i, m["content"]) for i, m in later if worker and i > worker[0] and m["role"] == "user"), None)
        events.append(dict(tool_result_index=index,
            first_following_worker=dict(index=worker[0], text=worker[1], utf8_sha256=digest(worker[1].encode()),
                                       contains_prescribed_phrase=phrase in worker[1]) if worker else None,
            next_customer=dict(index=user[0], text=user[1]) if user else None))
    markers = [dict(index=i, text=m["content"]) for i, m in enumerate(public) if m["role"]=="user"
               and any(token in (m.get("content") or "") for token in ("###TRANSFER###", "###STOP###", "###OUT-OF-SCOPE###"))]
    return dict(public_trajectory_sha256=ref["sha256"] if ref else None, successful_tool_handoffs=events,
                customer_end_markers=markers, public_message_count=len(public))


def summarize(run, specification):
    seal = read_json(safe_path(run, "completion.json").read_bytes())
    ledger = (run / "allocations.json").read_bytes()
    if digest(ledger) != seal["allocation_sha256"]:
        raise ContractError("Handoff traces require the sealed assignments")
    spec = read_json(specification.read_bytes())
    if spec.get("format") != "agent-memory-two-pass-handoff-comparison-plan/v1" or spec["task_id"] != "36":
        raise ContractError("Unexpected handoff comparison scope")
    rows = [r for r in read_json(ledger) if r["phase"] == "solve"]
    unavailable, cases, groups = [], [], defaultdict(list)
    for row in rows:
        result = row.get("result", {})
        if result.get("grading_status") == "graded" and row["task_id"] != spec["task_id"]:
            continue
        folder = safe_path(run, row["directory"])
        blobs = BlobStore(folder)
        if result and digest((folder / "log.jsonl").read_bytes()) != result["log_sha256"]:
            raise ContractError("Source journal differs from the sealed score row")
        info = {k: row[k] for k in ("index", "task_id", "repetition", "arm", "run_id", "status")}
        info.update(grading_status=result.get("grading_status"), official_success=result.get("official_success"),
                    native_steps=result.get("native_steps"), termination=result.get("termination_reason"),
                    error_code=result.get("error_code"), skip_reason=row.get("reason"), http_calls=result.get("http_calls"),
                    log_sha256=result.get("log_sha256"))
        if result.get("grading_status") != "graded":
            ref = result.get("reached_state_assessment_ref")
            assessment = blobs.get(ref) if ref else {}
            db = (assessment.get("db_component") or {}).get("db_check") or {}
            if info["official_success"] is not None:
                raise ContractError("Prevent an ungraded or unstarted allocation from acquiring a score")
            unavailable.append(dict(**info, diagnostic_db_match=db.get("db_match"), diagnostic_ref_sha256=ref["sha256"] if ref else None))
        if row["task_id"] == spec["task_id"]:
            trace = handoff_trace(row, blobs, spec["prescribed_public_phrase"]) if result else {}
            cases.append(dict(**info, **trace))
            handoffs = trace.get("successful_tool_handoffs", [])
            first = handoffs[0]["first_following_worker"] if handoffs else None
            if first:
                groups[row["repetition"], first["utf8_sha256"]].append(row["index"])
    if len(cases) != 12 or len({(r["repetition"], r["arm"]) for r in cases}) != 12:
        raise ContractError("All twelve original task-36 cells must be represented")
    return dict(format="agent-memory-two-pass-handoffs-and-ungraded/v1", plan_sha256=digest(specification.read_bytes()),
                allocation_sha256=seal["allocation_sha256"], task_36=cases,
                equal_first_post_transfer_worker_text=[dict(repetition=k[0],utf8_sha256=k[1],jobs=v) for k,v in sorted(groups.items())],
                source_of_unavailable_grade=dict(Counter(r["error_code"] or r["skip_reason"] for r in unavailable)),
                unavailable=unavailable)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("run","specification","output"):
        parser.add_argument(name,type=Path)
    args=parser.parse_args()
    if args.output.exists() or args.output.absolute().is_relative_to(args.run.absolute()):
        raise ContractError("Use a new path outside the archived runs")
    write_json(args.output,summarize(args.run,args.specification))


if __name__=="__main__":
    main()
