"""Build and verify discovery dossiers, kept separate from learner exports.

Run from the repository root. Checking shared artifacts needs only the standard
library. --intake additionally compares original source records and full fetched
base-code objects; neither mode executes downloaded code or trajectory commands.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path

from data_pipeline.collect_memory_evidence_discovery import sha, write

DEFAULT = Path('docs/design-data/memory-evidence-discovery-20261108-v1')


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def check_blob(obj):
    data = obj['content'].encode()
    git_sha = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    if git_sha != obj['sha']:
        raise ValueError('Fetched code disagrees with its Git blob ID')


def fetch_inventory(root, output):
    import fsspec
    import pyarrow.parquet as pq
    if output.exists():
        raise FileExistsError('Use a new inventory path')
    plan = json.loads((root / 'intake-plan.json').read_text())
    url = (f"https://huggingface.co/datasets/{plan['trajectory_dataset']}/resolve/"
           f"{plan['trajectory_revision']}/{plan['trajectory_file']}")
    with fsspec.open(url, 'rb', block_size=2 ** 20, cache_type='bytes') as stream:
        table = pq.ParquetFile(stream).read_row_group(
            plan['row_group'], columns=['trajectory_id', 'instance_id', 'repo'])
    write(output, table.to_pylist())
    expected = json.loads((root / 'intake-ledger.json').read_text())['selection']['inventory_sha256']
    if sha(output) != expected:
        raise ValueError('Identifier inventory differs from the original intake')
    print(json.dumps({'identifier_rows': table.num_rows, 'sha256': sha(output)}))


def fetch_code(root, intake):
    from urllib.request import urlopen
    count = 0
    for path in sorted((root / 'current-evidence').glob('*.json')):
        current = json.loads(path.read_text())
        row = current['target_trajectory_row_index']
        for code in current['base_code']:
            target = intake / 'target-code' / str(row) / (code['path'].replace('/', '__') + '.json')
            if target.exists():
                obj = json.loads(target.read_text())
            else:
                url = f"https://raw.githubusercontent.com/{code['repository']}/{code['base_commit']}/{code['path']}"
                with urlopen(url, timeout=30) as response:
                    content = response.read().decode('utf-8')
                obj = {'row': row, 'repository_full_name': code['repository'],
                       'original_repository': current['task']['repo'], 'ref': code['base_commit'],
                       'path': code['path'], 'content': content, 'sha': code['git_blob_sha'],
                       'encoding': 'utf-8', 'display_url': code['source_url']}
            check_blob(obj)
            if digest(obj['content']) != code['full_content_sha256']:
                raise ValueError('Pinned base code changed')
            if not target.exists():
                write(target, obj)
            count += 1
    print(json.dumps({'full_code_objects_verified': count, 'downloaded_code_executed': False}))


def expand(root, intake, inventory, output):
    import fsspec
    import pyarrow.parquet as pq
    if (output / 'expanded-target-tasks.json').exists():
        raise FileExistsError('Use a new expansion output directory')
    plan = json.loads((root / 'intake-plan.json').read_text())
    selected = json.loads((intake / 'selection.json').read_text())['selected']
    excluded = {x['instance_id'] for x in selected}
    metadata = json.loads(inventory.read_text())
    wanted = {x['instance_id'] for x in metadata if x['repo'] in plan['candidate_repositories']} - excluded
    source_dates = {}
    for card in read_jsonl(root / 'source-cards.jsonl'):
        task = json.loads((intake / 'source-tasks' / f"{card['row_index']}.json").read_text())
        source_dates[task['repo']] = task['created_at']
    rows = []
    for name in plan['task_files']:
        url = f"https://huggingface.co/datasets/{plan['task_dataset']}/resolve/{plan['task_revision']}/{name}"
        with fsspec.open(url, 'rb', block_size=2 ** 20, cache_type='bytes') as stream:
            parquet = pq.ParquetFile(stream)
            offset = 0
            for batch in parquet.iter_batches(batch_size=1024, columns=plan['task_columns']):
                for index, task in enumerate(batch.to_pylist(), offset):
                    if task['instance_id'] in wanted and task['created_at'] > source_dates[task['repo']]:
                        rows.append({**task, 'dataset': plan['task_dataset'],
                                     'revision': plan['task_revision'], 'file': name, 'row_index': index})
                offset += batch.num_rows
    rows.sort(key=lambda t: (plan['candidate_repositories'].index(t['repo']), t['created_at'], t['instance_id']))
    if len(rows) > 52:
        raise ValueError('Expansion exceeds the stated review budget')
    write(output / 'expanded-target-tasks.json', rows)
    write(output / 'target-expansion-manifest.json', {
        'plan_sha256': sha(root / 'target-expansion-plan.json'),
        'source_freeze_sha256': sha(root / 'source-freeze.json'),
        'candidate_count': len(rows),
        'candidates': [{k: v for k, v in t.items() if k not in ['problem_statement', 'docker_image', 'image_name']} for t in rows],
        'task_file_sha256': sha(output / 'expanded-target-tasks.json'),
        'future_trajectories_loaded': False,
    })
    print(json.dumps({'expanded_task_candidates': len(rows)}))


def build(root, intake, inventory):
    if (root / 'cases.jsonl').exists():
        raise FileExistsError('Dossiers already exist; use a new version')
    specs = json.loads((root / 'case-review.json').read_text())
    cards = {c['id']: c for c in read_jsonl(root / 'source-cards.jsonl')}
    metadata = json.loads(inventory.read_text())
    target_tasks = {int(p.stem): json.loads(p.read_text()) for p in (intake / 'target-tasks').glob('*.json')}
    expanded = json.loads((intake / 'expanded-target-tasks.json').read_text())
    first_row = {}
    for index, record in enumerate(metadata):
        first_row.setdefault(record['instance_id'], index)
    for task in expanded:
        target_tasks[first_row[task['instance_id']]] = task
    code_ranges = defaultdict(lambda: defaultdict(set))
    for spec in specs:
        for name, ranges in spec['code'].items():
            code_ranges[spec['target_row']][name].update(tuple(r) for r in ranges)
    current_refs = {}
    for row, files in code_ranges.items():
        task = target_tasks[row]
        blobs = []
        for name, ranges in sorted(files.items()):
            raw_path = intake / 'target-code' / str(row) / (name.replace('/', '__') + '.json')
            obj = json.loads(raw_path.read_text())
            check_blob(obj)
            if obj['ref'] != task['base_commit'] or obj['path'] != name:
                raise ValueError('Wrong current revision or file')
            if obj.get('original_repository', obj['repository_full_name']) != task['repo']:
                raise ValueError('Repository mismatch (including documented relocation)')
            lines = obj['content'].splitlines(keepends=True)
            spans = []
            for start, end in sorted(ranges):
                if not 1 <= start <= end <= len(lines):
                    raise ValueError('Invalid current-code line interval')
                text = ''.join(lines[start - 1:end])
                spans.append({'start_line': start, 'end_line': end, 'text': text, 'sha256': digest(text)})
            blobs.append({'path': name, 'repository': obj['repository_full_name'],
                          'base_commit': obj['ref'], 'git_blob_sha': obj['sha'],
                          'full_content_sha256': digest(obj['content']), 'source_url': obj['display_url'],
                          'spans': spans})
        current = {'target_trajectory_row_index': row, 'task': task, 'base_code': blobs,
                   'observation_kind': 'researcher_read_of_pinned_pre_patch_code',
                   'worker_initial_inspection_performed': False,
                   'restorable_worker_snapshot': None, 'target_trajectory_loaded': False,
                   'gold_patch_or_hidden_tests_loaded': False}
        path = root / 'current-evidence' / f'{row}.json'
        write(path, current)
        current_refs[row] = {'path': str(path), 'sha256': sha(path)}
    cases = []
    for spec in specs:
        card = cards[spec['memory_id']]
        task = target_tasks[spec['target_row']]
        cases.append({**{k: v for k, v in spec.items() if k != 'code'},
                      'target_instance_id': task['instance_id'], 'target_repository': task['repo'],
                      'target_base_commit': task['base_commit'], 'target_parent_group': task['repo'] + '@' + task['base_commit'],
                      'source_instance_id': card['source_instance_id'], 'source_repository': card['repo'],
                      'source_parent_group': card['repo'] + '@' + card['base_commit'],
                      'current_evidence': current_refs[spec['target_row']],
                      'source_evidence': {'path': card['evidence_file'], 'sha256': card['evidence_sha256']},
                      'relation_annotation_status': 'single_agent_provisional',
                      'observed_utility': None, 'worker_outcomes': None,
                      'readiness': 'code_grounded_review_only', 'learner_export_allowed': False,
                      'fresh_confirmatory_test_eligible': False})
    (root / 'cases.jsonl').write_text(''.join(json.dumps(x, ensure_ascii=False, allow_nan=False) + '\n' for x in cases))
    fields = ['id', 'target_instance_id', 'memory_id', 'relation', 'direct_reuse', 'current_requirement',
              'current_observation', 'reason', 'already_in_problem', 'missing', 'readiness', 'observed_utility']
    with (root / 'case-table.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for case in cases:
            writer.writerow({k: 'null' if case[k] is None else case[k] for k in fields})
    promoted = {c['target_instance_id'] for c in cases}
    write(root / 'expanded-target-screening.json', [
        {'instance_id': task['instance_id'], 'repository': task['repo'],
         'title': task['problem_statement'].splitlines()[0],
         'problem_statement_sha256': digest(task['problem_statement']),
         'review_status': 'problem_and_base_code_reviewed' if task['instance_id'] in promoted else
         'problem_body_reviewed_not_promoted_missing_offending_file' if task['instance_id'] == 'asottile__pyupgrade-146' else
         'title_screened_only_not_a_negative_label',
         'utility': None} for task in expanded])
    write(root / 'summary.json', {
        'source_trajectory_candidates': 24, 'source_cards_reviewed': len(cards),
        'source_repository_groups': len({c['repo'] for c in cards.values()}),
        'source_tool_call_observation_pairs': sum(len(c['calls']) for c in cards.values()),
        'original_targets': 12, 'additional_statement_candidates': len(expanded),
        'additional_problem_bodies_reviewed': 7, 'additional_targets_code_reviewed': 6,
        'current_problems': len(current_refs), 'case_pairs': len(cases),
        'relation_counts': dict(Counter(c['relation'] for c in cases)),
        'source_target_same_base_pairs': sum(c['source_parent_group'] == c['target_parent_group'] for c in cases),
        'new_training_examples': 0, 'worker_utility_labels': 0, 'ready_worker_snapshots': 0,
        'gpu_allocations': 0, 'paid_model_calls': 0,
        'interpretation': 'New discovery evidence and provisional relations, not a utility benchmark or proof of useful memory selection',
    })
    print(json.dumps(json.loads((root / 'summary.json').read_text()), ensure_ascii=False))


def make_index(root):
    files = [p for p in sorted(root.rglob('*')) if p.is_file()
             and p.name not in {'artifact-index.json', 'verification.json'}]
    write(root / 'artifact-index.json', {'purpose': 'Frozen discovery artifacts, not learner supervision',
                                       'artifacts': [{'path': str(p), 'sha256': sha(p)} for p in files]})


def check(root, intake=None):
    index = json.loads((root / 'artifact-index.json').read_text())
    freeze = json.loads((root / 'source-freeze.json').read_text())
    plan = json.loads((root / 'intake-plan.json').read_text())
    indexed = {item['path'] for item in index['artifacts']}
    actual = {str(p) for p in root.rglob('*') if p.is_file()
              and p.name not in {'artifact-index.json', 'verification.json'}}
    if indexed != actual:
        raise ValueError('Artifact inventory is incomplete or has stale entries')
    if sha(root / 'intake-plan.json') != freeze['plan_sha256']:
        raise ValueError('Source freeze refers to a different intake plan')
    for item in index['artifacts'] + freeze['artifacts']:
        if sha(Path(item['path'])) != item['sha256']:
            raise ValueError('Artifact changed: ' + item['path'])
    cards = read_jsonl(root / 'source-cards.jsonl')
    cases = read_jsonl(root / 'cases.jsonl')
    with (root / 'case-table.csv').open(newline='') as stream:
        table = list(csv.DictReader(stream))
    if len(table) != len(cases):
        raise ValueError('Case table has a different number of rows')
    for row, case in zip(table, cases):
        if any(row[k] != ('null' if case[k] is None else str(case[k])) for k in row):
            raise ValueError('Readable case table differs from structured cases')
    if len({c['id'] for c in cards}) != len(cards) or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Duplicate card/case ID')
    card_map = {c['id']: c for c in cards}
    source_ids = {c['source_instance_id'] for c in cards}
    target_ids = {c['target_instance_id'] for c in cases}
    if source_ids & target_ids:
        raise ValueError('Same issue used as source and target')
    calls, code_files, spans = 0, set(), 0
    for card in cards:
        if card['learner_export_allowed'] or card['observed_utility'] is not None:
            raise ValueError('Source card was promoted or assigned utility')
        evidence = json.loads(Path(card['evidence_file']).read_text())
        if evidence['instance_id'] != card['source_instance_id']:
            raise ValueError('Wrong source evidence')
        raw = None
        if intake:
            raw_path = intake / 'sources' / f"{card['row_index']}.json"
            if sha(raw_path) != evidence['source_record_sha256']:
                raise ValueError('Original source changed')
            raw = json.loads(raw_path.read_text())['trajectory']
        for action in evidence['actions']:
            calls += 1
            if action['tool_call']['id'] != action['observation']['tool_call_id']:
                raise ValueError('Unlinked source observation')
            if action['message_index'] >= action['observation_index']:
                raise ValueError('Source observation precedes its action')
            if raw and (action['tool_call'] not in raw[action['message_index']]['tool_calls']
                        or action['observation'] != raw[action['observation_index']]):
                raise ValueError('Export differs from the source')
        if [a['message_index'] for a in evidence['actions']] != card['calls']:
            raise ValueError('Reviewed source calls differ from the evidence')
    current_seen = set()
    for case in cases:
        if case['memory_id'] not in card_map or case['observed_utility'] is not None or case['worker_outcomes'] is not None:
            raise ValueError('Unknown memory or fabricated outcome')
        card = card_map[case['memory_id']]
        if (case['source_instance_id'] != card['source_instance_id']
                or case['source_evidence']['path'] != card['evidence_file']
                or case['source_repository'] != card['repo']):
            raise ValueError('Case references another memory source')
        if case['learner_export_allowed'] or case['fresh_confirmatory_test_eligible']:
            raise ValueError('Discovery record was promoted')
        for ref in [case['source_evidence'], case['current_evidence']]:
            if sha(Path(ref['path'])) != ref['sha256']:
                raise ValueError('Case reference hash mismatch')
        current_path = case['current_evidence']['path']
        obj = json.loads(Path(current_path).read_text())
        if obj['task']['instance_id'] != case['target_instance_id']:
            raise ValueError('Wrong current problem')
        allowed = set(plan['task_columns']) | {'dataset', 'revision', 'file', 'row_index'}
        if obj['task'].keys() - allowed or obj['target_trajectory_loaded'] or obj['gold_patch_or_hidden_tests_loaded']:
            raise ValueError('Forbidden target information')
        if (obj['task']['dataset'] != plan['task_dataset']
                or obj['task']['revision'] != plan['task_revision']
                or obj['task']['base_commit'] != case['target_base_commit']
                or obj['task']['repo'] != case['target_repository']):
            raise ValueError('Wrong target task metadata')
        if obj['worker_initial_inspection_performed'] or obj['restorable_worker_snapshot'] is not None:
            raise ValueError('Researcher code inspection mislabeled as worker snapshot')
        if current_path in current_seen:
            continue
        current_seen.add(current_path)
        for code in obj['base_code']:
            code_files.add((case['target_instance_id'], code['path']))
            if code['base_commit'] != case['target_base_commit']:
                raise ValueError('Current code from another revision')
            raw = None
            if intake:
                p = intake / 'target-code' / str(case['target_row']) / (code['path'].replace('/', '__') + '.json')
                raw = json.loads(p.read_text())
                check_blob(raw)
                if digest(raw['content']) != code['full_content_sha256'] or raw['sha'] != code['git_blob_sha']:
                    raise ValueError('Full current code changed')
            for span in code['spans']:
                spans += 1
                if digest(span['text']) != span['sha256']:
                    raise ValueError('Code span changed')
                if raw:
                    original = ''.join(raw['content'].splitlines(keepends=True)[span['start_line'] - 1:span['end_line']])
                    if original != span['text']:
                        raise ValueError('Code excerpt differs from original blob')
    result = {'status': 'pass', 'frozen_artifacts': len(index['artifacts']), 'source_cards': len(cards),
              'linked_source_actions': calls, 'current_problems': len(target_ids), 'case_pairs': len(cases),
              'current_code_files': len(code_files), 'code_spans': spans,
              'original_source_and_full_code_checked': intake is not None,
              'worker_utility_labels': 0, 'learner_exports_allowed': 0}
    summary = json.loads((root / 'summary.json').read_text())
    if (summary['case_pairs'] != len(cases) or summary['current_problems'] != len(target_ids)
            or summary['source_tool_call_observation_pairs'] != calls
            or summary['relation_counts'] != dict(Counter(c['relation'] for c in cases))):
        raise ValueError('Summary differs from the case/evidence records')
    print(json.dumps(result))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['inventory', 'fetch-code', 'expand', 'build', 'index', 'check'])
    parser.add_argument('--root', type=Path, default=DEFAULT)
    parser.add_argument('--intake', type=Path)
    parser.add_argument('--inventory', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'inventory':
        if not args.output:
            parser.error('inventory requires --output')
        fetch_inventory(args.root, args.output)
    elif args.command == 'fetch-code':
        if not args.intake:
            parser.error('fetch-code requires --intake')
        fetch_code(args.root, args.intake)
    elif args.command == 'expand':
        if not (args.intake and args.inventory and args.output):
            parser.error('expand requires --intake, --inventory and --output')
        expand(args.root, args.intake, args.inventory, args.output)
    elif args.command == 'build':
        if not (args.intake and args.inventory):
            parser.error('build requires --intake and --inventory')
        build(args.root, args.intake, args.inventory)
    elif args.command == 'index':
        make_index(args.root)
    else:
        check(args.root, args.intake)
