"""Print the request-local evidence and public actions leading up to a worker decision, offline."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.selection import worker_choices


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path, help="Directory containing the journal log.jsonl")
    parser.add_argument("--run", help="Worker run ID for a shared journal")
    parser.add_argument("--through", help="Last worker request to inspect, inclusive")
    args = parser.parse_args()
    print(json.dumps(worker_choices(args.journal, run_id=args.run, through_request=args.through),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
