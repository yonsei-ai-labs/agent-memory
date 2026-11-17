"""Inspect or run a bounded writing-only exploration; no benchmark actions or replay calls."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.writing_exploration import audit_writing
from agent_memory_eval.contracts.core import canonical, read_json
from agent_memory_eval.experiments.writing import preflight_writing, run_writing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("protocols/drafts/tau2-memory-writing-exploration-v1.json"))
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="Check recorded source and initial request bytes without HTTP")
    mode.add_argument("--live", action="store_true", help="Requires a ready profile and a clean committed checkout")
    mode.add_argument("--audit-only", action="store_true", help="Read saved evidence without source files, keys or HTTP")
    args = parser.parse_args()
    if args.audit_only:
        if args.output is None:
            parser.error("--audit-only requires --output")
        result = audit_writing(args.output)
    else:
        profile = read_json(args.config.read_bytes())
        if args.live:
            if args.output is None:
                parser.error("--live requires a new --output directory")
            run_writing(profile, args.output, Path.cwd())
            result = audit_writing(args.output)
            (args.output / "writing-summary.json").write_bytes(canonical(result) + b"\n")
        else:
            result = preflight_writing(profile, Path.cwd())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.live and (result["status"] != "completed" or result["errors"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
