"""Publish a linked offline report for a prompt comparison or the archived four-form study."""
import argparse
from pathlib import Path

from agent_memory_eval.analysis.prompt_report import publish_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--annotations", type=Path, help="Optional prior outcome-masked paired judgments")
    args = parser.parse_args()
    result = publish_report(args.run, args.output, annotations=args.annotations)
    print(args.output / "index.md", result["outcomes"], "pairs", result["paired_case_pages"])


if __name__ == "__main__":
    main()
