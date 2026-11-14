"""Select every original state whose measured nonempty actions disagree."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from data_pipeline.memory_decision_dataset import ROOT, readl, write, write_rows, digest, validate_dataset
from data_pipeline.memory_evidence_dataset import BASE, check as check_evidence
from data_pipeline.prepare_memory_contrast_learning import build_rows
from data_pipeline.prepare_memory_state_ablation import EVIDENCE, CLASSES

CONFIG = ROOT / 'training/memory_selector/configs/memory-fit-diagnostic-v1.json'


def reconstruct():
    actions = readl(BASE / 'supervision.jsonl')
    examples, targets = build_rows(actions)
    target = {r['example_id']: r for r in targets}
    grouped = defaultdict(list)
    for row in examples:
        grouped[row['state_id']].append(row)
    selected = {sid for sid, rows in grouped.items()
                if len({target[r['example_id']]['with'] for r in rows}) > 1}
    rows = sorted([r for r in examples if r['state_id'] in selected], key=lambda r: r['example_id'])
    return rows, [target[r['example_id']] for r in rows], [a for a in actions if a['state_id'] in selected]


def prepare(output):
    if output.exists():
        raise FileExistsError(output)
    validate_dataset(BASE); check_evidence(EVIDENCE)
    rows, targets, actions = reconstruct()
    output.mkdir(parents=True)
    write_rows(output / 'examples.jsonl', rows)
    write_rows(output / 'targets.jsonl', targets)
    write_rows(output / 'all-supervision.jsonl', actions)
    write(output / 'config.json', json.loads(CONFIG.read_text()))
    write(output / 'protocol.json', {
        'selection_rule': 'All original states with different observed successes among nonempty bundles; all their candidates retained.',
        'training_states': 7, 'training_examples': 22, 'source_families': 5,
        'four_way_counts': dict(Counter(CLASSES[t['class_id']] for t in targets)),
        'evaluation': 'The same training inputs; capacity/optimization check, not validation or test.',
        'primary_pass': 'At the fixed 128th update: 22/22 correct classes AND an observed optimal action in all 7 states, including no memory.',
        'secondary_checks': '16 informative pair rankings, effect signs, losses, gradient/update and exposure checks.',
        'checkpoint_selection': 'Final update fixed before fitting. Earlier pass times are descriptive only.',
        'state_weighting': 'Mean CE within each state; mean across the current batch (4 or 3 states). Same shuffled batches across loss arms.',
        'bound': 'Four fits total, 128 updates each, 3600 seconds for the full trainer. No follow-up hyperparameter search in this diagnostic.',
        'new_worker_calls': 0, 'new_effect_labels': 0,
        'independent_evaluation_examples': 0,
        'source_archive_sha256': digest((ROOT / 'evaluation/results/travel-policy-followup-20261109-v1/predictions.zip').read_bytes())})
    source = [CONFIG, Path(__file__), BASE / 'manifest.json', EVIDENCE / 'manifest.json',
              ROOT / 'training/memory_selector/configs/memory-contrast-loss-v1.json',
              ROOT / 'data_pipeline/prepare_memory_contrast_learning.py',
              ROOT / 'data_pipeline/memory_evidence_review.py']
    write(output / 'manifest.json', {
        'files_sha256': {p.name: digest(p.read_bytes()) for p in sorted(output.iterdir())},
        'source_sha256': {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in source}})
    return validate(output)


def validate(path):
    manifest = json.loads((path / 'manifest.json').read_text())
    for name, sha in manifest['files_sha256'].items():
        if digest((path / name).read_bytes()) != sha:
            raise ValueError('Changed diagnostic data: ' + name)
    for name, sha in manifest['source_sha256'].items():
        if digest((ROOT / name).read_bytes()) != sha:
            raise ValueError('Changed source: ' + name)
    if json.loads((path / 'config.json').read_text()) != json.loads(CONFIG.read_text()):
        raise ValueError('Diagnostic configuration differs from fixed protocol')
    rows, targets, actions = reconstruct()
    for name, expected in [('examples.jsonl', rows), ('targets.jsonl', targets), ('all-supervision.jsonl', actions)]:
        if readl(path / name) != expected:
            raise ValueError('Diagnostic source reconstruction differs: ' + name)
    labels_by_input = defaultdict(set)
    for row, target in zip(rows, targets):
        labels_by_input[row['texts']['goal_and_observations']].add(target['class_id'])
    if any(len(labels) > 1 for labels in labels_by_input.values()):
        raise ValueError('Identical input has conflicting labels')
    counts = dict(Counter(CLASSES[t['class_id']] for t in targets))
    if (len(rows), len({r['state_id'] for r in rows}), len(actions)) != (22, 7, 29):
        raise ValueError('Diagnostic population changed')
    if counts != {'both_failure': 5, 'gain': 6, 'harm': 5, 'both_success': 6}:
        raise ValueError('Diagnostic class balance changed')
    return {'rows': 22, 'states': 7, 'families': 5, 'four_way_counts': counts,
            'conflicting_inputs': 0, 'independent_evaluation_examples': 0}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['prepare', 'check'])
    p.add_argument('--path', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(prepare(a.path) if a.command == 'prepare' else validate(a.path), indent=2))
