"""Bounded public intake with source-only cards before target disclosure.

Read only the columns in the committed plan. Source tool calls are inert data;
target trajectories, gold patches and evaluation columns are never exported.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import subprocess


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def overlap(repos, previous, basis):
    paths = subprocess.check_output([
        'git', 'ls-tree', '-r', '--name-only', basis, '--',
        'datasets', 'docs/design-data', 'training/memory_selector/reviews'
    ], text=True).splitlines()
    hits = defaultdict(list)
    allowed = {p for p in paths if Path(p).suffix in {'.json', '.jsonl', '.csv'}}
    for repo in repos:
        result = subprocess.run([
            'git', 'grep', '-i', '-F', '-l', '-e', repo, '-e', repo.replace('/', '__'),
            basis, '--', 'datasets', 'docs/design-data', 'training/memory_selector/reviews'
        ], capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr)
        for match in result.stdout.splitlines():
            name = match.removeprefix(basis + ':')
            if name in allowed:
                data = subprocess.check_output(['git', 'show', f'{basis}:{name}'])
                hits[repo].append({'path': name, 'sha256': hashlib.sha256(data).hexdigest()})
    for repo in previous:
        if repo in repos:
            hits[repo].append({'previous_trajectory_intake': True})
    return {repo: hits[repo] for repo in repos}


def collect(plan_path, inventory_path, previous_selection, output):
    import fsspec
    import pyarrow.parquet as pq

    if output.exists():
        raise FileExistsError('Use a new intake directory')
    plan = json.loads(plan_path.read_text())
    metadata = json.loads(inventory_path.read_text())
    previous = json.loads(previous_selection.read_text())['repositories']
    checks = overlap(plan['candidate_repositories'], previous, plan['basis_commit'])
    eligible = [repo for repo, hits in checks.items() if not hits]
    by_repo = defaultdict(dict)
    for row, item in enumerate(metadata):
        if item['repo'] in eligible:
            by_repo[item['repo']].setdefault(item['instance_id'], row)
    wanted = {instance for issues in by_repo.values() for instance in issues}
    tasks = {}
    task_locations = {}
    for name in plan['task_files']:
        url = f"https://huggingface.co/datasets/{plan['task_dataset']}/resolve/{plan['task_revision']}/{name}"
        with fsspec.open(url, 'rb', block_size=2 ** 20, cache_type='bytes') as stream:
            parquet = pq.ParquetFile(stream)
            offset = 0
            for batch in parquet.iter_batches(batch_size=1024, columns=plan['task_columns']):
                for index, task in enumerate(batch.to_pylist(), offset):
                    if task['instance_id'] in wanted:
                        if task['instance_id'] in tasks:
                            raise ValueError('Task ID occurs twice in the metadata join')
                        tasks[task['instance_id']] = task
                        task_locations[task['instance_id']] = {'file': name, 'row_index': index}
                offset += batch.num_rows
    selected = []
    unresolved = []
    for repo in eligible:
        issues = by_repo[repo]
        available = [i for i in issues if i in tasks]
        available.sort(key=lambda i: (tasks[i]['created_at'], i))
        if len(available) < 3:
            unresolved.append({'repo': repo, 'reason': 'fewer_than_three_joined_issues'})
            continue
        for role, instance in [('source', i) for i in available[:2]] + [('target', available[-1])]:
            selected.append({'role': role, 'row_index': issues[instance], 'repo': repo,
                             'instance_id': instance, 'created_at': tasks[instance]['created_at'],
                             'task_location': task_locations[instance]})
    if not selected:
        raise ValueError('No eligible source/target groups')
    output.mkdir(parents=True)
    write(output / 'selection.json', {
        'selected': selected, 'prior_overlap': checks, 'unresolved': unresolved,
        'missing_task_ids': sorted(wanted - tasks.keys()),
        'plan_sha256': sha(plan_path), 'inventory_sha256': sha(inventory_path),
        'prior_selection_sha256': sha(previous_selection),
    })
    source_rows = {item['row_index']: item for item in selected if item['role'] == 'source'}
    url = (f"https://huggingface.co/datasets/{plan['trajectory_dataset']}/resolve/"
           f"{plan['trajectory_revision']}/{plan['trajectory_file']}")
    records = []
    with fsspec.open(url, 'rb', block_size=2 ** 20, cache_type='bytes') as stream:
        parquet = pq.ParquetFile(stream)
        offset = 0
        for batch in parquet.iter_batches(batch_size=128, row_groups=[plan['row_group']],
                                         columns=plan['trajectory_columns']):
            for index in sorted(i for i in source_rows if offset <= i < offset + batch.num_rows):
                record = batch.slice(index - offset, 1).to_pylist()[0]
                selected_row = source_rows[index]
                for key in ('instance_id', 'repo'):
                    if record[key] != selected_row[key]:
                        raise ValueError('Inventory/source identity mismatch')
                if isinstance(record['trajectory'], str):
                    record['trajectory'] = json.loads(record['trajectory'])
                path = output / 'sources' / f'{index}.json'
                write(path, record)
                records.append({**selected_row, 'path': str(path), 'sha256': sha(path),
                                'trajectory_id': record['trajectory_id']})
            offset += batch.num_rows
    if len(records) != len(source_rows):
        raise ValueError('Missing source records')
    # Keep target text in a separate directory, not in source-review summaries.
    for item in selected:
        write(output / (item['role'] + '-tasks') / (str(item['row_index']) + '.json'), {
            **tasks[item['instance_id']], 'dataset': plan['task_dataset'],
            'revision': plan['task_revision'], **item['task_location'],
        })
    write(output / 'source-records.json', records)
    write(output / 'intake.json', {
        'source_records': len(records), 'target_records': sum(x['role'] == 'target' for x in selected),
        'repositories': len({x['repo'] for x in selected}), 'plan_sha256': sha(plan_path),
        'script_sha256': sha(Path(__file__)), 'source_commands_executed': 0,
        'utility_labels_created': 0, 'gpu_allocations': 0, 'target_trajectories_exported': 0,
        'outcome_or_gold_columns_loaded': False,
    })
    print(json.dumps(json.loads((output / 'intake.json').read_text())))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('plan', 'inventory', 'previous-selection', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    collect(args.plan, args.inventory, args.previous_selection, args.output)
