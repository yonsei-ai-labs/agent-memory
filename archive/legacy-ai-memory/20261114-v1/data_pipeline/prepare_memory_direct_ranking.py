"""Freeze direct-success training without changing the measured data or labels."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import shutil

from data_pipeline.memory_decision_dataset import ROOT, digest, readl, write, write_rows
from data_pipeline.export_memory_worker_refresh import verify_dataset

SOURCE = ROOT / 'datasets/memory_decisions/20261110-refresh-v1'
CONFIG = ROOT / 'training/memory_selector/configs/memory-direct-ranking-v1.json'


def load(path):
    return json.loads(path.read_text())


def state_text(text):
    before, marker, _ = text.partition('\nProposed memory bundle:')
    if not marker:
        raise ValueError('Cannot separate current state from proposed memory')
    # Separate baseline estimator uses no candidate record or candidate-derived feature.
    _, marker, context = before.partition('\nCurrent goal:')
    if not marker:
        raise ValueError('Missing current goal')
    return 'Instruct: Estimate current task success without supplying past records.\nCurrent goal:' + context


def prepare(path):
    if path.exists():
        raise FileExistsError(path)
    verify_dataset(SOURCE)
    examples, targets = readl(SOURCE / 'examples.jsonl'), readl(SOURCE / 'targets.jsonl')
    lookup = {r['example_id']: r for r in targets}
    states, state_targets = {}, {}
    for row in examples:
        sid = row['state_id']
        value = {'state_id': sid, 'family_id': row['family_id'], 'text': state_text(row['texts']['goal_and_observations'])}
        target = {'state_id': sid, 'without': lookup[row['example_id']]['without']}
        if (sid in states and states[sid] != value) or (sid in state_targets and state_targets[sid] != target):
            raise ValueError('Candidate changes the no-memory state input/target')
        states[sid], state_targets[sid] = value, target
    folds = load(SOURCE / 'folds.json')
    for fold in folds:
        ordered = sorted(fold['train_families'], key=lambda x: digest(('direct-rank-inner:' + x).encode()))
        fold['inner_validation_families'] = ordered[:2]
        fold['inner_train_families'] = ordered[2:]
    path.mkdir(parents=True)
    shutil.copy2(CONFIG, path / 'config.json')
    write(path / 'folds.json', folds)
    write_rows(path / 'state-inputs.jsonl', [states[k] for k in sorted(states)])
    write_rows(path / 'state-targets.jsonl', [state_targets[k] for k in sorted(states)])
    write(path / 'protocol.json', {
        'source': str(SOURCE.relative_to(ROOT)), 'source_inputs_unchanged': True,
        'primary': 'Rank nonempty measured bundles by one logit for task success if provided; always provide the top bundle.',
        'secondary': 'Compare the selected bundle probability with a separate no-memory head on frozen base state embeddings; threshold 0, no cost tuning.',
        'labels': 'Existing binary task_success. Grounded success and UNKNOWN are reported separately, not relabeled.',
        'loss': 'State-mean BCE plus lambda * mean of within-state positive-negative softplus differences over informative training states.',
        'normalization': 'Uniform state batches; multiply each informative state rank mean by train_states / informative_train_states.',
        'inner_selection': 'One fixed seed 731, 6 train / 2 validation families per outer fold; choose among 8/16/24 epochs by validation top-choice success, then state-macro pair accuracy, then lower BCE, then fewer epochs.',
        'outer_refit': 'Fresh base/adapter/head on all 8 outer train families at the chosen epoch; two seeds, both reported; outer held outputs never select settings.',
        'no_memory_head': 'Frozen pinned base encodes only current state. Independent scalar MLP is trained on CPU; choose 16/64/128/256 epochs on inner validation BCE, then refit on all outer train states.',
        'fixed_hyperparameters': 'Lambda=1, learning rates, threshold, head width and LoRA rank are fixed; only epoch count selected internally.',
        'fits': {'inner_lora': 6, 'outer_lora': 12, 'baseline_cpu_outer': 6},
        'comparison_to_old_run': 'Reference only: the new scalar objective and batch organization differ; causal loss comparison is binary versus binary_rank.',
        'scope': 'Already explored 12 development families. No new independent test, no new worker calls, no retriever features or warm starts.',
        'stop': 'Complete these two arms or stop with an incomplete artifact at the four-hour limit; no automatic method or hyperparameter expansion.'})
    write(path / 'manifest.json', {
        'files_sha256': {p.name: digest(p.read_bytes()) for p in sorted(path.iterdir())},
        'source_sha256': {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in
                          [CONFIG, Path(__file__), *[SOURCE / n for n in ('examples.jsonl', 'targets.jsonl', 'folds.json', 'all-supervision.jsonl', 'manifest.json')]]}})
    return validate(path)


def validate(path):
    manifest = load(path / 'manifest.json')
    for name, expected in manifest['files_sha256'].items():
        if digest((path / name).read_bytes()) != expected:
            raise ValueError('Changed direct-ranking design: ' + name)
    for name, expected in manifest['source_sha256'].items():
        if digest((ROOT / name).read_bytes()) != expected:
            raise ValueError('Changed source: ' + name)
    if load(path / 'config.json') != load(CONFIG):
        raise ValueError('Configuration changed')
    rows = readl(SOURCE / 'examples.jsonl'); targets = {r['example_id']: r for r in readl(SOURCE / 'targets.jsonl')}
    states = {r['state_id']: r for r in readl(path / 'state-inputs.jsonl')}
    state_targets = {r['state_id']: r for r in readl(path / 'state-targets.jsonl')}
    for row in rows:
        if states[row['state_id']]['text'] != state_text(row['texts']['goal_and_observations']):
            raise ValueError('Baseline state depends on candidate')
        if state_targets[row['state_id']]['without'] != targets[row['example_id']]['without']:
            raise ValueError('Baseline target mismatch')
    grid = []
    for f in load(path / 'folds.json'):
        train, val, held = (set(f[k]) for k in ('inner_train_families', 'inner_validation_families', 'held_families'))
        if (len(train), len(val), len(held)) != (6, 2, 4) or train & val or train & held or val & held or train | val != set(f['train_families']):
            raise ValueError('Leaky nested family split')
        grouped = defaultdict(list)
        for r in rows:
            if r['family_id'] in val:
                grouped[r['state_id']].append(targets[r['example_id']]['with'])
        grid.append({'fold': f['fold'], 'inner_validation_states': len(grouped),
                     'inner_validation_informative_states': sum(len(set(v)) > 1 for v in grouped.values())})
    return {'examples': len(rows), 'states': len(states), 'families': len({r['family_id'] for r in rows}), 'grid': grid}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['prepare', 'check']); p.add_argument('--path', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(prepare(a.path) if a.command == 'prepare' else validate(a.path), indent=2))
