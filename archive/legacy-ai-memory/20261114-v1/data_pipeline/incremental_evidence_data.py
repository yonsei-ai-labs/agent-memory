"""Source-grounded field fragments and controlled partial-observation states."""
import argparse
from collections import Counter
import itertools
import json
from pathlib import Path
import shutil
import zipfile

from tokenizers import Tokenizer
from data_pipeline.collect_relation_source_pilot import digest, file_sha, readl, writel, write

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'datasets/memory_source_scale/20261111-v1'
DESIGN = ROOT / 'docs/design-data/memory-incremental-evidence-20261111-v2'
DATA = ROOT / 'datasets/memory_incremental_evidence/20261111-v2'


def tokenizer():
    with zipfile.ZipFile(ROOT / 'evaluation/results/memory-context-value-20261111-v1/results.zip') as z:
        return Tokenizer.from_str(z.read('worker-tokenizer.json').decode())


def observation(fields, observed, failed, variant):
    lines = []
    if failed:
        desc = fields[failed][0]
        lines.append(('An earlier lookup for ' + desc + ' failed and established no value.') if variant == 'direct'
                     else ('The first attempt to retrieve ' + desc + ' was unsuccessful; it supplied no answer.'))
    for key in observed:
        desc, value = fields[key]
        lines.append(('A completed check confirmed ' + desc + ': ' + value + '.') if variant == 'direct'
                     else ('The value of ' + desc + ' is already established as ' + value + ' by a later completed check.'))
    return '\n'.join(lines) or '(No current observations.)'


def state_specs(required, all_fields):
    out = [('missing', [], None), ('all_observed', list(required), None)]
    if len(required) > 1:
        out.extend(('one_observed', [k], None) for k in required)
    if len(required) > 2:
        out.extend(('one_missing', [k for k in required if k != absent], None) for absent in required)
    others = sorted(set(all_fields) - set(required))
    if others:
        out.append(('other_attribute_observed', [others[0]], None))
    out.append(('failed_lookup', [], required[0]))
    out.append(('recovered_lookup', [required[0]], required[0]))
    return out


def minimal_support_sets(bank, coverage, needed):
    needed = set(needed)
    if not needed:
        return [[]]
    ids = sorted(m['memory_id'] for m in bank)
    for n in range(1, len(ids) + 1):
        covers = [list(s) for s in itertools.combinations(ids, n)
                  if needed <= set().union(*(set(coverage[m]) for m in s))]
        if covers:
            return covers
    raise ValueError('No sufficient evidence')


def build(dest=DATA):
    if dest.exists():
        raise FileExistsError(dest)
    specs = json.loads((DESIGN / 'source-specs.json').read_text())
    tasks = {t['task_id']: t for t in readl(PARENT / 'tasks-supervision-only.jsonl')}
    parent_records = {r['memory_id']: r for r in readl(PARENT / 'records.jsonl')}
    tok = tokenizer(); records = {}; provenance = {}; cases = []; labels = []; ledger = []
    for spec in specs:
        task = tasks[spec['task_id']]
        if task['split'] not in ('train', 'dev'):
            raise ValueError('Reserved test cannot enter this study')
        fields = spec['fields']; required = spec['required']
        if not required or not set(required) <= set(fields):
            raise ValueError('Invalid required attributes')
        coverage = {}; bank = []
        card_specs = [(task['positive_ids'][spec.get('record_index', 0)], quote, keys) for quote, keys in spec['cards']]
        card_specs += [(task['positive_ids'][x['record_index']], x['text'], x['fields']) for x in spec.get('extra_cards', [])]
        for parent_id, quote, keys in card_specs:
            parent = parent_records[parent_id]; text = parent['text']; start = text.find(quote)
            if start < 0 or text.find(quote, start + 1) >= 0:
                raise ValueError('Missing/ambiguous source fragment: ' + task['task_id'] + ' ' + quote)
            if not set(keys) <= set(fields) or not keys:
                raise ValueError('Invalid fragment coverage')
            if any(fields[k][1].lower() not in quote.lower() for k in keys):
                raise ValueError('Value absent from approved source span')
            mid = 'ie-' + digest(parent_id + '|' + str(start) + '|' + quote)[:20]
            record = {'memory_id': mid, 'text': quote, 'source_group': task['source_group']}
            records[mid] = record; bank.append(record); coverage[mid] = list(keys)
            provenance[mid] = {'memory_id': mid, 'parent_record_id': parent_id,
                               'parent_text_sha256': digest(text), 'char_start': start, 'char_end': start + len(quote),
                               'source_text': text, 'source_group': task['source_group']}
        if len({m['memory_id'] for m in bank}) != len(bank):
            raise ValueError('Duplicate fragment')
        if set().union(*(set(v) for v in coverage.values())) != set(fields):
            raise ValueError('Uncovered reviewed attribute')
        bank.sort(key=lambda m: digest(task['task_id'] + '|' + m['memory_id']))
        goal = spec.get('goal', task['goal']) + '\nRequested details: ' + '; '.join(fields[k][0] for k in required) + '.'
        expected = {k: fields[k][1] for k in required}
        for idx, (mode, observed, failed) in enumerate(state_specs(required, fields)):
            remaining = sorted(set(required) - set(observed))
            support = minimal_support_sets(bank, coverage, remaining)
            for variant in ('direct', 'paraphrase'):
                sid = task['task_id'] + ':ie:' + str(idx) + ':' + variant
                c = {'state_id': sid, 'task_id': task['task_id'], 'source_group': task['source_group'],
                     'split': task['split'], 'mode': mode, 'variant': variant, 'state_pair_id': task['task_id'] + ':ie:' + str(idx),
                     'goal': goal, 'original_goal': task['goal'], 'goal_modified': goal != task['goal'],
                     'observations': observation(fields, observed, failed, variant), 'bank': bank,
                     'requested_fields': {k: fields[k][0] for k in required}, 'controlled_state': True}
                lab = {'state_id': sid, 'goal_link': {mid: int(bool(set(v) & set(required))) for mid, v in coverage.items()},
                       'labels': {mid: int(bool(set(v) & set(remaining))) for mid, v in coverage.items()},
                       'coverage': coverage, 'observed_fields': observed, 'remaining_fields': remaining,
                       'required_fields': required, 'expected': expected, 'minimal_support_sets': support,
                       'oracle_ids': min(support, key=lambda ids: (len(tok.encode('\n\n'.join(records[i]['text'] for i in ids), add_special_tokens=False).ids), ids)),
                       'utility': None, 'review_status': 'single_source_reading_review_controlled_augmentation'}
                cases.append(c); labels.append(lab)
        ledger.append({'task_id': task['task_id'], 'source_group': task['source_group'], 'split': task['split'],
                       'fields': fields, 'source_spans': len(bank), 'required_fields': required,
                       'original_goal': task['goal'], 'derived_goal': goal,
                       'minimum_records_when_missing': len(minimal_support_sets(bank, coverage, required)[0]),
                       'scope': 'Controlled field recovery from exact original source fragments; full underlying task not executed.'})
    # Old train rows are replay supervision, never additional new experiences.
    used = {c['source_group'] for c in cases}
    old_cases = readl(PARENT / 'train-120-cases.jsonl')
    old_labs = {r['state_id']: r for r in readl(PARENT / 'train-120-labels.jsonl')}
    groups = sorted({c['source_group'] for c in old_cases} - used, key=digest)
    replay = []; replay_labels = []
    for i, group in enumerate(groups[:80]):
        mode = ('missing', 'known', 'unrelated')[i % 3]
        c = min((c for c in old_cases if c['source_group'] == group and c['mode'] == mode), key=lambda c: c['state_id'])
        replay.append(c); replay_labels.append(old_labs[c['state_id']])
    if len(replay) != 80:
        raise ValueError('Insufficient independent replay parents')
    dest.mkdir(parents=True)
    for split in ('train', 'dev'):
        selected = [c for c in cases if c['split'] == split]; ids = {c['state_id'] for c in selected}
        writel(dest / (split + '-cases.jsonl'), selected)
        writel(dest / (split + '-labels.jsonl'), [l for l in labels if l['state_id'] in ids])
    writel(dest / 'records.jsonl', list(records.values())); writel(dest / 'provenance.jsonl', list(provenance.values()))
    writel(dest / 'review-ledger.jsonl', ledger); writel(dest / 'replay-cases.jsonl', replay); writel(dest / 'replay-labels.jsonl', replay_labels)
    shutil.copy2(DESIGN / 'source-specs.json', dest / 'source-specs.json')
    shutil.copy2(DESIGN / 'protocol.json', dest / 'protocol.json')
    manifest = {'experiment': 'memory-incremental-evidence-20261111-v2',
                'parent_manifest_sha256': file_sha(PARENT / 'manifest.json'),
                'partitions': {s: {'groups': len({c['source_group'] for c in cases if c['split'] == s}),
                                   'questions': sum(r['split'] == s for r in ledger),
                                   'states': sum(c['split'] == s for c in cases)} for s in ('train', 'dev')},
                'spans': len(records), 'replay_groups': len(replay), 'new_independent_sources': 0,
                'sha256': {p.name: file_sha(p) for p in sorted(dest.iterdir()) if p.is_file()}}
    write(dest / 'manifest.json', manifest)
    return audit(dest)


def audit(data=DATA):
    manifest = json.loads((data / 'manifest.json').read_text())
    if file_sha(PARENT / 'manifest.json') != manifest['parent_manifest_sha256']:
        raise ValueError('Parent changed')
    for name, sha in manifest['sha256'].items():
        if file_sha(data / name) != sha:
            raise ValueError('Frozen derived data changed: ' + name)
    records = {r['memory_id']: r for r in readl(data / 'records.jsonl')}
    for p in readl(data / 'provenance.jsonl'):
        if digest(p['source_text']) != p['parent_text_sha256'] or p['source_text'][p['char_start']:p['char_end']] != records[p['memory_id']]['text']:
            raise ValueError('Source span mismatch')
    groups = {}; report = {}
    for split in ('train', 'dev'):
        cs = readl(data / (split + '-cases.jsonl')); labs = {l['state_id']: l for l in readl(data / (split + '-labels.jsonl'))}
        if set(labs) != {c['state_id'] for c in cs}:
            raise ValueError('Label alignment')
        groups[split] = {c['source_group'] for c in cs}
        need = related = multiple = 0; families = {}
        for c in cs:
            l = labs[c['state_id']]; ids = {m['memory_id'] for m in c['bank']}
            if set(l['labels']) != ids or set(l['goal_link']) != ids or l['utility'] is not None:
                raise ValueError('Invalid supervision')
            remaining = set(l['required_fields']) - set(l['observed_fields'])
            if remaining != set(l['remaining_fields']):
                raise ValueError('Partial observation mismatch')
            for mid in ids:
                if l['labels'][mid] != int(bool(set(l['coverage'][mid]) & remaining)):
                    raise ValueError('Need mismatch')
                if l['goal_link'][mid] != int(bool(set(l['coverage'][mid]) & set(l['required_fields']))):
                    raise ValueError('Goal-link mismatch')
            if minimal_support_sets(c['bank'], l['coverage'], remaining) != l['minimal_support_sets']:
                raise ValueError('Incorrect sufficiency sets')
            need += sum(l['labels'].values()); related += sum(l['goal_link'].values())
            multiple += len(l['minimal_support_sets'][0]) > 1
            families.setdefault(c['task_id'], []).append(c)
        for rows in families.values():
            if len({json.dumps(c['bank'], sort_keys=True) for c in rows}) != 1 or len({c['goal'] for c in rows}) != 1:
                raise ValueError('Bank/goal changed across state contrasts')
        report[split] = {'groups': len(groups[split]), 'questions': len(families), 'states': len(cs),
                         'pairs': sum(len(c['bank']) for c in cs), 'positive_need_pairs': need,
                         'positive_goal_link_pairs': related, 'states_requiring_multiple_records': multiple,
                         'modes': dict(Counter(c['mode'] for c in cs))}
    replay = readl(data / 'replay-cases.jsonl'); replay_groups = {c['source_group'] for c in replay}
    if groups['train'] & groups['dev'] or replay_groups & (groups['train'] | groups['dev']):
        raise ValueError('Source overlap')
    report.update(source_spans=len(records), replay_groups=len(replay_groups), manifest_verified=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('mode', choices=['build', 'audit'])
    parser.add_argument('--data', type=Path, default=DATA); a = parser.parse_args()
    print(json.dumps(build(a.data) if a.mode == 'build' else audit(a.data), indent=2))
