"""Preflight or run the remaining never-started assignments from the frozen comparison."""
import argparse
import json
from pathlib import Path

from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.fresh_continuation import continue_study

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = parser.parse_args()
    print(json.dumps(continue_study(ROOT, args.workspace.resolve(), live=args.live), indent=2))
