"""Post-hoc constructive feasibility check; no changes to the frozen grader.

These human-authored general wrappers see only the goal, object, and operation.
They are not trained memory selectors and are not prospective model baselines.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from data_pipeline.api_worker_judge import execute
from data_pipeline.api_transfer_runtime import plain

PRESERVE = '''def solve(data, operation):
    work = copy.deepcopy(data)
    result = operation(work)
    return work if result is None else result
'''

MUTATE = '''def solve(data, operation):
    result = operation(data)
    if result is None or result is data:
        return data
    if isinstance(data, np.ndarray):
        if data.dtype != result.dtype:
            data.dtype = result.dtype
        if data.shape != result.shape:
            data.resize(result.shape)
        data[...] = result
    elif isinstance(data, pd.DataFrame):
        result = result.copy(deep=True)
        data.drop(index=data.index, columns=data.columns, inplace=True)
        data[result.columns] = result
    elif sp.issparse(data):
        data.resize(result.shape)
        data.data = result.data.copy()
        data.indices = result.indices.copy()
        data.indptr = result.indptr.copy()
    elif isinstance(data, nx.Graph):
        data.clear()
        data.update(result)
    elif isinstance(data, Image.Image):
        data.thumbnail(result.size)
        data.paste(result)
    elif isinstance(data, list):
        data[:] = result
    elif isinstance(data, dict):
        data.clear()
        data.update(result)
    return data
'''


def write(path, value): path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def check(data, output, sort_sparse_indices=False):
    if output.exists(): raise FileExistsError(output)
    output.mkdir(parents=True)
    mutate = MUTATE
    if sort_sparse_indices:
        mutate = mutate.replace('        data.indptr = result.indptr.copy()\n',
                                '        data.indptr = result.indptr.copy()\n        data.sort_indices()\n')
    programs = {'preserve': PRESERVE, 'mutate': mutate}
    write(output / 'programs-before-execution.json', programs)
    states = {}
    for split in ['train', 'dev', 'test']:
        for line in (data / (split + '.jsonl')).read_text().splitlines():
            row = json.loads(line); states[row['state_id']] = row
    fixtures = [{r['case_id']: r for r in json.loads((data / 'evidence' / name).read_text())}
                for name in ['catalog.json', 'confirmation-fixtures.json']]
    records = []
    for state, row in sorted(states.items()):
        executions = [plain(execute(specs[world], programs[row['goal']], row['goal'], i))
                      for world in row['world_ids'] for i, specs in enumerate(fixtures)]
        records.append({'state_id': state, 'split': row['split'], 'family': row['family'],
                        'goal': row['goal'], 'success': all(e['passed'] for e in executions), 'executions': executions})
    summary = {split: {'states': sum(r['split'] == split for r in records),
                      'passed': sum(r['split'] == split and r['success'] for r in records)} for split in ['train', 'dev', 'test']}
    write(output / 'executions.json', records)
    value = {'status': 'complete', 'split_results': summary, 'failed_states': [r['state_id'] for r in records if not r['success']],
        'execution_records': sum(len(r['executions']) for r in records),
        'programs_sha256': hashlib.sha256((output / 'programs-before-execution.json').read_bytes()).hexdigest(),
        'post_hoc_human_authored_control': True, 'sparse_sorting_followup': sort_sparse_indices,
        'memory_used': False, 'models_trained': False,
        'fixed_grader_modified': False, 'fixture_expected_values_in_program': False,
        'claim_limit': 'Constructive feasibility for these frozen fixtures and interpreter. Not a general API adapter proof or prospective selector benchmark.'}
    write(output / 'summary.json', value); print(json.dumps(value, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ['data', 'output']: parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--sort-sparse-indices', action='store_true')
    a = parser.parse_args(); check(a.data, a.output, a.sort_sparse_indices)
