"""Matched packing exposure versus partial-observation coverage, with literal facts preserved."""
import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import shutil

from data_pipeline.collect_relation_source_pilot import readl, writel, write, file_sha, digest
from data_pipeline.partial_record_data import DATA as PARENT, audit as parent_audit, make_state, tokenizer
from data_pipeline.incremental_evidence_data import minimal_support_sets
from training.memory_selector.incremental_evidence_protocol import pair_text, memory_text

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'datasets/memory_augmentation_ablation/20261112-v1'
DESIGN = ROOT / 'docs/design-data/memory-augmentation-ablation-20261112-v1'
WORK = ROOT / 'runs/memory-augmentation-ablation-20261112-v1'
ARMS = ('repeat', 'format', 'relation')
PARTIAL_CONFIGS = (1, 2, 5, 6)
GOAL_FRAMES = (
    'Recover these details from the earlier conversation: {}.',
    'Using the earlier conversation, recover the following details: {}.',
    'Find the following details in the earlier conversation: {}.',
    'Report these details from the earlier conversation: {}.',
)


def experimental_row(spec, config, arm, source_index):
    """Slot 1 differs; slot 0 and old anchors are untouched in all arms."""
    c, l = make_state(spec, config, 'atomic' if arm == 'repeat' else 'compound', 'direct', 'slot1')
    if arm != 'repeat':
        view = (source_index + config) % len(GOAL_FRAMES)
        c['goal'] = GOAL_FRAMES[view].format('; '.join(c['requested_fields'].values()))
        c['goal_view'] = view
    if arm == 'format' and config in PARTIAL_CONFIGS:
        # Format exposure includes only none-known/all-requested-known compound
        # states. Partial states remain in the shared ATOMIC training branch.
        # Removing observations here does not change this record's positive label.
        c['observations'] = '(No current observations.)'
        c['mode'] = 'none_known'
        l['observed_fields'] = []
        l['remaining_fields'] = list(l['required_fields'])
        l['minimal_support_sets'] = minimal_support_sets(c['bank'], l['coverage'], set(l['remaining_fields']))
        l['oracle_ids'] = l['minimal_support_sets'][0]
    c['ablation_arm'] = arm
    return c, l


def protocol():
    p = deepcopy(json.loads((PARENT / 'protocol.json').read_text()))
    p.update(experiment='memory-augmentation-ablation-20261112-v1',
        status='frozen_before_new_training',
        question='With packing, facts, per-row need labels, unique input count, initial weights and updates matched, does exposing compound records under partial observation improve need decisions over format exposure alone?',
        partition='Reuse the original train/dev/test source partition. Test source groups remain absent from training, but their results have already been inspected; this is a mechanism diagnostic, not fresh confirmation.',
        selection='Primary fixed threshold 0.5. No threshold, checkpoint, arm or seed selected on evaluation outcomes.',
        evaluation='Reuse all 384 prior test states as a previously observed diagnostic. Primary 48 partly observed compound states; also all states, atomic controls and old BEAM development regression. Score all models after training and freeze their hashes first.',
        worker_policy='Reuse exact-message-hash cached Qwen3.5-4B outputs for all 96 compound/prose states. Missing cache entries stop evaluation; no new worker calls.',
        reporting='Primary contrast is relation minus format. Repeat is an additional descriptive reference, with fewer unique inputs and a different packing distribution. Form/rel have identical banks, requested facts, per-row need/goal labels and common atomic/anchor rows. Observation information differs by design in 80 rows; selector text length is not exactly matched. No general retrieval superiority or fresh-source confirmation claim.',
        primary_contrast={'arms': ['format', 'relation'], 'threshold': .5,
            'metric': 'compound_partial_retained / 48',
            'secondary': ['complete_missing_coverage / 240', 'source_macro_balanced_accuracy', 'no_need_abstained',
                          'unnecessary_records', 'cached_worker_supported_correct / 96', 'old_context_false_positive'],
            'direction': 'Report both seeds and every stratum, regardless of direction; no outcome-dependent extension.'})
    p['training'].update(arms=list(ARMS), seeds=[731, 1729], epochs=2, anchor_states=80,
        loss='Unchanged parent need-only weighted cross entropy; 400 state entries, batch 4, 200 updates. Format/relation have identical per-row candidate labels and therefore identical loss weights. Repeat retains the original atomic replay data.')
    p['bounds'].update(max_training_seconds=7200, max_unique_worker_calls=0)
    p.pop('calibration', None)
    return p


def build(dest=DATA):
    parent_audit()
    if dest.exists():
        raise FileExistsError(dest)
    dest.mkdir(parents=True)
    specs = json.loads((PARENT / 'source-specs.json').read_text())
    train = sorted((s for s in specs if s['split'] == 'train'), key=lambda s: s['source_group'])
    source_index = {s['task_id']: i for i, s in enumerate(train)}
    spec_by_id = {s['task_id']: s for s in train}
    old = readl(PARENT / 'control-cases.jsonl')
    labs = {r['state_id']: r for r in readl(PARENT / 'control-labels.jsonl')}
    for arm in ARMS:
        cs, ls = [], []
        for c in old:
            if c['state_id'].endswith(':slot1'):
                x, y = experimental_row(spec_by_id[c['task_id']], c['config'], arm, source_index[c['task_id']])
            else:
                x, y = deepcopy(c), deepcopy(labs[c['state_id']])
                x['ablation_arm'] = arm
            cs.append(x); ls.append(y)
        writel(dest / (arm + '-cases.jsonl'), cs)
        writel(dest / (arm + '-labels.jsonl'), ls)
    for name in ('source-specs.json', 'source-reservation.json', 'dev-cases.jsonl', 'dev-labels.jsonl',
                 'test-cases.jsonl', 'test-labels.jsonl'):
        shutil.copy2(PARENT / name, dest / name)
    write(dest / 'protocol.json', protocol())
    write(dest / 'manifest.json', dict(experiment='memory-augmentation-ablation-20261112-v1',
        parent_manifest_sha256=file_sha(PARENT / 'manifest.json'),
        sha256={p.name: file_sha(p) for p in sorted(dest.iterdir()) if p.is_file()}))
    return audit(dest)


def audit(data=DATA):
    parent = parent_audit()
    manifest = json.loads((data / 'manifest.json').read_text())
    if file_sha(PARENT / 'manifest.json') != manifest['parent_manifest_sha256']:
        raise ValueError('Parent manifest changed')
    for name, expected in manifest['sha256'].items():
        if file_sha(data / name) != expected:
            raise ValueError('Frozen data changed: ' + name)
    cases, labels, report = {}, {}, {}
    tok = tokenizer()
    for split in (*ARMS, 'dev', 'test'):
        cs = readl(data / (split + '-cases.jsonl'))
        ls = {r['state_id']: r for r in readl(data / (split + '-labels.jsonl'))}
        cases[split], labels[split] = cs, ls
        if len(cs) != len(ls) or len({c['state_id'] for c in cs}) != len(cs):
            raise ValueError('Duplicate or missing state')
        groups = {c['source_group'] for c in cs}
        model_inputs = {tuple(pair_text(c, m) for m in c['bank']) for c in cs}
        partial_compound = 0
        for c in cs:
            l = ls[c['state_id']]
            remaining = set(l['required_fields']) - set(l['observed_fields'])
            if remaining != set(l['remaining_fields']) or l['utility'] is not None:
                raise ValueError('Invalid state/utility label')
            if {m['memory_id'] for m in c['bank']} != set(l['labels']):
                raise ValueError('Candidate alignment')
            if any(l['labels'][mid] != int(bool(set(fs) & remaining)) for mid, fs in l['coverage'].items()):
                raise ValueError('Invalid need label')
            if minimal_support_sets(c['bank'], l['coverage'], remaining) != l['minimal_support_sets']:
                raise ValueError('Invalid sufficient set')
            if c.get('packing') == 'compound' and l['observed_fields'] and remaining:
                partial_compound += 1
        report[split] = dict(states=len(cs), source_groups=len(groups), unique_selector_state_inputs=len(model_inputs),
            candidate_pairs=sum(len(c['bank']) for c in cs), positive_pairs=sum(sum(l['labels'].values()) for l in ls.values()),
            packing_counts=dict(Counter(c['packing'] for c in cs)), partially_observed_compound_states=partial_compound,
            max_bank_tokens=max(len(tok.encode(memory_text(c, [m['memory_id'] for m in c['bank']]), add_special_tokens=False).ids) for c in cs))
    changes = 0
    for a, b in zip(cases['format'], cases['relation']):
        sid = a['state_id']
        for key in ('state_id', 'source_group', 'task_id', 'goal', 'requested_fields', 'bank', 'packing'):
            if a[key] != b[key]:
                raise ValueError('Unmatched primary contrast: ' + key)
        for key in ('labels', 'goal_link', 'expected', 'field_rules', 'coverage'):
            if labels['format'][sid].get(key) != labels['relation'][sid].get(key):
                raise ValueError('Primary label/fact mismatch: ' + key)
        changes += a['observations'] != b['observations']
    if changes != 80 or report['format']['unique_selector_state_inputs'] != report['relation']['unique_selector_state_inputs']:
        raise ValueError('Wrong manipulation or input variety')
    for key in ('candidate_pairs', 'positive_pairs', 'packing_counts', 'source_groups', 'states'):
        if report['format'][key] != report['relation'][key]:
            raise ValueError('Unmatched primary distribution: ' + key)
    from training.memory_selector.partial_record_protocol import loss_weights
    wa, ma = loss_weights(cases['format'], labels['format'])
    wb, mb = loss_weights(cases['relation'], labels['relation'])
    if wa != wb or ma != mb:
        raise ValueError('Primary loss mass differs')
    # The exact original replay is a reference, not the primary causal contrast.
    original = readl(PARENT / 'control-cases.jsonl')
    if any(pair_text(a, ma) != pair_text(b, mb) for a, b in zip(cases['repeat'], original)
           for ma, mb in zip(a['bank'], b['bank'])):
        raise ValueError('Repeat reference differs from parent')
    sets = {s: {pair_text(c, m) for c in cs for m in c['bank']} for s, cs in cases.items()}
    for arm in ARMS:
        train_groups = {c['source_group'] for c in cases[arm]}
        for split in ('dev', 'test'):
            if train_groups & {c['source_group'] for c in cases[split]} or sets[arm] & sets[split]:
                raise ValueError('Training/evaluation overlap')
    # Frame assignment is counterbalanced over sources, not a label-coded suffix.
    frame_labels = defaultdict_counter(cases['relation'], labels['relation'])
    if len({tuple(v) for v in frame_labels.values()}) != 1:
        raise ValueError('Goal frame predicts the need label')
    report.update(parent_verified=parent['manifest_verified'], primary_observation_changes=changes,
                  primary_loss_weights_identical=True, per_goal_frame_negative_positive=frame_labels,
                  evaluation_already_observed=True, outcome_labels_present=False)
    return report


def defaultdict_counter(cases, labels):
    out = {str(i): [0, 0] for i in range(len(GOAL_FRAMES))}
    for c in cases:
        if 'goal_view' in c:
            for y in labels[c['state_id']]['labels'].values():
                out[str(c['goal_view'])][y] += 1
    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('build', 'audit'))
    args = parser.parse_args()
    print(json.dumps(build() if args.mode == 'build' else audit(), indent=2))
