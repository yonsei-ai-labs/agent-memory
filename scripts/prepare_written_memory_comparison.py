"""Prepare all written-memory control inputs offline; this command has no live mode."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.experiments.writing_comparison import audit_comparison, export_comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=ROOT / "protocols/drafts/tau2-written-memory-replay-v2.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit-only", action="store_true", help="Reconstruct from sources and compare an existing export without writing")
    args = parser.parse_args()
    operation = audit_comparison if args.audit_only else export_comparison
    result = operation(read_json(args.profile.read_bytes()), ROOT, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
