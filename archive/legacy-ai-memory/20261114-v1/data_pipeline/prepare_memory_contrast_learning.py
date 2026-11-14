"""Prepare matched family folds for original/contrast data and CE/selection loss."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from data_pipeline.memory_decision_dataset import ROOT, readl, write, write_rows, digest, validate_dataset
from data_pipeline.memory_evidence_dataset import BASE, check as check_evidence
from data_pipeline.memory_evidence_review import render_selector
from data_pipeline.prepare_memory_state_ablation import EVIDENCE, INSTRUCTION, CLASSES, identity, outcome_class, family_folds

CONFIG = ROOT / 'training/memory_selector/configs/memory-contrast-loss-v1.json'


def build_rows(actions):
    states = {r['state_id']: r for r in readl(EVIDENCE / 'selector-inputs.jsonl')}
    memories = {r['memory_id']: r for r in readl(BASE / 'memories.jsonl')}
    base = {r['state_id']: r for r in actions if not r['ordered_selected_memory_ids']}
    examples, targets = [], []
    for action in actions:
        mids, sid = action['ordered_selected_memory_ids'], action['state_id']
        if not mids:
            continue
        eid = identity(sid, mids)
        examples.append({'example_id': eid, 'state_id': sid, 'family_id': states[sid]['family_id'],
            'ordered_selected_memory_ids': mids, 'memory_count': len(mids),
            'delivered_memory_tokens': action['delivered_memory_tokens'],
            'texts': {view: INSTRUCTION + render_selector(states[sid], memories, view, mids)
                      for view in ('goal', 'goal_and_observations')}})
        without, with_memory = base[sid]['outcomes']['task_success'], action['outcomes']['task_success']
        targets.append({'example_id': eid, 'class_id': outcome_class(without, with_memory),
            'without': without, 'with': with_memory, 'generation_id': action['generation_id'],
            'baseline_generation_id': base[sid]['generation_id']})
    return examples, targets


def prepare(output, observations=None):
    if output.exists():
        raise FileExistsError(output)
    validate_dataset(BASE)
    check_evidence(EVIDENCE)
    config = json.loads(CONFIG.read_text())
    old = readl(BASE / 'supervision.jsonl')
    new, source_manifest = [], None
    if observations is not None:
        source_manifest = json.loads((observations / 'manifest.json').read_text())
        for name, sha in source_manifest['files_sha256'].items():
            if digest((observations / name).read_bytes()) != sha:
                raise ValueError('Changed validated worker outcomes')
        verification = json.loads((observations / 'verification.json').read_text())
        if not verification['source_reconstruction'] or not all(r['same_input_tokens'] and r['same_output_tokens'] for r in verification['repeat_audit']):
            raise ValueError('Worker/source verification incomplete')
        new = readl(observations / 'new-supervision.jsonl')
    actions = old + new
    examples, targets = build_rows(actions)
    original_ids = [identity(r['state_id'], r['ordered_selected_memory_ids']) for r in old if r['ordered_selected_memory_ids']]
    output.mkdir(parents=True)
    write_rows(output / 'examples.jsonl', examples)
    write_rows(output / 'targets.jsonl', targets)
    write_rows(output / 'all-supervision.jsonl', actions)
    write(output / 'original-example-ids.json', original_ids)
    write(output / 'folds.json', family_folds([r['family_id'] for r in examples], config['folds']))
    write(output / 'config.json', config)
    write(output / 'source-observation-manifest.json', source_manifest)
    write(output / 'protocol.json', {'experiment': config['experiment'], 'classes': list(CLASSES),
        'scope': 'Exploratory within the same 12 prior families. New observed bundles do not create independent families.',
        'available_arms': config['arms'] if new else [a for a in config['arms'] if a.startswith('original')],
        'original_rows': len(original_ids), 'new_rows': len(new),
        'training': 'All states once per epoch; mean CE per state; four states/update. Same shuffled state order and initialization across arms.',
        'candidate_groups': 'Real observed actions only. No-memory score and utility fixed to zero.',
        'selection_loss': 'Expected observed regret under softmax((p_gain-p_harm)/0.5), including no memory; weight 1 versus 0.',
        'augmentation': 'Additional measured effects of unchanged near-reference records; no relation-to-utility label conversion.',
        'evaluation': 'Same held-out family pools across arms, original candidates and expanded candidates reported separately.',
        'data_budget': 'State exposures and update count matched, not encoder tokens or raw candidate exposures.',
        'checkpoint_selection': 'Fixed final epoch; no held-out or post-outcome hyperparameter selection.',
        'new_semantic_supervision': False, 'new_independent_families': 0,
        'worker_contract': json.loads((BASE / 'contract.json').read_text())['worker']})
    write(output / 'manifest.json', {'files_sha256': {p.name: digest(p.read_bytes()) for p in sorted(output.iterdir())},
        'source_sha256': {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in (CONFIG, BASE / 'manifest.json', EVIDENCE / 'manifest.json', Path(__file__))},
        'observation_manifest_sha256': digest((observations / 'manifest.json').read_bytes()) if observations else None})
    return validate(output)


def validate(path):
    manifest = json.loads((path / 'manifest.json').read_text())
    for name, sha in manifest['files_sha256'].items():
        if digest((path / name).read_bytes()) != sha:
            raise ValueError('Changed prepared data: ' + name)
    for name, sha in manifest['source_sha256'].items():
        if digest((ROOT / name).read_bytes()) != sha:
            raise ValueError('Changed source: ' + name)
    actions = readl(path / 'all-supervision.jsonl')
    lookup = {(r['state_id'], tuple(r['ordered_selected_memory_ids'])): r for r in actions}
    if len(lookup) != len(actions):
        raise ValueError('Duplicate state/action')
    old = readl(BASE / 'supervision.jsonl')
    if any(lookup[r['state_id'], tuple(r['ordered_selected_memory_ids'])] != r for r in old):
        raise ValueError('Original measured outcomes changed')
    rows, targets = readl(path / 'examples.jsonl'), readl(path / 'targets.jsonl')
    expected_rows, expected_targets = build_rows(actions)
    if rows != expected_rows or targets != expected_targets:
        raise ValueError('Inputs/targets differ from source reconstruction')
    ids = [r['example_id'] for r in rows]
    if len(set(ids)) != len(ids) or len(rows) != len(targets):
        raise ValueError('Duplicate/incomplete example identity')
    original_ids = json.loads((path / 'original-example-ids.json').read_text())
    expected_original = [identity(r['state_id'], r['ordered_selected_memory_ids']) for r in old if r['ordered_selected_memory_ids']]
    if original_ids != expected_original:
        raise ValueError('Original/augmented membership changed')
    config = json.loads((path / 'config.json').read_text())
    folds = json.loads((path / 'folds.json').read_text())
    if folds != family_folds([r['family_id'] for r in rows], config['folds']):
        raise ValueError('Family split changed')
    owners = defaultdict(set)
    for r, t in zip(rows, targets):
        if not 0 < r['delivered_memory_tokens'] <= 512:
            raise ValueError('Unbudgeted candidate')
        for key in (r['state_id'], t['generation_id'], t['baseline_generation_id']):
            owners[key].add(r['family_id'])
    if any(len(v) > 1 for v in owners.values()):
        raise ValueError('Shared trajectory crosses folds')
    return {'rows': len(rows), 'original_rows': len(original_ids), 'new_rows': len(rows) - len(original_ids),
        'states': len({r['state_id'] for r in rows}), 'families': len({r['family_id'] for r in rows}),
        'four_way_counts': dict(Counter(CLASSES[r['class_id']] for r in targets)),
        'available_arms': json.loads((path / 'protocol.json').read_text())['available_arms']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'check'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--observations', type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output, args.observations) if args.command == 'prepare' else validate(args.output), indent=2))
