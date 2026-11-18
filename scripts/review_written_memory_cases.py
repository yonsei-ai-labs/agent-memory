"""Validate source-bound public rubrics and calibrate against historical replies only."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.written_comparison import review_original_decisions
from agent_memory_eval.contracts.core import ContractError, digest, canonical, read_json
from agent_memory_eval.recording.journal import safe_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--rubrics", type=Path, required=True)
    args = parser.parse_args()
    preparation = read_json(safe_path(args.preparation, "preparation.json").read_bytes())
    if (preparation.get("format") != "agent-memory-written-replay-preparation/v2"
            or digest(canonical(preparation["case_review_packet"])) != preparation["case_review_packet_sha256"]):
        raise ContractError("Expected an intact v2 case review packet; audit the preparation against sources first")
    result = review_original_decisions(preparation["case_review_packet"], read_json(args.rubrics.read_bytes()))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
