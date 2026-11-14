"""Connect the full fresh measurement version to a fixed four-arm experiment."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import shutil

from data_pipeline.memory_decision_dataset import ROOT, readl, write, digest
from data_pipeline.export_memory_worker_refresh import verify_dataset, check
from data_pipeline.prepare_memory_state_ablation import CLASSES, family_folds

FRESH = ROOT / 'datasets/memory_decisions/20261110-refresh-v1'
CONFIG = ROOT / 'training/memory_selector/configs/memory-refreshed-learning-v1.json'
COPIED = ('examples.jsonl', 'targets.jsonl', 'all-supervision.jsonl', 'original-example-ids.json', 'folds.json')


def load(path):
    return json.loads(path.read_text())


def prepare(output):
    if output.exists():
        raise FileExistsError(output)
    check(ROOT / 'evaluation/results/memory-worker-refresh-20261110-v1')
    output.mkdir(parents=True)
    for name in COPIED:
        shutil.copy2(FRESH / name, output / name)
    config = load(CONFIG)
    write(output / 'config.json', config)
    write(output / 'protocol.json', {
        'experiment': config['experiment'], 'available_arms': config['arms'], 'classes': list(CLASSES),
        'measurement_version': config['worker_measurement_version'],
        'fresh_dataset_manifest_sha256': digest((FRESH / 'manifest.json').read_bytes()),
        'original_rows': 237, 'new_rows': 171, 'all_rows': 408, 'states': 96, 'families': 12,
        'training': '64 states/fold, every retained candidate eight times; same seeded state order and four states/update.',
        'learner_input': 'Only texts.goal_and_observations; IDs/folds/relationship bits/outcomes are not neural inputs.',
        'primary_evaluation_pool': 'expanded', 'secondary_evaluation_pool': 'original',
        'evaluation': 'All four arms score identical held-family pools; all 408 candidates cross-fitted once per arm/seed.',
        'selection': 'P(gain)-P(harm), withhold at score <= 0; ties by memory tokens then example ID.',
        'checkpoint_selection': 'Fixed final epoch, no tuning or checkpoint choice on held families.',
        'data_budget': 'State exposure/update count matched; candidate and token exposure differs and is reported.',
        'new_worker_calls': 0, 'new_independent_families': 0,
        'old_output_labels_used': False, 'actual_progress_or_failure_history_added': False,
        'cost_scope': 'Measure candidate scoring and delivered tokens; no end-to-end latency claim across local selector and stored L4 worker.',
        'scope': 'Exploratory development-family cross-fitting. No new held-out source validation or model-size comparison.'})
    write(output / 'manifest.json', {
        'files_sha256': {p.name: digest(p.read_bytes()) for p in sorted(output.iterdir())},
        'source_sha256': {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in (CONFIG, FRESH / 'manifest.json', Path(__file__))}})
    return validate(output)


def validate(path):
    verify_dataset(FRESH)
    manifest = load(path / 'manifest.json')
    for name, sha in manifest['files_sha256'].items():
        if digest((path / name).read_bytes()) != sha:
            raise ValueError('Changed learning input: ' + name)
    for name, sha in manifest['source_sha256'].items():
        if digest((ROOT / name).read_bytes()) != sha:
            raise ValueError('Changed refreshed-learning source: ' + name)
    for name in COPIED:
        if (path / name).read_bytes() != (FRESH / name).read_bytes():
            raise ValueError('Learning data differs from the fresh measurement source: ' + name)
    config, protocol = load(path / 'config.json'), load(path / 'protocol.json')
    if config != load(CONFIG) or protocol['available_arms'] != config['arms']:
        raise ValueError('Changed fixed training design')
    if protocol['fresh_dataset_manifest_sha256'] != digest((FRESH / 'manifest.json').read_bytes()):
        raise ValueError('Old or mixed worker measurement version')
    examples, targets = readl(path / 'examples.jsonl'), readl(path / 'targets.jsonl')
    if any(r['measurement_version'] != config['worker_measurement_version'] for r in targets):
        raise ValueError('Old worker labels entered the experiment')
    folds = load(path / 'folds.json')
    if folds != family_folds([r['family_id'] for r in examples], config['folds']):
        raise ValueError('Parent-family folds changed')
    owners = defaultdict(set)
    for row, target in zip(examples, targets):
        for key in (row['state_id'], target['generation_id'], target['baseline_generation_id']):
            owners[key].add(row['family_id'])
    if any(len(v) != 1 for v in owners.values()):
        raise ValueError('Shared observations cross family boundaries')
    originals = set(load(path / 'original-example-ids.json'))
    grid = []
    for fold in folds:
        for arm in config['arms']:
            train = [r for r in examples if r['family_id'] in fold['train_families']
                     and (arm.startswith('augmented') or r['example_id'] in originals)]
            held = [r for r in examples if r['family_id'] in fold['held_families']]
            if len({r['state_id'] for r in train}) != 64 or len({r['state_id'] for r in held}) != 32:
                raise ValueError('Unequal train/held state population')
            if {r['example_id'] for r in train} & {r['example_id'] for r in held}:
                raise ValueError('Family leakage')
            grid.append({'fold': fold['fold'], 'arm': arm, 'train_rows': len(train), 'held_rows': len(held)})
    return {'rows': len(examples), 'original_rows': len(originals), 'states': 96, 'families': 12,
        'four_way_counts': dict(Counter(CLASSES[t['class_id']] for t in targets)),
        'planned_fits': len(grid) * len(config['seeds']), 'grid': grid,
        'all_labels_fresh': True, 'new_worker_calls': 0}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('prepare', 'check'))
    p.add_argument('--path', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(prepare(a.path) if a.command == 'prepare' else validate(a.path), indent=2))
