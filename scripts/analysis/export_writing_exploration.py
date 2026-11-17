"""Export audited writing metrics and condition-hidden memories without model calls."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.writing_export import export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.run, args.output)))


if __name__ == "__main__":
    main()
