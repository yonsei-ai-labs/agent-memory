"""Preflight or execute a user-configured pair of writing prompts."""
import argparse
import json
from pathlib import Path

from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.experiments.prompt_comparison import run_comparison
from agent_memory_eval.experiments.prompt_delivery import run_delivery_comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--checkout", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true", help="Check sources, native setup, prompts and scheduling offline")
    mode.add_argument("--live", action="store_true", help="Execute in a clean frozen checkout with per-request spend guards")
    args = parser.parse_args()
    runner = (run_delivery_comparison if read_json(args.config.read_bytes()).get("format") == "agent-memory-prompt-delivery/v1"
              else run_comparison)
    result = runner(ROOT, args.workspace, args.checkout or args.workspace / "data/tau2-bench", args.config, live=args.live)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
