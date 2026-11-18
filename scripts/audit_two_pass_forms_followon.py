"""Audit the three sealed forms batches and publish a read-only combined analysis view."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.forms_followon import PROFILE, audit
from agent_memory_eval.contracts.validation import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--profile", type=Path, help="Frozen follow-on ready JSON under the evidence workspace by default")
    parser.add_argument("--output", type=Path, required=True, help="New directory outside all three source batches")
    parser.add_argument("--completion-sha256", help="Independent SHA-256 of the new batch completion file")
    parser.add_argument("--profile-sha256", help="Independent SHA-256 of the follow-on ready profile")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    receipt = audit(workspace, args.profile or workspace / PROFILE, args.output, expected_completion=args.completion_sha256,
                    expected_profile=args.profile_sha256)
    print(json.dumps({key: receipt[key] for key in ("status", "previous_as_sealed", "followon", "combined")}, indent=2))


if __name__ == "__main__":
    main()
