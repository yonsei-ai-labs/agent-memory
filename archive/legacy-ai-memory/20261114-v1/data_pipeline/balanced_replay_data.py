"""Matched continuation/replay inputs and untouched-source field contrasts."""
from collections import Counter
from copy import deepcopy
import argparse
import json
from pathlib import Path
import shutil

from data_pipeline.collect_relation_source_pilot import readl, writel, write, digest, file_sha, groups_for_questions
from data_pipeline.partial_record_data import make_state, tokenizer
from training.memory_selector import context_value_protocol as context

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'datasets/memory_balanced_replay/20261112-v1'
WORK = ROOT / 'runs/memory-balanced-replay-20261112-v1'
DESIGN = ROOT / 'docs/design-data/memory-balanced-replay-20261112-v1'
PARENT = ROOT / 'datasets/memory_partial_records/20261111-v1'
INITIAL = ROOT / 'runs/memory-partial-records-20261111-v1/training'
SCALE = ROOT / 'datasets/memory_source_scale/20261111-v1'
PREVIOUS = ROOT / 'datasets/memory_incremental_evidence/20261111-v2'
RAW = ROOT / 'runs/memory-relation-intake-20261111-v1/raw'


def fresh_state(spec, config, packing):
    c, l = make_state(spec, config if config < 8 else 0 if config == 8 else 1, packing, 'prose')
    sid = spec['task_id'] + ':brfresh:' + str(config) + ':' + packing
    c.update(state_id=sid, config=config, study_split='fresh', renderer='plain')
    l['state_id'] = sid
    if config >= 8:
        key = 'attribute_1' if config == 8 else 'attribute_2'
        desc = next(f['description'] for f in spec['facts'] if f['key'] == key)
        failed = 'An attempted lookup for ' + desc + ' failed and established no value.'
        c['observations'] = failed if config == 8 else c['observations'] + '\n' + failed
        c['mode'] = 'failed_lookup' if config == 8 else 'partly_known_failed_lookup'
    return c, l


def make_replay():
    cs = readl(context.DATA / 'train-cases.jsonl')
    labs = {l['state_id']: l for l in readl(context.DATA / 'train-labels.jsonl')}
    rows = []; labels = []; ledger = []
    for c in cs:
        if c['mode'] == 'updated':
            # No need to supervise uncertain "new setting attempted" or old
            # scope still needed cases for the requested missing/known contrast.
            continue
        row = deepcopy(c); label = deepcopy(labs[c['state_id']]); row['renderer'] = 'context'
        rows.append(row); labels.append(label)
        if c['mode'] == 'missing':
            failed = deepcopy(row); fl = deepcopy(label)
            failed['state_id'] = fl['state_id'] = c['case_id'] + ':failed_lookup'
            failed['mode'] = fl['mode'] = 'failed_lookup'
            failed['observations'] = 'A lookup for the requested information failed. No value was established.'
            failed['observation_records'] = [{'time': 'current', 'text': failed['observations'], 'controlled_observation': True}]
            rows.append(failed); labels.append(fl)
            ledger.append({'origin': c['case_id'], 'domain': 'beam_train', 'review': 'Reuse reviewed source labels for missing, known and unrelated-observation. Add no-value failed lookup, same need labels. Updated condition not fitted; preserve unknown masks.', 'source_group': c['source_group']})
    ids = json.loads((DESIGN / 'scale-replay-candidates.json').read_text())
    scale_cs = readl(SCALE / 'train-120-cases.jsonl'); scale_ls = {l['state_id']: l for l in readl(SCALE / 'train-120-labels.jsonl')}
    for tid in ids:
        source_rows = [c for c in scale_cs if c['task_id'] == tid]
        for c in source_rows:
            row = deepcopy(c); label = deepcopy(scale_ls[c['state_id']]); row['renderer'] = 'plain'
            ambiguous = [m['memory_id'] for m in row['bank'] if (tid == 'qa_297' and '34.052235' in m['text']) or (tid == 'qa_316' and 'Apple garden' in m['text'])]
            if c['mode'] != 'known':
                for mid in ambiguous: label['labels'].pop(mid, None)
            if c['mode'] == 'unrelated' and any(row['observations'] == m['text'] for m in row['bank'] if m['memory_id'] in ambiguous):
                row['observations'] = next(m['text'] for m in row['bank'] if label['labels'].get(m['memory_id']) == 0)
            rows.append(row); labels.append(label)
            if c['mode'] == 'missing':
                failed = deepcopy(row); fl = deepcopy(label)
                failed['state_id'] = fl['state_id'] = tid + '--failed_lookup'
                failed['mode'] = 'failed_lookup'; failed['observations'] = 'An attempted lookup for the requested details failed and established no value.'
                rows.append(failed); labels.append(fl)
                ledger.append({'origin': tid, 'domain': 'mem2act_train', 'source_group': c['source_group'],
                    'review': 'Reviewed all ten original candidate passages. Near-coincident coordinates and metaphorical Apple mention remain unlabelled in missing-information states; known state still requires no additional fact.', 'masked_candidate_ids': ambiguous})
    if len(rows) != 80: raise ValueError('Expected 52 BEAM and 28 Mem2Act replay states')
    return rows, labels, ledger


def build(dest=DATA):
    if dest.exists(): raise FileExistsError(dest)
    parent_cs = readl(PARENT / 'augmented-cases.jsonl')
    parent_ls = {l['state_id']: l for l in readl(PARENT / 'augmented-labels.jsonl')}
    new_cs, new_ls, ledger = make_replay(); dest.mkdir(parents=True)
    for arm in ('control', 'replay'):
        cs = []; ls = []; slot = 0
        for old in parent_cs:
            is_anchor = old['state_id'].startswith('anchor:')
            c = deepcopy(new_cs[slot] if is_anchor and arm == 'replay' else old)
            l = deepcopy(new_ls[slot] if is_anchor and arm == 'replay' else parent_ls[old['state_id']])
            c['origin_state_id'] = c['state_id']; c['training_bucket'] = 'replay' if is_anchor else 'core'
            if is_anchor:
                c['state_id'] = l['state_id'] = 'replay-slot-' + str(slot).zfill(2); slot += 1
            c.setdefault('renderer', 'plain'); cs.append(c); ls.append(l)
        writel(dest / (arm + '-cases.jsonl'), cs); writel(dest / (arm + '-labels.jsonl'), ls)
    fresh_cs = []; fresh_ls = []
    for spec in json.loads((DESIGN / 'source-specs.json').read_text()):
        for config in range(10):
            for packing in ('atomic', 'compound'):
                c, l = fresh_state(spec, config, packing); fresh_cs.append(c); fresh_ls.append(l)
    writel(dest / 'fresh-cases.jsonl', fresh_cs); writel(dest / 'fresh-labels.jsonl', fresh_ls)
    writel(dest / 'replay-review.jsonl', ledger)
    # Previously viewed test becomes an explicitly named regression set.
    for prefix, parent, split in [('partial-regression', PARENT, 'test'), ('beam-regression', context.DATA, 'dev'),
                                  ('incremental-regression', PREVIOUS, 'dev'), ('source-regression', SCALE, 'dev')]:
        for kind in ('cases', 'labels'):
            shutil.copy2(parent / (split + '-' + kind + '.jsonl'), dest / (prefix + '-' + kind + '.jsonl'))
    for name in ('source-specs.json', 'protocol.json', 'intake-rule.json', 'scale-replay-candidates.json'):
        shutil.copy2(DESIGN / name, dest / name)
    write(dest / 'manifest.json', {'experiment': 'memory-balanced-replay-20261112-v1',
        'parent_manifests': {str(p.relative_to(ROOT)): file_sha(p) for p in [PARENT / 'manifest.json', SCALE / 'manifest.json', PREVIOUS / 'manifest.json', context.DATA / 'manifest.json']},
        'sha256': {p.name: file_sha(p) for p in sorted(dest.iterdir()) if p.is_file()}})
    return audit(dest)


def audit(data=DATA):
    manifest = json.loads((data / 'manifest.json').read_text())
    for name, sha in manifest['sha256'].items():
        if file_sha(data / name) != sha: raise ValueError('Frozen data changed: ' + name)
    for name, sha in manifest['parent_manifests'].items():
        if file_sha(ROOT / name) != sha: raise ValueError('Previous experiment changed')
    raw_manifest = json.loads((PARENT / 'source-reservation.json').read_text())
    for name, info in raw_manifest['raw_source_files'].items():
        if file_sha(RAW / name) != info['sha256']: raise ValueError('Raw source changed')
    sessions = readl(RAW / 'mem2act-sessions.jsonl'); qs = readl(RAW / 'mem2act-qa.jsonl')
    groups, _, _ = groups_for_questions(qs, sessions, True)
    qgroup = {q['qa_id']: g for g, rows in groups.items() for q in rows}; by_session = {s['session_id']: s for s in sessions}
    prior_groups = {s['source_group'] for s in json.loads((PARENT / 'source-specs.json').read_text())}
    scale_groups = {r['source_group'] for r in readl(SCALE / 'records.jsonl')}
    specs = json.loads((data / 'source-specs.json').read_text()); fresh_groups = {s['source_group'] for s in specs}
    if len(fresh_groups) != 12 or fresh_groups & (prior_groups | scale_groups): raise ValueError('Fresh source overlap')
    for s in specs:
        p = s['paragraph']; turn = by_session[p['session_id']]['turns'][p['turn']]
        if qgroup[s['task_id']] != s['source_group'] or raw_manifest['assignment'][s['source_group']] != 'test': raise ValueError('Source reservation mismatch')
        if turn['role'] != 'user' or turn['source_id'] != p['source_id'] or p['full_turn'] not in turn['content'] or p['full_turn'][p['char_start']:p['char_end']] != p['text']:
            raise ValueError('Original passage mismatch')
        for f in s['facts']:
            if p['text'][f['char_start']:f['char_end']] != f['quote'] or f['value'] not in f['quote'] or f['value'].casefold() in f['description'].casefold(): raise ValueError('Invalid source field')
    report = {}; splits = {}; labs = {}
    for split in ('control', 'replay', 'fresh', 'partial-regression', 'beam-regression', 'incremental-regression', 'source-regression'):
        cs = readl(data / (split + '-cases.jsonl')); ls = {l['state_id']: l for l in readl(data / (split + '-labels.jsonl'))}
        if len(cs) != len({c['state_id'] for c in cs}) or set(ls) != {c['state_id'] for c in cs}: raise ValueError('Label alignment')
        splits[split] = cs; labs[split] = ls
        for c in cs:
            if not set(ls[c['state_id']]['labels']) <= {m['memory_id'] for m in c['bank']}: raise ValueError('Unknown supervised candidate')
            if split in ('control', 'replay') and c['split'] != 'train': raise ValueError('Evaluation data used to train')
        report[split] = {'states': len(cs), 'groups': len({c['source_group'] for c in cs}),
                         'candidate_pairs': sum(len(c['bank']) for c in cs), 'supervised_pairs': sum(len(l['labels']) for l in ls.values()),
                         'positive_pairs': sum(sum(l['labels'].values()) for l in ls.values())}
    for a, b in zip(splits['control'], splits['replay']):
        if a['state_id'] != b['state_id']: raise ValueError('Update positions differ')
        if a['training_bucket'] == 'core' and (a != b or labs['control'][a['state_id']] != labs['replay'][b['state_id']]): raise ValueError('Core contrasts changed')
    train_groups = {c['source_group'] for s in ('control', 'replay') for c in splits[s]}
    training_memory_groups = {m['source_group'] for s in ('control', 'replay') for c in splits[s] for m in c['bank']}
    evaluation_groups = {c['source_group'] for s in splits if s not in ('control', 'replay') for c in splits[s]}
    if (train_groups | training_memory_groups) & evaluation_groups: raise ValueError('Source overlap through targets or distractors')
    fresh_ls = labs['fresh']
    for c in splits['fresh']:
        l = fresh_ls[c['state_id']]; rem = set(l['required_fields']) - set(l['observed_fields'])
        if rem != set(l['remaining_fields']) or any(y != int(bool(set(l['coverage'][mid]) & rem)) for mid, y in l['labels'].items()): raise ValueError('Incorrect fresh need label')
    tok = tokenizer(); banks = []; support = []
    for c in splits['beam-regression']:
        l = labs['beam-regression'][c['state_id']]
        banks.append(len(tok.encode(context.memory_text(c, [m['memory_id'] for m in c['bank']]), add_special_tokens=False).ids))
        support.append(len(tok.encode(context.memory_text(c, l['oracle_ids']), add_special_tokens=False).ids))
    if not min(banks) > 128 or max(support) > 128: raise ValueError('No fair budget competition')
    report.update(manifest_verified=True, fresh_independent_groups=12, unchanged_core_states=320,
                  beam_full_bank_token_range=[min(banks), max(banks)], beam_max_reviewed_support_tokens=max(support),
                  sealed_beam_confirm_used=False, outcome_labels_for_training=False)
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('mode', choices=['build', 'audit']); a = p.parse_args()
    print(json.dumps(build() if a.mode == 'build' else audit(), indent=2))
