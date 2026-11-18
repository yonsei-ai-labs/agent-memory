"""Check or execute the approved 308 unstarted full episodes of the two-pass forms study."""
import argparse
import json
from pathlib import Path

from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_supplement import preflight, run_supplement


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--checkout", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--live", type=Path, metavar="READY_PROFILE")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    result = (preflight(workspace) if args.preflight else
              run_supplement(read_json(args.live.read_bytes()), workspace, args.checkout or workspace / "data/tau2-bench"))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
