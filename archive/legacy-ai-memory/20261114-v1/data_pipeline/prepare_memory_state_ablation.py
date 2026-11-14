"""Prepare a bounded, exploratory input ablation from actual observed effects."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from data_pipeline.memory_decision_dataset import ROOT, readl, write, write_rows, digest, validate_dataset
from data_pipeline.memory_evidence_dataset import BASE, check as check_evidence
from data_pipeline.memory_evidence_review import render_selector

EVIDENCE = ROOT / 'datasets/memory_decisions/20261109-evidence-v1'
CONFIG = ROOT / 'training/memory_selector/configs/memory-state-ablation-v1.json'
CLASSES = ('both_failure', 'gain', 'harm', 'both_success')
INSTRUCTION = 'Instruct: Estimate how supplying the proposed past records changes the current task outcome.\n'


def identity(sid, ordered_ids):
    return digest(json.dumps([sid, ordered_ids], ensure_ascii=False).encode())


def outcome_class(without, with_memory):
    if without not in (0, 1) or with_memory not in (0, 1):
        raise ValueError('Binary observed task outcomes required')
    return {(0, 0): 0, (0, 1): 1, (1, 0): 2, (1, 1): 3}[without, with_memory]


def family_folds(families, count):
    ordered = sorted(set(families), key=lambda f: digest(('memory-state-ablation-v1:' + f).encode()))
    if len(ordered) < 2 * count:
        raise ValueError('Too few source families')
    return [{'fold': i, 'train_families': [f for j, f in enumerate(ordered) if j % count != i],
             'held_families': [f for j, f in enumerate(ordered) if j % count == i]} for i in range(count)]


def state_weights(rows):
    counts = Counter(r['state_id'] for r in rows)
    return [len(rows) / (len(counts) * counts[r['state_id']]) for r in rows]


def prepare(output):
    if output.exists():
        raise FileExistsError(output)
    validate_dataset(BASE)
    check_evidence(EVIDENCE)
    config = json.loads(CONFIG.read_text())
    states = {r['state_id']: r for r in readl(EVIDENCE / 'selector-inputs.jsonl')}
    memories = {r['memory_id']: r for r in readl(BASE / 'memories.jsonl')}
    actions = readl(BASE / 'supervision.jsonl')
    baselines = {r['state_id']: r for r in actions if not r['ordered_selected_memory_ids']}
    examples, targets = [], []
    for action in actions:
        mids = action['ordered_selected_memory_ids']
        if not mids:
            continue  # no-memory effect is exactly zero; it is always an available action
        sid = action['state_id']
        state = states[sid]
        eid = identity(sid, mids)
        examples.append({'example_id': eid, 'state_id': sid, 'family_id': state['family_id'],
            'ordered_selected_memory_ids': mids, 'memory_count': len(mids),
            'delivered_memory_tokens': action['delivered_memory_tokens'],
            'texts': {view: INSTRUCTION + render_selector(state, memories, view, mids) for view in config['views']}})
        without = baselines[sid]['outcomes']['task_success']
        with_memory = action['outcomes']['task_success']
        targets.append({'example_id': eid, 'class_id': outcome_class(without, with_memory),
                        'without': without, 'with': with_memory,
                        'generation_id': action['generation_id'],
                        'baseline_generation_id': action['paired_baseline_generation_id']})
    folds = family_folds([r['family_id'] for r in examples], config['folds'])
    protocol = {'experiment': config['experiment'], 'classes': list(CLASSES),
        'scope': 'Exploratory three-fold cross-fitting of existing 12 training families; no fresh test.',
        'action_space': 'Only the observed ordered bundles per state, plus no memory. Not arbitrary retrieval.',
        'upstream_candidates': 'Existing policy proposals were made before this selector experiment. The prior retriever is not re-trained within these folds; no claim of end-to-end unseen-family retrieval.',
        'input_change': 'Add current observations to the selector only. Same proposed bundles, labels, training order and worker inputs.',
        'worker': json.loads((BASE / 'contract.json').read_text())['worker'],
        'memory_token_cap': 512, 'none_score': 0.0,
        'score': 'p(gain)-p(harm); provide only above fixed zero threshold',
        'semantic_supervision_used': False, 'new_worker_calls': 0,
        'class_weights': 'none; reported probabilities are model scores, not validated causal probabilities',
        'sampling': 'Every nonempty action once per epoch, inverse action-count weighting for equal state mass',
        'fold_generation': 'Hash-sort family IDs, round-robin into three folds; no outcome stratification',
        'training_selection': 'Fixed eight epochs; no held-out checkpoint/hyperparameter selection',
        'primary': 'Within-seed paired goal vs goal_and_observations LoRA results',
        'controls': ['same-head frozen encoder', 'count/length-only four-way linear model', 'training class prior'],
        'limits': ['Small artificial condition mixture and only five states with observed harm.',
                   'Existing designated-record count shortcut remains; numeric baseline uses only rendered-input lengths and proposed-bundle count.',
                   'No actual sequential progress/failure histories.',
                   'Worker timing was measured on different hardware; selector and worker time reported separately.',
                   'No-memory result is reused, not an independent execution per proposal.',
                   'Two seeds are two repeated fits of the same outcomes, not independent task samples.']}
    output.mkdir(parents=True)
    write_rows(output / 'examples.jsonl', examples)
    write_rows(output / 'targets.jsonl', targets)
    write(output / 'folds.json', folds)
    write(output / 'protocol.json', protocol)
    write(output / 'config.json', config)
    sources = [BASE / 'manifest.json', EVIDENCE / 'manifest.json', CONFIG, Path(__file__)]
    write(output / 'manifest.json', {
        'source_sha256': {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in sources},
        'files_sha256': {p.name: digest(p.read_bytes()) for p in sorted(output.iterdir())}})
    return validate(output)


def validate(path):
    record = json.loads((path / 'manifest.json').read_text())
    for name, sha in record['files_sha256'].items():
        if digest((path / name).read_bytes()) != sha:
            raise ValueError('Prepared input changed: ' + name)
    for name, sha in record['source_sha256'].items():
        if digest((ROOT / name).read_bytes()) != sha:
            raise ValueError('Source changed: ' + name)
    rows, targets = readl(path / 'examples.jsonl'), readl(path / 'targets.jsonl')
    ids = {r['example_id'] for r in rows}
    if len(ids) != len(rows) or ids != {r['example_id'] for r in targets} or len(targets) != len(rows):
        raise ValueError('Incomplete or duplicate action identities')
    for r in rows:
        if set(r) != {'example_id', 'state_id', 'family_id', 'ordered_selected_memory_ids', 'memory_count', 'delivered_memory_tokens', 'texts'}:
            raise ValueError('Unapproved training input field')
        if r['example_id'] != identity(r['state_id'], r['ordered_selected_memory_ids']):
            raise ValueError('Action identity changed')
        if not r['ordered_selected_memory_ids'] or not 0 <= r['delivered_memory_tokens'] <= 512:
            raise ValueError('Invalid observed bundle')
    for r in targets:
        if r['class_id'] != outcome_class(r['without'], r['with']):
            raise ValueError('Four-way target disagrees with paired outcome')
    folds = json.loads((path / 'folds.json').read_text())
    families = {r['family_id'] for r in rows}
    held = []
    for fold in folds:
        train, test = set(fold['train_families']), set(fold['held_families'])
        if train & test or train | test != families:
            raise ValueError('Family fold leakage or missing family')
        held.extend(test)
    if len(held) != len(families) or set(held) != families:
        raise ValueError('Every family must be held out exactly once')
    # A shared worker input, baseline or current state must not cross families.
    owners = defaultdict(set)
    target_map = {r['example_id']: r for r in targets}
    for r in rows:
        t = target_map[r['example_id']]
        for key in (r['state_id'], t['generation_id'], t['baseline_generation_id']):
            owners[key].add(r['family_id'])
    if any(len(v) > 1 for v in owners.values()):
        raise ValueError('Shared trajectory crosses folds')
    return {'examples': len(rows), 'states': len({r['state_id'] for r in rows}), 'families': len(families),
            'four_way_counts': dict(Counter(CLASSES[r['class_id']] for r in targets)),
            'folds': len(folds), 'new_model_calls': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'check'])
    parser.add_argument('--path', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps((prepare if args.command == 'prepare' else validate)(args.path), indent=2))
