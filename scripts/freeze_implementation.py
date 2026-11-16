"""Record a clean implementation commit before freezing a new experimental protocol."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.contracts.core import canonical
from agent_memory_eval.reproduction.freeze import implementation_identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.resolve().is_relative_to((args.root.resolve() / "runs")):
        parser.error("Freeze metadata must be outside preserved runs")
    result = implementation_identity(args.root)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as stream:
            stream.write(canonical(result) + b"\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
