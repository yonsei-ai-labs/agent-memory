"""Reproduce one registered historical review in a detached worktree; never run models."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.reproduction.runner import (
    DEFAULT_REGISTRY,
    ReproductionError,
    load_entry,
    preflight,
    reproduce,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--inputs-root", type=Path, help="Default: repository root containing the original runs")
    parser.add_argument("--registry", type=Path, help="Default: protocols/reproductions/historical-v1.json")
    parser.add_argument("--output", type=Path, help="New directory for receipts and disposable worktree")
    parser.add_argument("--run", action="store_true", help="Execute the fixed review; default is preflight only")
    parser.add_argument("--keep-worktree", action="store_true", help="Keep copied inputs and environment for inspection")
    parser.add_argument("--timeout-seconds", type=float, default=1800)
    args = parser.parse_args()
    if args.run and args.output is None:
        parser.error("--run requires a new --output directory")
    try:
        repo = args.repo.resolve()
        config = load_entry(repo, args.registry or repo / DEFAULT_REGISTRY, args.experiment)
        if not args.run:
            print(json.dumps(preflight(config, args.inputs_root or repo), ensure_ascii=False, indent=2))
            return 0
        result = reproduce(config, args.inputs_root or repo, args.output,
            keep_worktree=args.keep_worktree, timeout_seconds=args.timeout_seconds,
            progress=lambda phase: print(json.dumps(dict(phase=phase)), flush=True))
        print(json.dumps(dict(status=result["status"], experiment=args.experiment,
            receipt=str(args.output.absolute() / "receipt.json"),
            source_inputs_preserved=result["source_inputs_preserved"], cleanup=result["cleanup"]),
            ensure_ascii=False, indent=2))
        return 0 if result["status"] == "passed" else 1
    except (ReproductionError, OSError, KeyError, ValueError) as error:
        print(json.dumps(dict(status="rejected", error_type=type(error).__name__, error=str(error)), ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
