"""Inspect or run a bounded single-reply diagnostic; never execute business tools."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.contracts.core import canonical, new_id, read_json
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner.decision_replay import audit_replay, compile_replay, preflight_replay, prepare_replay, run_replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("protocols/drafts/tau2-decision-replay-v1.json"))
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="Verify original raw requests locally; no HTTP")
    mode.add_argument("--live", action="store_true", help="Requires a separately completed, ready protocol")
    mode.add_argument("--audit-only", action="store_true", help="Read saved evidence without calls or original source runs")
    args = parser.parse_args()
    if args.audit_only:
        if args.output is None:
            parser.error("--audit-only requires --output")
        result = audit_replay(args.output)
    else:
        profile = read_json(args.config.read_bytes())
        if not args.live:
            plan = preflight_replay(profile) if args.preflight else compile_replay(profile)
            print(json.dumps(dict(status="ready" if plan["ready"] else "draft", unresolved_fields=plan["unresolved_fields"],
                cases=8, proposed_repetitions=profile["repetitions"], proposed_requests=8 * 3 * profile["repetitions"] if profile["repetitions"] else None,
                original_requests_verified=args.preflight, memo_author_kind=plan["memos"]["author_kind"],
                worker_generated_memos=False, business_tools_executed=0, benchmark_or_model_calls=0), ensure_ascii=False, indent=2))
            return
        compile_replay(profile, require_ready=True)
        if args.output is None:
            parser.error("--live requires --output")
        if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
            parser.error("Output must be new or empty")
        prepared = prepare_replay(profile)
        with Journal(args.output, new_id("decision_replay")) as journal:
            result = run_replay(journal, profile, prepared)
        with (args.output / "decision-replay-summary.json").open("xb") as stream:
            stream.write(canonical(result) + b"\n")
    print(json.dumps({k: v for k, v in result.items() if k != "runs"}, ensure_ascii=False, indent=2))
    if result["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
