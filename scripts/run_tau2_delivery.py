"""Inspect a draft locally, preflight a ready plan, execute, or audit frozen-bank delivery."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.tau2_delivery import audit_delivery
from agent_memory_eval.contracts.core import canonical, new_id, read_json
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner.tau2_delivery import compile_delivery, preflight_delivery, prepare_delivery, run_delivery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("protocols/drafts/tau2-delivery-comparison-v1.json"))
    parser.add_argument("--checkout", type=Path, default=Path("data/tau2-bench"))
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--preflight", action="store_true", help="Check ready local inputs with no network or model calls")
    mode.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()
    if args.audit_only:
        if args.output is None:
            parser.error("--audit-only requires --output")
        print(json.dumps(audit_delivery(args.output), ensure_ascii=False, indent=2))
        return
    profile = read_json(args.config.read_bytes())
    if not args.live:
        plan = preflight_delivery(profile, args.checkout)[0] if args.preflight else compile_delivery(profile, require_ready=False)
        status = "local_inputs_verified" if args.preflight else "configured" if plan["ready"] else "draft"
        print(json.dumps(dict(status=status, unresolved_fields=plan["unresolved_fields"],
            planned_runs=len(plan["blocks"]) * len(profile["conditions"]) if "blocks" in plan else None,
            conditions=profile["conditions"], benchmark_or_model_calls=0), ensure_ascii=False, indent=2))
        return
    if args.output is None:
        parser.error("--live requires --output")
    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
        parser.error("Output must be new or empty")
    prepared = prepare_delivery(profile, args.checkout)
    with Journal(args.output, new_id("delivery"), contract_version="1.2") as journal:
        summary = run_delivery(journal, profile, prepared)
    with (args.output / "delivery-summary.json").open("xb") as stream:
        stream.write(canonical(summary) + b"\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
