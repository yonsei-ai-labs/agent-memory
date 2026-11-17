"""Prepare all written-memory control inputs offline; this command has no live mode."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.writing_comparison import export_comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=ROOT / "protocols/drafts/tau2-written-memory-replay-v1.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = export_comparison(json.loads(args.profile.read_text(encoding="utf-8")), ROOT, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
