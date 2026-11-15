"""Inspect, locally preflight, execute or audit an explicitly configured no-memory batch."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.tau2_baseline import audit_baseline
from agent_memory_eval.contracts.core import canonical, digest, new_id, read_json
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner.tau2_baseline import compile_baseline, preflight_baseline, prepare_baseline, run_baseline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("protocols/drafts/tau2-baseline-screening-v1.json"))
    parser.add_argument("--checkout", type=Path, default=Path("data/tau2-bench"))
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="Local checks only, including an unready draft")
    mode.add_argument("--live", action="store_true", help="Run the ready profile with bounded paid calls")
    mode.add_argument("--audit-only", action="store_true", help="Read evidence without calls, checkout or writes")
    args = parser.parse_args()
    if args.audit_only:
        if args.output is None:
            parser.error("--audit-only requires --output")
        print(json.dumps(audit_baseline(args.output), ensure_ascii=False, indent=2))
        return
    profile = read_json(args.config.read_bytes())
    if not args.live:
        plan = preflight_baseline(profile, args.checkout)[0] if args.preflight else compile_baseline(profile, require_ready=False)
        print(json.dumps(dict(status="ready" if plan["ready"] else "draft", unresolved_fields=plan["unresolved_fields"],
            local_sources_verified=args.preflight, tasks=len(plan["task_ids"]) if args.preflight else None,
            planned_runs=len(plan["allocations"]) if "allocations" in plan else None,
            schedule_sha256=digest(canonical(plan["allocations"])) if "allocations" in plan else None,
            benchmark_or_model_calls=0), ensure_ascii=False, indent=2))
        return
    compile_baseline(profile)
    if args.output is None:
        parser.error("--live requires --output")
    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
        parser.error("Output must be new or empty")
    prepared = prepare_baseline(profile, args.checkout)
    with Journal(args.output, new_id("baseline")) as journal:
        summary = run_baseline(journal, profile, prepared)
    with (args.output / "baseline-summary.json").open("xb") as stream:
        stream.write(canonical(summary) + b"\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
