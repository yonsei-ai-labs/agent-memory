"""Preflight or run the frozen own-source two-pass airline experiment."""
import argparse
import json
from pathlib import Path

from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import DESIGN, prepare, run_batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT, help="Workspace containing archived sources and raw runs")
    parser.add_argument("--checkout", type=Path, help="Pinned tau2 checkout (default workspace/data/tau2-bench)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true", help="Reconstruct sources and check native configurations without HTTP")
    mode.add_argument("--live", type=Path, metavar="READY_PROFILE", help="Execute the separate, committed-runtime ready profile")
    mode.add_argument("--audit", type=Path, metavar="RUN", help="Recompute the paired results from the archived journals offline")
    args = parser.parse_args()
    checkout = args.checkout or args.workspace / "data/tau2-bench"
    if args.audit:
        from agent_memory_eval.analysis.forms_results import summarize
        result = summarize(args.audit)
    elif args.live:
        result = run_batch(read_json(args.live.read_bytes()), args.workspace, checkout)
    else:
        result = prepare(ROOT, args.workspace.resolve(), checkout, read_json((ROOT / DESIGN).read_bytes()))["summary"]
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
