"""Verify two sealed batches offline and build a read-only view for the existing forms analysis tools."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.forms_supplement import PROFILE, audit
from agent_memory_eval.contracts.validation import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT, help="Original repository-shaped evidence root")
    parser.add_argument("--profile", type=Path, help="Frozen supplement ready JSON; defaults to the workspace's approved profile")
    parser.add_argument("--output", type=Path, required=True, help="New analysis directory outside both raw evidence roots")
    parser.add_argument("--supplement-completion-sha256", help="Independently pinned digest of the supplement completion file")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    result = audit(workspace, args.profile or workspace / PROFILE, args.output,
                   expected_completion=args.supplement_completion_sha256)
    print(json.dumps({k: result[k] for k in ("status", "original", "supplement", "combined")}, indent=2))


if __name__ == "__main__":
    main()
