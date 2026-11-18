"""Audit recorded single replies; no model, catalog or business tool calls."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.snapshot_replay import audit_snapshot_replays
from agent_memory_eval.analysis.written_comparison import review_recorded_replies
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.recording.journal import safe_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preparation", type=Path, help="Optional v2 preparation directory for source-bound public checks")
    parser.add_argument("--rubrics", type=Path, help="Rubrics bound to the preparation's public packet")
    args = parser.parse_args()
    if (args.preparation is None) != (args.rubrics is None):
        parser.error("--preparation and --rubrics must be provided together")
    if args.preparation is None:
        result = audit_snapshot_replays(args.output)
    else:
        preparation = read_json(safe_path(args.preparation, "preparation.json").read_bytes())
        packet = preparation["case_review_packet"]
        if (preparation.get("format") != "agent-memory-written-replay-preparation/v2"
                or digest(canonical(packet)) != preparation["case_review_packet_sha256"]):
            raise ContractError("Expected an intact v2 public review packet")
        result = review_recorded_replies(args.output, packet, read_json(args.rubrics.read_bytes()))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
