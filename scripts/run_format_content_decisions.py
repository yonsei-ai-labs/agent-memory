"""Run the matched-content and historical-bank study, or audit its blinded and joined results."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.format_content import review_packet, summarize
from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.format_content import PROFILE, STUDY
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.experiments.replay_comparison import audited_allocations, live, preflight


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "live", "mask", "analyze"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--review", type=Path, help="Completed arm-masked numeric-meaning annotations for analyze")
    parser.add_argument("--result", type=Path, help="Write to a new JSON file")
    args = parser.parse_args()
    if (args.result and (args.mode == "live" or args.result.exists())) or ((args.mode == "analyze") != bool(args.review)):
        parser.error("Use a new output in read-only modes. Supply --review exactly in analyze mode.")
    workspace, profile = args.workspace.resolve(), read_json((ROOT / PROFILE).read_bytes())
    if args.mode == "preflight":
        result, _ = preflight(workspace, profile, STUDY)
    elif args.mode == "live":
        result = live(workspace, profile, STUDY)
    else:
        raw = audited_allocations(workspace, profile, STUDY)
        result = review_packet(raw, profile) if args.mode == "mask" else summarize(raw, profile, read_json(args.review.read_bytes()))
    if args.result:
        write_json(args.result, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
