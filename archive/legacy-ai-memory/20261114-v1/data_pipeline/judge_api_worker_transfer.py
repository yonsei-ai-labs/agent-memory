"""Judge a new fixed worker only after freezing every transferred action."""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import zipfile
from data_pipeline.api_worker_judge_serialized import judge
from evaluation.verify_api_utility import metrics, action_order

ROOT = Path(__file__).resolve().parents[1]


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path): return json.loads(path.read_text())
def lines(path): return [json.loads(line) for line in path.read_text().splitlines()]
def write(path, value): path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def evaluate(worker, bundle, data, frozen, output):
    if output.exists(): raise FileExistsError(output)
    freeze = read(frozen); result = read(worker / 'artifacts/results.json'); status = read(worker / 'run-status.json')
    if read(worker / 'audit.json')['status'] != 'passed' or not status['stopped'] or result['status'] != 'complete':
        raise ValueError('Audited, completed and stopped worker required')
    if freeze['status'] != 'frozen_before_transfer_generation' or result['config']['model_id'] != 'Qwen/Qwen3.5-9B':
        raise ValueError('Unexpected transfer contract')
    checks = {'transfer_jobs': bundle / 'jobs.jsonl', 'transfer_protocol': bundle / 'protocol.json',
              'judge': ROOT / 'data_pipeline/api_worker_judge.py',
              'interpreter': ROOT / 'data_pipeline/api_worker_interpreter.py',
              'serialization_wrapper': ROOT / 'data_pipeline/api_worker_judge_serialized.py'}
    for key, path in checks.items():
        if sha(path) != freeze['source_sha256'][key]: raise ValueError('Pre-generation source changed: ' + key)
    with zipfile.ZipFile(worker / 'upload.zip') as archive:
        if archive.read('data/jobs.jsonl') != (bundle / 'jobs.jsonl').read_bytes(): raise ValueError('Generated prompts differ from transfer freeze')
    if result['protocol']['included_splits'] != ['test'] or result['generated_jobs'] != 284:
        raise ValueError('Unexpected transfer cohort')
    journal = output.with_suffix('.prejudge.json')
    if journal.exists(): raise FileExistsError(journal)
    write(journal, {'frozen_policy_sha256': sha(frozen), 'frozen_utc': freeze['frozen_utc'],
                    'before_judging_utc': datetime.now(timezone.utc).isoformat(),
                    'worker_predictions_sha256': sha(worker / 'artifacts/predictions.jsonl'),
                    'source_sha256': {k: sha(p) for k, p in checks.items()},
                    'new_policy_training_or_selection': False})
    summary = judge(data, worker / 'artifacts/predictions.jsonl', output, ['test'])
    outcomes = defaultdict(dict)
    for row in lines(output / 'judgments.jsonl'):
        if '-repeat-' not in row['job_id']: outcomes[row['state_id']][row['action']] = row
    rows = defaultdict(list)
    for row in lines(data / 'test.jsonl'): rows[row['state_id']].append(row)
    if set(rows) != set(outcomes): raise ValueError('Transfer state inventory changed')
    policies = []
    for policy in freeze['policies']:
        choices = [(r['state_id'], r['action']) for r in policy['choices']]
        if [s for s, _ in choices] != sorted(rows): raise ValueError('Frozen transfer decision order differs')
        policies.append({'name': policy['name'], 'kind': policy['kind'], 'metrics': metrics(choices, outcomes)})
    oracle = [(state, min(action_order(rs), key=lambda a: (-int(outcomes[state][a]['all_passed']),
               outcomes[state][a]['input_tokens'] + outcomes[state][a]['output_tokens']))) for state, rs in sorted(rows.items())]
    value = {'status': 'complete', 'primary': freeze['primary'],
        'secondary_from_original_dev': freeze['secondary_from_original_dev'], 'variants': policies,
        'action_oracle': metrics(oracle, outcomes), 'oracle_is_diagnostic_only': True,
        'frozen_transfer_policy_sha256': sha(frozen), 'worker_audit_sha256': sha(worker / 'audit.json'),
        'judgments_sha256': sha(output / 'judgments.jsonl'), 'selection_after_transfer': False,
        'training_performed': False, 'local_gpu_used': False,
        'native_policy_limitation': 'Native policies reuse fixed 4B-query decisions, not 9B hidden states. '
                                    'A live deployment would also need that 4B feature producer.'}
    write(output / 'policy-results.json', value)
    print(json.dumps({'states': summary['states'], 'syntax_supported_fraction': summary['syntax_supported_fraction'],
                      'policies': {r['name']: {'successes': r['metrics']['successes'],
                         'improvements': r['metrics']['improvements'], 'harms': r['metrics']['harms']} for r in policies},
                      'oracle': value['action_oracle']['successes']}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ['worker', 'bundle', 'data', 'frozen', 'output']: parser.add_argument('--' + name, type=Path, required=True)
    a = parser.parse_args(); evaluate(a.worker, a.bundle, a.data, a.frozen, a.output)
