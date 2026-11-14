"""Reproduce the bounded public trajectory intake without executing source code.

Only row group zero and four named columns are read. Repository/issue identity,
not resolution or hidden evaluation outcomes, determines the sample.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import time

DATASET = 'nebius/SWE-rebench-openhands-trajectories'
REVISION = '35455389ab51bf5e2306bfd436ef72d0f98bf882'
METADATA_COLUMNS = ['trajectory_id', 'instance_id', 'repo']
COLUMNS = METADATA_COLUMNS + ['trajectory']


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select(metadata):
    by_repo = defaultdict(dict)
    for row, record in enumerate(metadata):
        by_repo[record['repo']].setdefault(record['instance_id'], row)
    eligible = [repo for repo, issues in by_repo.items() if len(issues) >= 2]
    repositories = sorted(eligible, key=lambda repo: hashlib.sha256(('broad-memory-20261108:' + repo).encode()).hexdigest())[:30]
    rows = sorted(row for repo in repositories for row in list(by_repo[repo].values())[:3])
    return {
        'rows': rows, 'repositories': repositories,
        'method': 'First distinct issue occurrences for 30 SHA-ordered multi-issue repositories; selection uses identifiers only',
        'outcomes_used': False, 'loaded_columns': COLUMNS,
    }


def collect(output):
    import fsspec
    import pyarrow.parquet as pq

    if output.exists():
        raise FileExistsError('Use a new intake directory')
    output.mkdir(parents=True)
    (output / 'raw').mkdir()
    url = f'https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/trajectories.parquet'
    started = time.monotonic()
    with fsspec.open(url, 'rb', block_size=2 ** 20, cache_type='bytes') as stream:
        parquet = pq.ParquetFile(stream)
        metadata = parquet.read_row_group(0, columns=METADATA_COLUMNS).to_pylist()
        selection = select(metadata)
        write(output / 'metadata.json', metadata)
        write(output / 'selection.json', selection)
        table = parquet.read_row_group(0, columns=COLUMNS)
        catalog = []
        for row in selection['rows']:
            record = table.slice(row, 1).to_pylist()[0]
            if isinstance(record['trajectory'], str):
                record['trajectory'] = json.loads(record['trajectory'])
            if any(record[key] != metadata[row][key] for key in METADATA_COLUMNS):
                raise ValueError('Metadata/trajectory identity mismatch')
            path = output / 'raw' / f'{row}.json'
            write(path, record)
            task = next(message['content'] for message in record['trajectory'] if message['role'] == 'user')
            catalog.append({'row_index': row, 'repo': record['repo'], 'instance_id': record['instance_id'],
                            'trajectory_id': record['trajectory_id'], 'messages': len(record['trajectory']),
                            'path': str(path), 'sha256': sha(path), 'task': task})
    write(output / 'catalog.json', catalog)
    write(output / 'source-intake.json', {
        'rows_saved': len(catalog), 'repositories': len(selection['repositories']), 'hub_revision': REVISION,
        'metadata_sha256': sha(output / 'metadata.json'), 'selection_sha256': sha(output / 'selection.json'),
        'commands_executed': 0, 'utility_labels_created': 0, 'seconds': time.monotonic() - started,
    })
    print(json.dumps({'rows': len(catalog), 'repositories': len(selection['repositories'])}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    collect(parser.parse_args().output)
