"""Run and audit a fresh synthetic example; never call a model or benchmark API."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.offline import summarize
from agent_memory_eval.contracts.core import canonical, new_id
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner.offline import OfflineRunner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="A new or empty directory; existing runs are never overwritten")
    parser.add_argument("--analyze-only", action="store_true", help="Read an existing journal without rerunning")
    args = parser.parse_args()
    if not args.analyze_only:
        if args.output.exists() and any(args.output.iterdir()):
            parser.error("Output must be new or empty; use --analyze-only to read existing data")
        with Journal(args.output, new_id("illustrative")) as journal:
            OfflineRunner(journal).two_pass()
    summary = summarize(args.output)
    if not args.analyze_only:
        revision = args.output / "analysis" / new_id("synthetic")
        revision.mkdir(parents=True, exist_ok=False)
        with (revision / "summary.json").open("xb") as stream:
            stream.write(canonical(summary) + b"\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
