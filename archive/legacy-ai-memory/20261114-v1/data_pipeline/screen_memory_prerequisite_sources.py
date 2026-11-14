"""Bounded read-only discovery of prerequisite failures in a pinned public log.

Outputs are review candidates, not automatically labeled training examples.
Source commands are data and are never executed.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import pyarrow.parquet as pq
from data_pipeline.scope_expansion import PUBLIC_REVISION

PATTERNS = {
    'missing_executable': r'(?im)(command not found|(?:bash|sh):[^\n]{0,100}: not found)',
    'missing_import': r'(ModuleNotFoundError|ImportError: (?:No module named|cannot import name))',
    'runner_contract': r'(?im)(unrecognized arguments|ERROR: (?:file or directory not found|not found:|usage:)|no tests ran)',
    'initialization': r'(ImproperlyConfigured|AppRegistryNotReady|settings are not configured|Apps aren.t loaded yet)',
}


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def write(path, value): path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
def content(message):
    value = message.get('content') or ''
    return value if isinstance(value, str) else '\n'.join(x.get('text', '') for x in value)


def scan(parquet, plan_path, output):
    if output.exists(): raise FileExistsError(output)
    plan = json.loads(plan_path.read_text())
    if sha(parquet) != plan['candidate_parquet_sha256']: raise ValueError('Public source shard changed')
    start, end = plan['candidate_row_range_inclusive']; candidates = []; rows_reviewed = 0; repositories = Counter(); offset = 0
    for batch in pq.ParquetFile(parquet).iter_batches(batch_size=128, columns=plan['loaded_columns']):
        if offset > end: break
        if offset + batch.num_rows <= start: offset += batch.num_rows; continue
        for index, source in enumerate(batch.to_pylist(), offset):
            if not start <= index <= end: continue
            rows_reviewed += 1
            repo, revision, _ = source['instance_id'].split('.', 2); repo = repo.replace('__', '/', 1); repositories[repo] += 1
            messages = json.loads(source['messages']); actions = []
            for mi, message in enumerate(messages):
                for call in message.get('tool_calls') or []:
                    try: args = json.loads(call['function']['arguments'])
                    except (json.JSONDecodeError, TypeError): continue
                    matches = [(oi, observation) for oi, observation in enumerate(messages[mi+1:], mi+1)
                               if observation.get('role') == 'tool' and call['id'] in (observation.get('tool_call_ids') or [])]
                    actions.append({'message_index': mi, 'call': call, 'arguments': args, 'matches': matches})
            for ai, action in enumerate(actions):
                if action['call']['function']['name'] != 'bash' or len(action['matches']) != 1: continue
                oi, observation = action['matches'][0]; text = content(observation)
                categories = [name for name, pattern in PATTERNS.items() if re.search(pattern, text)]
                if not categories: continue
                following = []
                for later in actions[ai+1:ai+9]:
                    following.append({'message_index': later['message_index'], 'tool_call': later['call'],
                        'observation_index': later['matches'][0][0] if len(later['matches']) == 1 else None,
                        'observation': later['matches'][0][1] if len(later['matches']) == 1 else None})
                candidates.append({'candidate_id': f'swesmith-tool0-row{index}-message{action["message_index"]}',
                    'dataset': plan['dataset'], 'hub_revision': PUBLIC_REVISION,
                    'parquet_file': plan['candidate_shard'], 'parquet_sha256': plan['candidate_parquet_sha256'],
                    'row_index': index, 'instance_id': source['instance_id'], 'traj_id': source['traj_id'],
                    'repository': repo, 'upstream_revision': revision,
                    'connected_family': repo + '@' + revision,
                    'raw_messages_sha256': hashlib.sha256(source['messages'].encode()).hexdigest(),
                    'message_index': action['message_index'], 'tool_call': action['call'],
                    'observation_index': oi, 'observation': observation, 'candidate_categories': categories,
                    'subsequent_tool_calls': following, 'review_status': 'unreviewed_discovery_candidate',
                    'utility_label': None, 'target_state_provided': False})
        offset += batch.num_rows
    if rows_reviewed != end - start + 1: raise ValueError('Incomplete fixed source interval')
    output.mkdir(parents=True); (output / 'initial-review').mkdir()
    for candidate in candidates[:20]: write(output / 'initial-review' / (candidate['candidate_id'] + '.json'), candidate)
    metadata = [{k: v for k, v in c.items() if k not in ['observation', 'subsequent_tool_calls', 'tool_call']} for c in candidates]
    write(output / 'candidates.json', metadata)
    write(output / 'screening.json', {'status': 'complete', 'source_rows_scanned': rows_reviewed,
        'repository_row_counts': dict(repositories), 'candidate_tool_calls': len(candidates),
        'candidate_trajectories': len({c['row_index'] for c in candidates}),
        'candidate_connected_families': len({c['connected_family'] for c in candidates}),
        'initial_review_candidates_saved': min(20, len(candidates)), 'plan_sha256': sha(plan_path),
        'script_sha256': sha(Path(__file__)), 'commands_executed': 0,
        'new_training_examples': 0, 'actual_utility_labels': 0, 'final_task_outcomes_loaded': False})
    print(json.dumps(json.loads((output / 'screening.json').read_text()), indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    for name in ['parquet', 'plan', 'output']: p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args(); scan(a.parquet, a.plan, a.output)
