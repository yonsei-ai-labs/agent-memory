"""Three-condition blocked autonomy pilot, with optional reads and one fixed bank."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.tau2_autonomy import audit_pilot
from agent_memory_eval.contracts.core import canonical, new_id, read_json
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner.tau2_autonomy import prepare_pilot, run_pilot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,default=Path("protocols/pilots/tau2-autonomous-memory-v1.1.json"))
    parser.add_argument("--checkout",type=Path,default=Path("data/tau2-bench"))
    parser.add_argument("--output",type=Path,required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live",action="store_true",help="Execute the bounded comparison using the declared source")
    mode.add_argument("--audit-only",action="store_true",help="Recompute saved evidence without writes or API calls")
    args = parser.parse_args()
    if args.audit_only:
        print(json.dumps(audit_pilot(args.output),ensure_ascii=False,indent=2))
        return
    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
        parser.error("Output must be new or empty")
    profile = read_json(args.config.read_bytes())
    prepared = prepare_pilot(profile,args.checkout)
    with Journal(args.output,new_id("autonomy-pilot")) as journal:
        summary = run_pilot(journal,profile,prepared,live=args.live)
    with (args.output/"pilot-summary.json").open("xb") as stream:
        stream.write(canonical(summary)+b"\n")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    if summary["status"] == "incomplete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
