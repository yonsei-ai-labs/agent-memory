"""Literal-source packing contrasts; freeze sources and labels before inference."""
import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import shutil

from data_pipeline.collect_relation_source_pilot import readl, writel, write, digest, file_sha
from data_pipeline.incremental_evidence_data import tokenizer, minimal_support_sets

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / 'docs/design-data/memory-partial-records-20261111-v1'
DATA = ROOT / 'datasets/memory_partial_records/20261111-v1'
WORK = ROOT / 'runs/memory-partial-records-20261111-v1'
PREVIOUS = ROOT / 'datasets/memory_incremental_evidence/20261111-v2'
SCALE = ROOT / 'datasets/memory_source_scale/20261111-v1'
INITIAL = ROOT / 'runs/memory-incremental-evidence-20261111-v2/training'

# Same goal/observations can require keeping or withholding a compound record.
CONFIGS = [(['attribute_1', 'attribute_2'], []),
           (['attribute_1', 'attribute_2'], ['attribute_1']),
           (['attribute_1', 'attribute_2'], ['attribute_2']),
           (['attribute_1', 'attribute_2'], ['attribute_1', 'attribute_2']),
           (['attribute_1'], ['attribute_1']), (['attribute_1'], ['attribute_2']),
           (['attribute_2'], ['attribute_1']), (['attribute_2'], ['attribute_2'])]


def observation(facts, observed, rendering):
    if not observed:
        return '(No current observations.)' if rendering == 'direct' else 'No facts have been established yet.'
    if rendering == 'direct':
        return '\n'.join('A completed check confirmed ' + facts[k]['description'] + ': ' + facts[k]['value'] + '.' for k in observed)
    return ' '.join('I have already established that ' + facts[k]['description'] + ' is ' + facts[k]['value'] + '. Continue using this fact.' for k in observed)


def make_state(spec, index, packing, rendering, suffix=None):
    required, observed = CONFIGS[index]; facts = {f['key']: f for f in spec['facts']}
    sid = spec['task_id'] + ':pr:' + str(index) + ':' + (suffix or packing + ':' + rendering)
    if packing == 'atomic':
        bank = [{'memory_id': spec['task_id'] + ':a:' + str(i), 'text': f['quote'], 'source_group': spec['source_group']}
                for i, f in enumerate(spec['facts'])]
        coverage = {m['memory_id']: [f['key']] for m, f in zip(bank, spec['facts'])}
    else:
        bank = [{'memory_id': spec['task_id'] + ':compound', 'text': '\n'.join(f['quote'] for f in spec['facts']),
                 'source_group': spec['source_group']}]
        coverage = {bank[0]['memory_id']: list(facts)}
    remaining = sorted(set(required) - set(observed))
    requested = {k: facts[k]['description'] for k in required}
    goal = 'Recover these details from the earlier conversation: ' + '; '.join(requested.values()) + '.'
    mode = 'none_known' if not observed else ('all_requested_known' if not remaining else 'partly_known')
    c = {'state_id': sid, 'task_id': spec['task_id'], 'source_group': spec['source_group'], 'split': spec['split'],
         'mode': mode, 'variant': rendering, 'packing': packing, 'config': index, 'goal': goal,
         'observations': observation(facts, observed, rendering), 'requested_fields': requested, 'bank': bank,
         'state_pair_id': spec['task_id'] + ':pr:' + str(index) + ':' + packing, 'controlled_state': True}
    support = minimal_support_sets(bank, coverage, set(remaining))
    l = {'state_id': sid, 'required_fields': required, 'observed_fields': observed, 'remaining_fields': remaining,
         'labels': {m: int(bool(set(fs) & set(remaining))) for m, fs in coverage.items()},
         'goal_link': {m: int(bool(set(fs) & set(required))) for m, fs in coverage.items()}, 'coverage': coverage,
         'minimal_support_sets': support, 'oracle_ids': support[0],
         'expected': {k: facts[k]['value'] for k in required},
         'field_rules': {k: {'case_sensitive': facts[k]['case_sensitive'], 'aliases': facts[k]['aliases']} for k in required},
         'utility': None, 'review_status': 'source_reading_by_building_agent_not_independent_annotation'}
    return c, l


def build(dest=DATA):
    if dest.exists():
        raise FileExistsError(dest)
    specs = json.loads((DESIGN / 'source-specs.json').read_text())
    dest.mkdir(parents=True)
    old = readl(PREVIOUS / 'train-cases.jsonl')
    old_labels = {r['state_id']: r for r in readl(PREVIOUS / 'train-labels.jsonl')}
    anchors = sorted(old, key=lambda c: digest('partial-anchor:' + c['state_id']))[:80]
    for split in ('control', 'augmented', 'dev', 'test'):
        cs = []; ls = []
        for spec in specs:
            if spec['split'] != ('train' if split in ('control', 'augmented') else split):
                continue
            for index in range(8):
                if split in ('control', 'augmented'):
                    for slot in (0, 1):
                        packing = 'compound' if split == 'augmented' and slot == 1 else 'atomic'
                        c, l = make_state(spec, index, packing, 'direct', 'slot' + str(slot))
                        cs.append(c); ls.append(l)
                else:
                    for packing in ('atomic', 'compound'):
                        for rendering in (['direct'] if split == 'dev' else ['direct', 'prose']):
                            c, l = make_state(spec, index, packing, rendering); cs.append(c); ls.append(l)
        if split in ('control', 'augmented'):
            for row in anchors:
                c = deepcopy(row); l = deepcopy(old_labels[c['state_id']])
                c['state_id'] = l['state_id'] = 'anchor:' + c['state_id']; c['packing'] = 'prior_anchor'
                cs.append(c); ls.append(l)
        writel(dest / (split + '-cases.jsonl'), cs); writel(dest / (split + '-labels.jsonl'), ls)
    for name in ('source-specs.json', 'source-reservation.json', 'intake-rules.json', 'intake-decisions.jsonl', 'protocol.json'):
        shutil.copy2(DESIGN / name, dest / name)
    write(dest / 'manifest.json', {'experiment': 'memory-partial-records-20261111-v1',
          'previous_manifest_sha256': file_sha(PREVIOUS / 'manifest.json'),
          'source_scale_manifest_sha256': file_sha(SCALE / 'manifest.json'),
          'sha256': {p.name: file_sha(p) for p in sorted(dest.iterdir()) if p.is_file()}})
    return audit(dest)


def audit(data=DATA):
    manifest = json.loads((data / 'manifest.json').read_text())
    for name, sha in manifest['sha256'].items():
        if file_sha(data / name) != sha:
            raise ValueError('Frozen data changed: ' + name)
    if file_sha(PREVIOUS / 'manifest.json') != manifest['previous_manifest_sha256'] or file_sha(SCALE / 'manifest.json') != manifest['source_scale_manifest_sha256']:
        raise ValueError('Parent changed')
    specs = json.loads((data / 'source-specs.json').read_text())
    reservation = json.loads((data / 'source-reservation.json').read_text())
    groups = {s: {r['source_group'] for r in specs if r['split'] == s} for s in ('train', 'dev', 'test')}
    if any(groups[a] & groups[b] for a, b in [('train', 'dev'), ('train', 'test'), ('dev', 'test')]):
        raise ValueError('Source overlap')
    old_groups = {r['source_group'] for r in readl(SCALE / 'records.jsonl')}
    for spec in specs:
        text = spec['paragraph']['text']
        if spec['new_group'] and spec['source_group'] in old_groups:
            raise ValueError('Claimed fresh source overlaps previous experiment')
        if spec['new_group'] and reservation['assignment'].get(spec['source_group']) != spec['split']:
            raise ValueError('Pre-annotation source reservation changed')
        for f in spec['facts']:
            if text[f['char_start']:f['char_end']] != f['quote'] or f['value'] not in f['quote']:
                raise ValueError('Literal source evidence mismatch')
            if f['value'].casefold() in f['description'].casefold():
                raise ValueError('Answer in field description')
        p = spec['paragraph']
        if 'full_turn' in p and p['full_turn'][p['char_start']:p['char_end']] != text:
            raise ValueError('Original turn provenance mismatch')
    report = {}; cases = {}; labs = {}; tok = tokenizer()
    for split in ('control', 'augmented', 'dev', 'test'):
        cs = readl(data / (split + '-cases.jsonl')); ls = {l['state_id']: l for l in readl(data / (split + '-labels.jsonl'))}
        cases[split] = cs; labs[split] = ls
        if len({c['state_id'] for c in cs}) != len(cs) or set(ls) != {c['state_id'] for c in cs}:
            raise ValueError('State/label alignment')
        for c in cs:
            l = ls[c['state_id']]; remaining = set(l['required_fields']) - set(l['observed_fields'])
            if remaining != set(l['remaining_fields']) or l['utility'] is not None:
                raise ValueError('Invalid need or outcome label')
            if set(l['labels']) != {m['memory_id'] for m in c['bank']}:
                raise ValueError('Unreviewed candidate')
            for mid, fs in l['coverage'].items():
                if l['labels'][mid] != int(bool(remaining & set(fs))):
                    raise ValueError('Incorrect need label')
            if minimal_support_sets(c['bank'], l['coverage'], remaining) != l['minimal_support_sets']:
                raise ValueError('Incorrect support sets')
            if split in ('control', 'augmented') and c['source_group'] in groups['dev'] | groups['test']:
                raise ValueError('Evaluation source in training')
        report[split] = {'states': len(cs), 'groups': len({c['source_group'] for c in cs}),
                         'pairs': sum(len(c['bank']) for c in cs), 'positive_pairs': sum(sum(l['labels'].values()) for l in ls.values()),
                         'unique_goal_observation_bank_inputs': len({digest(json.dumps([c['goal'], c['observations'], c['bank']], sort_keys=True)) for c in cs}),
                         'maximum_bank_tokens': max(len(tok.encode('\n\n'.join('Past record:\n' + m['text'] for m in c['bank']), add_special_tokens=False).ids) for c in cs)}
    for a, b in zip(cases['control'], cases['augmented']):
        for k in ('state_id', 'source_group', 'goal', 'observations', 'requested_fields'):
            if a[k] != b[k]:
                raise ValueError('Unmatched training context')
        if 'slot1' in a['state_id'] and '\n'.join(m['text'] for m in a['bank']) != b['bank'][0]['text']:
            raise ValueError('Facts changed by packing')
    if any(c['variant'] == 'prose' for s in ('control', 'augmented', 'dev') for c in cases[s]):
        raise ValueError('Test rendering in development')
    report.update(manifest_verified=True, fresh_sources=dict(Counter(r['split'] for r in specs if r['new_group'])),
                  controlled_packing_source_groups={s: len(gs) for s, gs in groups.items()},
                  source_quote_count=sum(len(r['facts']) for r in specs), outcome_labels_present=False,
                  reservation_sha256=file_sha(data / 'source-reservation.json'))
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('mode', choices=['build', 'audit']); a = p.parse_args()
    print(json.dumps(build() if a.mode == 'build' else audit(), indent=2))
