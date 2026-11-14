"""Print a versioned action-analysis supplement without writes or model calls."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.action_comparison import analyze_pilot_actions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Original completed or explicitly skipped pilot")
    args = parser.parse_args()
    print(json.dumps(analyze_pilot_actions(args.input), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
