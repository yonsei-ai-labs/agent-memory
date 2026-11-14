"""Pinned no-memory OpenRouter/tau2 connection check, separate from research plans."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.adapters.tau2 import prepare_native, run_native_check
from agent_memory_eval.analysis.connection import audit_connection
from agent_memory_eval.contracts.core import canonical, new_id
from agent_memory_eval.recording.journal import Journal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("protocols/checks/openrouter-tau2-smoke.json"))
    parser.add_argument("--checkout", type=Path, default=Path("data/tau2-bench"))
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Make the bounded paid calls in this connection profile")
    mode.add_argument("--audit-only", action="store_true", help="Read existing evidence without API calls or writes")
    args = parser.parse_args()
    if args.audit_only:
        print(json.dumps(audit_connection(args.output), ensure_ascii=False, indent=2))
        return
    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
        parser.error("Output must be new or empty")
    config = json.loads(args.config.read_text())
    if args.live and config.get("profile_version") == "1.0":
        parser.error("The historical GPT-4.1 profile is not enabled for live calls; use the current DeepSeek profile")
    prepared = prepare_native(config, args.checkout)
    with Journal(args.output, new_id("connection")) as journal:
        summary = run_native_check(journal, config, prepared, live=args.live)
    with (args.output / "connection-summary.json").open("xb") as stream:
        stream.write(canonical(summary) + b"\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["status"] == "error":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
