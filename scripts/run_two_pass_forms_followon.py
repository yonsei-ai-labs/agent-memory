"""Check or execute the 282 remaining two-pass episodes under the identified-504 amendment."""
import argparse
import json
from pathlib import Path

from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_followon import preflight, run_followon


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--live", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    result = preflight(workspace) if args.preflight else run_followon(read_json(args.live.read_bytes()), workspace, workspace / "data/tau2-bench")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
