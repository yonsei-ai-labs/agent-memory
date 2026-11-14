"""Guided native memory connection check; separate from policy-effect experiments."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.tau2_memory import audit_memory_check
from agent_memory_eval.contracts.core import canonical, new_id
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner.tau2_memory import prepare_memory_check, run_memory_check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("protocols/checks/openrouter-tau2-memory-smoke.json"))
    parser.add_argument("--checkout", type=Path, default=Path("data/tau2-bench"))
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Run the bounded paid memory connection check")
    mode.add_argument("--audit-only", action="store_true", help="Check saved evidence without writes or API calls")
    args = parser.parse_args()
    if args.audit_only:
        print(json.dumps(audit_memory_check(args.output),ensure_ascii=False,indent=2))
        return
    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
        parser.error("Output must be new or empty")
    profile = json.loads(args.config.read_text())
    prepared = prepare_memory_check(profile,args.checkout)
    with Journal(args.output,new_id("memory-connection")) as journal:
        summary = run_memory_check(journal,profile,prepared,live=args.live)
    with (args.output/"memory-summary.json").open("xb") as stream:
        stream.write(canonical(summary)+b"\n")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    if summary["status"] == "error":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
