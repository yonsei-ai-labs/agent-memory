"""Collect pinned public sources for a 30-source relation-label quality pilot.

Downloads and source grouping only. No model execution, automatic utility labels,
or expansion of unreviewed cases into training examples.
"""
import argparse
import ast
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import hashlib
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

PM_REVISION = 'ed956dea41521fc4499acbc63f966e0fd3c053ba'
MA_REVISION = 'c935c65be9aa19c930b60657c0c4b2b7ea13a34a'
PM_ROOT = f'https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2/resolve/{PM_REVISION}/'
MA_ROOT = f'https://raw.githubusercontent.com/Cantaloupe-M/Mem2ActBench/{MA_REVISION}/'
SALT = 'memory-relation-intake-20261111-v1'


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''): h.update(block)
    return h.hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def writel(path, rows):
    Path(path).write_text(''.join(json.dumps(r, ensure_ascii=False, allow_nan=False) + '\n' for r in rows))


def readl(path):
    return [json.loads(s) for s in Path(path).read_text().splitlines() if s.strip()]


def literal(value):
    """Upstream CSV stores Python literals. Never execute expressions from data."""
    if not isinstance(value, str): return value
    try:
        return json.loads(value)
    except ValueError:
        return ast.literal_eval(value)


def download(url, target, cap):
    target = Path(target)
    if target.exists():
        raise FileExistsError(target)
    started = time.monotonic()
    temp = target.with_suffix(target.suffix + '.part')
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with urlopen(Request(url, headers={'User-Agent': 'ai-memory-lab-source-intake/1.0'}), timeout=30) as response, temp.open('wb') as f:
            while True:
                block = response.read(1 << 20)
                if not block: break
                size += len(block)
                if size > cap or time.monotonic() - started > 240:
                    raise ValueError('Bounded source download exceeded limits')
                f.write(block)
        temp.replace(target)
    finally:
        if temp.exists(): temp.unlink()
    return {'url': url, 'file': target.name, 'bytes': size, 'sha256': file_sha(target)}


def groups_for_questions(questions, sessions, include_index=False):
    """Group transitively by every original source co-occurring in a mixed session.

    This is stricter than grouping by positive source IDs only: background turns
    in a source session must not silently cross a proposed partition.
    """
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def join(xs):
        xs = sorted(set(xs))
        if not xs: return
        anchor = find(xs[0])
        for x in xs[1:]: parent[find(x)] = anchor
    membership = defaultdict(set)
    for session in sessions:
        ids = set(session['original_conversation_ids']) | {t['source_id'] for t in session['turns'] if t.get('source_id')}
        join(ids)
        for source in ids: membership[source].add(session['session_id'])
    for q in questions: join(q['source_conversation_ids'])
    all_members = defaultdict(list)
    for source in list(parent): all_members[find(source)].append(source)
    canonical = {root: 'mem2act:' + digest('\n'.join(sorted(ids)))[:20] for root, ids in all_members.items()}
    grouped = defaultdict(list)
    for q in questions:
        if not q['source_conversation_ids']:
            continue  # Record these as ungrounded in the inventory, not a source group.
        roots = {find(i) for i in q['source_conversation_ids']}
        if len(roots) != 1: raise ValueError('Failed source component grouping')
        grouped[canonical[roots.pop()]].append(q)
    source_groups = {source: canonical[find(source)] for source in parent}
    if include_index: return dict(grouped), dict(membership), source_groups
    return dict(grouped), dict(membership)


def ordered(values):
    return sorted(values, key=lambda x: (digest(SALT + ':' + x), x))


def partition(ids, count=100):
    if count != 100: raise ValueError('This protocol reserves exactly 100 groups per source')
    chosen = ordered(ids)[:count]
    if len(chosen) != count:
        raise ValueError('Insufficient source groups for frozen expansion reservation')
    return {x: ('train' if i < 60 else 'dev' if i < 80 else 'test') for i, x in enumerate(chosen)}


def choose_strata(rows, strata, group_key, row_key):
    """Keep the first hash-ordered eligible row from each distinct training group."""
    selected, used = [], set()
    for name, predicate, n in strata:
        options = sorted((r for r in rows if predicate(r)), key=lambda r: (digest(SALT + ':' + row_key(r)), row_key(r)))
        count = 0
        for r in options:
            group = group_key(r)
            if group in used: continue
            selected.append({'stratum': name, 'source_group': group, 'row': r})
            used.add(group); count += 1
            if count == n: break
        if count != n:
            raise ValueError(f'Insufficient unique training sources for {name}: {count}/{n}')
    return selected


def collect(output, resume=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=resume)
    raw = output / 'raw'; raw.mkdir(exist_ok=resume)
    specs = [
        (MA_ROOT + 'Mem2ACTBench_small/qa_dataset.jsonl', raw / 'mem2act-qa.jsonl', 2_000_000),
        (MA_ROOT + 'Mem2ACTBench_small/Mem2ACT_conversation.jsonl', raw / 'mem2act-sessions.jsonl', 15_000_000),
        (MA_ROOT + 'README.md', raw / 'mem2act-README.md', 100_000),
        (PM_ROOT + 'benchmark/text/train.csv', raw / 'persona-train.csv', 200_000_000),
        (PM_ROOT + 'benchmark/text/val.csv', raw / 'persona-val.csv', 25_000_000),
        (PM_ROOT + 'README.md', raw / 'persona-README.md', 100_000),
        (PM_ROOT + 'column_descriptions.md', raw / 'persona-columns.md', 100_000),
    ]
    manifest_path = output / 'source-files.json'
    prior = json.loads(manifest_path.read_text()) if manifest_path.exists() else {'files': []}
    known = {r['file']: r for r in prior['files']}
    def fetch(spec):
        url, path, cap = spec
        if resume and path.exists():
            if path.name in known and (known[path.name]['url'] != url or known[path.name]['sha256'] != file_sha(path)):
                raise ValueError(f'Previously downloaded source changed: {path.name}')
            result = {'url': url, 'file': path.name, 'bytes': path.stat().st_size,
                      'sha256': file_sha(path), 'reused_local_download': True}
        else:
            result = download(*spec)
        print(json.dumps({k: result[k] for k in ('file', 'bytes', 'sha256')}), flush=True)
        return result
    sources, errors = [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fetch, spec): spec for spec in specs}
        for future in as_completed(futures):
            try:
                sources.append(future.result())
            except Exception as exc:
                errors.append({'url': futures[future][0], 'error': str(exc)})
            write(output / 'source-files.json', {'files': sorted(sources, key=lambda x: x['file']),
                  'errors': errors, 'model_calls': 0, 'gpu_allocations': 0})
    if errors: raise ValueError(f'Source downloads incomplete: {errors}')
    return select(output)


def select(output):
    output = Path(output); raw = output / 'raw'
    manifest = json.loads((output / 'source-files.json').read_text())
    if manifest.get('errors'): raise ValueError('Source downloads incomplete')
    for f in manifest['files']:
        if file_sha(raw / f['file']) != f['sha256']: raise ValueError('Changed raw source')
    questions, sessions = readl(raw / 'mem2act-qa.jsonl'), readl(raw / 'mem2act-sessions.jsonl')
    groups, membership, source_groups = groups_for_questions(questions, sessions, include_index=True)
    ma_splits = partition(groups)
    ma_rows = [{'source_group': g, 'question': q} for g, qs in groups.items() if ma_splits.get(g) == 'train' for q in qs]
    ma_pilot = choose_strata(ma_rows, [(level, lambda r, level=level: r['question']['complexity_metadata']['level'] == level, n)
                                      for level, n in [('L4', 5), ('L3', 2), ('L2', 5), ('L1', 3)]],
                            lambda r: r['source_group'], lambda r: r['question']['qa_id'])
    csv.field_size_limit(5_000_000)
    with (raw / 'persona-train.csv').open(newline='') as f:
        persona = [{**r, '_csv_row': i + 2} for i, r in enumerate(csv.DictReader(f))]
    with (raw / 'persona-val.csv').open(newline='') as f:
        val_personas = {r['persona_id'] for r in csv.DictReader(f)}
    persona_ids = {r['persona_id'] for r in persona}
    pm_splits = partition('persona:' + x for x in persona_ids)
    pm_rows = [r for r in persona if pm_splits.get('persona:' + r['persona_id']) == 'train']
    pm_pilot = choose_strata(pm_rows, [
        ('updated_self', lambda r: r['who'] == 'self' and r['updated'] == 'True', 5),
        ('other_subject', lambda r: r['who'] == 'others', 5),
        ('stable_self', lambda r: r['who'] == 'self' and r['updated'] == 'False', 5)],
        lambda r: 'persona:' + r['persona_id'], lambda r: f"{r['persona_id']}:{r['_csv_row']}")
    reservation = [{'source': source, 'source_group': g, 'split': split}
                   for source, splits in [('mem2act', ma_splits), ('personamem_v2', pm_splits)] for g, split in sorted(splits.items())]
    for r in reservation:
        if r['source'] == 'mem2act':
            r['original_source_ids'] = sorted(s for s, g in source_groups.items() if g == r['source_group'])
        else:
            r['original_source_ids'] = [r['source_group'].split(':', 1)[1]]
    pilot = []
    for item in ma_pilot:
        q = item['row']['question']
        pilot.append({'case_id': 'mem2act:' + q['qa_id'], 'source': 'mem2act', 'source_group': item['source_group'],
            'split': 'train_pilot', 'stratum': item['stratum'], 'source_row': q,
            'missing_source_ids': [s for s in q['source_conversation_ids'] if s not in membership]})
    for item in pm_pilot:
        r = item['row']
        pilot.append({'case_id': 'persona:' + r['persona_id'] + ':row' + str(r['_csv_row']),
            'source': 'personamem_v2', 'source_group': item['source_group'], 'split': 'train_pilot',
            'stratum': item['stratum'], 'source_row': r})
    # Do not silently reselect a pilot after its content has been reviewed.
    outputs = []
    for name, rows in [('source-reservation.jsonl', reservation), ('pilot-selection.jsonl', pilot)]:
        serialized = ''.join(json.dumps(r, ensure_ascii=False, allow_nan=False) + '\n' for r in rows)
        target = output / name
        if target.exists() and target.read_text() != serialized:
            raise ValueError(f'Frozen selection changed: {name}; use a new version')
        outputs.append((target, serialized))
    for target, serialized in outputs: target.write_text(serialized)
    write(output / 'selection-freeze.json', {
        'selection_sha256': file_sha(output / 'pilot-selection.jsonl'),
        'reservation_sha256': file_sha(output / 'source-reservation.jsonl'),
        'quota_adjustment': 'Reserved train has only 2 L3 groups; before content review L3 3->2, L2 4->5; unchanged group assignments.',
    })
    summary = {'questions_mem2act': len(questions), 'sessions_mem2act': len(sessions), 'mem2act_components': len(groups),
        'mem2act_no_source_ids': [q['qa_id'] for q in questions if not q['source_conversation_ids']],
        'persona_train_rows': len(persona), 'persona_train_users': len(persona_ids), 'persona_val_users': len(val_personas),
        'persona_train_val_user_overlap': len(persona_ids & val_personas),
        'reservation': dict(Counter(x['split'] for x in reservation)), 'pilot_cases': len(pilot),
        'pilot_source_groups': len({r['source_group'] for r in pilot}), 'pilot_strata': dict(Counter(r['stratum'] for r in pilot)),
        'selection': 'SHA order within predeclared relation strata from the reserved training groups; no worker outcomes',
        'utility_labels': 0, 'source_revisions': {'mem2act': MA_REVISION, 'personamem_v2': PM_REVISION}}
    write(output / 'selection-summary.json', summary)
    print(json.dumps(summary, indent=2), flush=True)
    return summary


def histories(output):
    """Fetch only the frozen training pilot's PersonaMem histories and raw records."""
    output = Path(output)
    freeze = json.loads((output / 'selection-freeze.json').read_text())
    if freeze['selection_sha256'] != file_sha(output / 'pilot-selection.jsonl'):
        raise ValueError('Pilot selection changed after freeze')
    prior_path = output / 'history-files.json'
    prior = json.loads(prior_path.read_text()) if prior_path.exists() else {'files': []}
    records = {r['file']: r for r in prior['files']}
    specs = []
    for item in readl(output / 'pilot-selection.jsonl'):
        if item['source'] != 'personamem_v2': continue
        row = item['source_row']
        for column in ('raw_persona_file', 'chat_history_32k_link'):
            path = row[column]
            if not path.startswith('data/') or '..' in path or '://' in path:
                raise ValueError('Unexpected upstream relative path')
            target = output / 'raw' / Path(path).name
            if target.name in records:
                if file_sha(target) != records[target.name]['sha256']: raise ValueError('Changed history')
                continue
            specs.append((PM_ROOT + path, target, 8_000_000))
    with ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(lambda spec: download(*spec), specs):
            records[result['file']] = result
            write(prior_path, {'files': sorted(records.values(), key=lambda r: r['file'])})
    print(json.dumps({'history_files': len(records)}), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('collect', 'select', 'histories'))
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--resume', action='store_true', help='Reuse completed downloads from an interrupted pinned-source intake')
    args = p.parse_args()
    if args.mode == 'collect': collect(args.output, resume=args.resume)
    elif args.mode == 'select': select(args.output)
    else: histories(args.output)
