"""Execute fixed public decisions and reconstruct sealed single-response evidence."""
import argparse
from pathlib import Path

from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.experiments.multi_workflow import PROFILE, STUDY
from agent_memory_eval.experiments.replay_comparison import audited_allocations, live, preflight


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['preflight', 'live', 'audit', 'mask', 'profile-results'])
    parser.add_argument('--workspace', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--after-profile', action='store_true', help='Use the separately registered public-profile continuation')
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if args.after_profile:
        from agent_memory_eval.experiments.profile_step import PROFILE, STUDY
    profile = read_json((ROOT / PROFILE).read_bytes())
    if args.output and args.output.exists():
        parser.error('Use a new output path')
    if args.mode == 'profile-results' and not args.after_profile:
        parser.error('The profile continuation report requires --after-profile')
    if args.mode == 'live':
        result = live(workspace, profile, STUDY)
    elif args.mode == 'preflight':
        result, _ = preflight(workspace, profile, STUDY)
    else:
        result = audited_allocations(workspace, profile, STUDY)
        if args.mode == 'profile-results':
            from agent_memory_eval.experiments.profile_step import summarize
            result = summarize(result, profile)
        if args.mode == 'mask':
            texts = {}
            for row in result['rows']:
                score = row['score'] or {}
                if 'review_id' in score:
                    texts[score['review_id']] = dict(case=row['case'], text=score['public_text'])
            result = {k: texts[k] for k in sorted(texts)}
    if args.output:
        write_json(args.output, result)
    else:
        print({k: result[k] for k in ('scored_replies', 'http_calls', 'stop_reason', 'reported_usd') if k in result}
              or f'{len(result.get("slots", result))} entries')
