"""Stage, execute or independently audit the four-arm written-memory replay batch."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.written_replay_batch import audit_batch
from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.written_replay_batch import run_batch, stage_batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Batch profile; optional external anchor in audit mode")
    parser.add_argument("--preparation", type=Path, help="Source-verified v2 preparation export")
    parser.add_argument("--output", type=Path, help="New journal, or existing journal for --audit-only")
    parser.add_argument("--details", action="store_true", help="Include every shared response and logical assignment")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="Validate all inputs locally in temporary storage (default)")
    mode.add_argument("--stage", action="store_true", help="Keep a portable local input journal without HTTP")
    mode.add_argument("--live", action="store_true", help="Submit only under a separate ready batch profile")
    mode.add_argument("--audit-only", action="store_true", help="Read an archived journal without sources or HTTP")
    args = parser.parse_args()
    if (args.stage or args.live or args.audit_only) and args.output is None:
        parser.error("This mode requires --output")
    if args.audit_only:
        profile = read_json(args.config.read_bytes()) if args.config else None
        result = audit_batch(args.output, expected_profile=profile)
    else:
        if args.preparation is None:
            parser.error("Select the v2 preparation with --preparation")
        if args.output and not (args.live or args.stage):
            parser.error("Use --stage to keep the preflight journal")
        config = args.config or ROOT / "protocols/drafts/tau2-written-memory-batch-v1.json"
        profile = read_json(config.read_bytes())
        if args.live:
            run_batch(profile, ROOT, args.preparation, args.output)
            result = audit_batch(args.output, expected_profile=profile)
        else:
            result = stage_batch(profile, ROOT, args.preparation, args.output)
    if not args.details:
        result = {k:v for k, v in result.items() if k not in {"allocations", "distinct_inputs", "input_groups"}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.live and result["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
