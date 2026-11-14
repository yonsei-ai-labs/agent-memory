"""Package actual comparison evidence and verify it without allocating a GPU."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import tempfile
import zipfile

import numpy as np

from data_pipeline.memory_decision_dataset import ROOT, digest, write, readl
from data_pipeline.export_memory_state_ablation import deterministic_zip
from data_pipeline.prepare_memory_contrast_learning import validate
from data_pipeline.memory_evidence_dataset import BASE
from evaluation.memory_contrast_loss import head_probabilities
from evaluation.memory_state_ablation import policy_summary, classification
from training.memory_selector.run_memory_contrast_worker import sha_file

CORE = ('config.json', 'protocol.json', 'data-manifest.json', 'environment.json',
        'weight-provenance.json', 'run-status.json', 'text-index.jsonl')
MODEL = ('head.npz', 'held-vectors.npz', 'predictions.jsonl', 'train-predictions.jsonl',
         'fit.json', 'curve.json', 'adapter-changes.json', 'exposure.json', 'state-exposure.json')
SUMMARIES = ('policy-summary.json', 'classification.json', 'paired-comparisons.json',
             'selection-regret.json', 'by-condition-summary.json', 'fits.json', 'verification.json',
             'new-bundle-classification.json')


def export(root, output):
    if output.exists():
        raise FileExistsError(output)
    run_data = {'original-training': 'data-original'}
    if (root / 'augmented-training').exists():
        run_data['augmented-training'] = 'data-augmented'
    common = 'data-augmented' if len(run_data) == 2 else 'data-original'
    files, checkpoints = {}, {}
    for run, data in run_data.items():
        validate(root / data)
        state = json.loads((root / run / 'run-status.json').read_text())
        if state['status'] != 'complete':
            raise ValueError('Cannot describe an unfinished arm as complete')
        for name in CORE:
            files[run + '/' + name] = root / run / name
        for path in (root / data).iterdir():
            if path.is_file():
                files[data + '/' + path.name] = path
        for name in state['completed_models']:
            for leaf in MODEL:
                files[f'{run}/{name}/{leaf}'] = root / run / name / leaf
            folder = root / run / name
            for path in folder.rglob('*'):
                if path.is_file() and (path.name == 'optimizer.pt' or path.parent.name == 'adapter'):
                    checkpoints[str(path.relative_to(root))] = path
    for section in ('scored', 'evaluation', 'design'):
        for path in (root / section).rglob('*'):
            if path.is_file():
                files[str(path.relative_to(root))] = path
    if common == 'data-augmented':
        for section in ('observations', 'worker-local/artifacts'):
            for path in (root / section).rglob('*'):
                if path.is_file():
                    files[str(path.relative_to(root))] = path
        for name in ('run-status.json', 'input-snapshot.zip', 'input-manifest.json'):
            files['worker-local/' + name] = root / 'worker-local' / name
    # Failed calibration is research evidence, not an omitted experiment.
    for attempt in ('worker-local-initial-check', 'worker-local-calibration', 'worker-local'):
        folder = root / attempt
        if not folder.exists():
            continue
        for path in (folder / 'artifacts').rglob('*'):
            if path.is_file():
                files[str(path.relative_to(root))] = path
        for name in ('run-status.json', 'input-snapshot.zip', 'input-manifest.json'):
            files[attempt + '/' + name] = folder / name
    for attempt in ('worker', 'worker-a100', 'worker-retry', 'worker-colab', 'worker-colab-final'):
        path = root / attempt / 'run-status.json'
        if path.is_file():
            files[str(path.relative_to(root))] = path
    for name in ('pre-outcome-freeze.json', 'local-validation.log', 'resource-accounting.json',
                 'original-training.log', 'augmented-training.log', 'worker-local.log',
                 'new-input-preflight.json', 'planned-input-ambiguity.json', 'selector-tokenizer-preflight.json',
                 'worker-tokenizer-preflight.json', 'worker-environment-check.json',
                 'worker-reproduction-requirements.txt', 'continue_experiment.py', 'continuation-status.json',
                 'worker-initial-check-summary.json', 'original-training-diagnostic.json', 'original-case-changes.json',
                 'compatibility-amendment.json', 'compatibility-validation.log', 'contrast-validation-followup.log',
                 'audit_loss_gradients.py', 'original-loss-diagnostic.json', 'augmented-loss-diagnostic.json',
                 'loss-gradient-validation.json', 'worker-compatibility-failure.json', 'worker-failure-verification.json',
                 'classification-change-decomposition.json'):
        path = root / name
        if path.is_file():
            files[name] = path
    for pattern in ('data_pipeline/*memory*contrast*.py', 'training/memory_selector/*memory*contrast*.py',
                    'training/memory_selector/memory_selection_loss.py', 'training/memory_selector/configs/memory-contrast*.json',
                    'evaluation/*memory*contrast*.py', 'evaluation/memory_worker_compatibility.py',
                    'evaluation/audit_memory_worker_failure.py',
                    'evaluation/tests/test_memory_contrasts.py', 'evaluation/tests/test_memory_worker_compatibility.py',
                    'training/memory_selector/tests/test_memory_selection_loss.py'):
        for path in ROOT.glob(pattern):
            files['executed-source/' + str(path.relative_to(ROOT))] = path
    if any(not p.is_file() for p in files.values()):
        raise ValueError('Missing comparison evidence')
    verification = json.loads((root / 'evaluation/verification.json').read_text())
    if verification['models'] != (24 if common == 'data-augmented' else 12):
        raise ValueError('Unexpected model grid')
    output.mkdir(parents=True)
    archive = output / 'predictions.zip'
    deterministic_zip(archive, files)
    if archive.stat().st_size >= 95 * 1024**2:
        raise ValueError('Prediction archive too large for normal Git sharing')
    write(output / 'checkpoint-sha256.json', {n: sha_file(p) for n, p in sorted(checkpoints.items())})
    record = {'archive_sha256': sha_file(archive), 'files_sha256': {n: sha_file(p) for n, p in sorted(files.items())},
        'training_runs': run_data, 'common_data': common, 'full_checkpoints_in_shared_zip': False,
        'checkpoint_manifest_sha256': sha_file(output / 'checkpoint-sha256.json'),
        'original_worker_archive_sha256': sha_file(ROOT / 'evaluation/results/travel-policy-followup-20261109-v1/predictions.zip')}
    write(output / 'shared-manifest.json', record)
    for name in SUMMARIES:
        (output / name).write_bytes((root / 'evaluation' / name).read_bytes())
    full = {**files, **checkpoints, 'checkpoint-sha256.json': output / 'checkpoint-sha256.json'}
    deterministic_zip(root / 'full-review.zip', full)
    write(output / 'full-review-location.json', {'path': str((root / 'full-review.zip').resolve()),
        'sha256': sha_file(root / 'full-review.zip'), 'bytes': (root / 'full-review.zip').stat().st_size, 'in_git': False})
    return check(output)


def check(path):
    record = json.loads((path / 'shared-manifest.json').read_text())
    if sha_file(path / 'predictions.zip') != record['archive_sha256']:
        raise ValueError('Archive changed')
    if sha_file(path / 'checkpoint-sha256.json') != record['checkpoint_manifest_sha256']:
        raise ValueError('Checkpoint manifest changed')
    checkpoints = json.loads((path / 'checkpoint-sha256.json').read_text())
    if sha_file(ROOT / 'evaluation/results/travel-policy-followup-20261109-v1/predictions.zip') != record['original_worker_archive_sha256']:
        raise ValueError('Original worker source changed')
    n = models = 0
    max_error = 0.0
    with zipfile.ZipFile(path / 'predictions.zip') as z, tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        names = z.namelist()
        if len(names) != len(set(names)) or set(names) != set(record['files_sha256']):
            raise ValueError('Duplicate or incomplete members')
        for name, sha in record['files_sha256'].items():
            if Path(name).is_absolute() or '..' in Path(name).parts or digest(z.read(name)) != sha:
                raise ValueError('Unsafe or changed member: ' + name)
        z.extractall(root)
        data = root / record['common_data']
        validate(data)
        config = json.loads((data / 'config.json').read_text())
        folds = json.loads((data / 'folds.json').read_text())
        examples = {r['example_id']: r for r in readl(data / 'examples.jsonl')}
        replay = json.loads((root / 'scored/verification.json').read_text())
        if replay['status'] != 'complete':
            raise ValueError('Incomplete actual checkpoint replay')
        actual = {r['model']: r for r in replay['models']}
        expected_all = set()
        group_predictions, group_timings = defaultdict(dict), defaultdict(dict)
        for run, fitted_data in record['training_runs'].items():
            validate(root / fitted_data)
            state = json.loads((root / run / 'run-status.json').read_text())
            if (json.loads((root / run / 'config.json').read_text()) != config
                    or json.loads((root / run / 'data-manifest.json').read_text()) != json.loads((root / fitted_data / 'manifest.json').read_text())):
                raise ValueError('Fitted configuration or data provenance changed')
            expected = {f'lora--{a}--fold{f["fold"]}--seed{s}' for a in state['arms'] for f in folds for s in config['seeds']}
            if state['status'] != 'complete' or set(state['completed_models']) != expected or len(state['completed_models']) != len(expected):
                raise ValueError('Incomplete model grid')
            if expected & expected_all:
                raise ValueError('Duplicate fitted arm')
            expected_all |= expected
            for name in sorted(expected):
                folder = root / run / name
                fit = json.loads((folder / 'fit.json').read_text())
                ps = readl(root / 'scored' / name / 'predictions.jsonl')
                ids = {eid for eid, r in examples.items() if r['family_id'] in folds[fit['fold']]['held_families']}
                if len(ps) != len(ids) or {r['example_id'] for r in ps} != ids:
                    raise ValueError('Wrong common held identities')
                vectors = np.load(root / 'scored' / name / 'held-vectors.npz')['vectors']
                head = np.load(folder / 'head.npz')
                reproduced = head_probabilities(vectors, head)
                saved = np.array([p['probabilities'] for p in ps])
                if saved.shape != reproduced.shape or not np.isfinite(saved).all():
                    raise ValueError('Invalid saved probabilities')
                error = float(np.abs(reproduced - saved).max())
                if not np.isfinite(reproduced).all() or error > 2e-5 or not np.array_equal(reproduced.argmax(1), saved.argmax(1)):
                    raise ValueError('Independent head replay differs')
                max_error = max(max_error, error); n += len(ps); models += 1
                if sha_file(folder / 'head.npz') != actual[name]['head_sha256']:
                    raise ValueError('Head differs from GPU audit')
                for leaf, key in (('predictions.jsonl', 'prediction_sha256'), ('held-vectors.npz', 'vector_sha256')):
                    if sha_file(root / 'scored' / name / leaf) != actual[name][key]:
                        raise ValueError('Saved replay differs from the actual GPU audit')
                if sha_file(folder / 'fit.json') != actual[name]['fit_sha256']:
                    raise ValueError('Fitted model metadata differs from GPU audit')
                for leaf, sha in actual[name]['adapter_sha256'].items():
                    if checkpoints[f'{run}/{name}/adapter/{leaf}'] != sha:
                        raise ValueError('Adapter manifest differs from GPU audit')
                group = f'{fit["arm"]}--seed{fit["seed"]}'
                for row in ps:
                    if row['example_id'] in group_predictions[group]:
                        raise ValueError('Duplicate cross-fitted prediction')
                    group_predictions[group][row['example_id']] = row['probabilities']
                    group_timings[group][row['example_id']] = row
        if expected_all != set(actual):
            raise ValueError('Replayed model coverage differs')
        for name in SUMMARIES:
            if (path / name).read_bytes() != (root / 'evaluation' / name).read_bytes():
                raise ValueError('Public metric differs from archived evaluation')
        lookup = {(r['state_id'], tuple(r['ordered_selected_memory_ids'])): r for r in readl(data / 'all-supervision.jsonl')}
        origins = {r['state_id']: r for r in readl(BASE / 'provenance.jsonl')}
        original = set(json.loads((data / 'original-example-ids.json').read_text()))
        selected = readl(root / 'evaluation/selected-actions.jsonl')
        policy_groups = defaultdict(list)
        for row in selected:
            action = lookup[row['state_id'], tuple(row['ordered_selected_memory_ids'])]
            if row['success'] != action['outcomes']['task_success'] or row['generation_id'] != action['generation_id']:
                raise ValueError('Selected policy outcome differs from actual worker')
            v = action['outcomes']
            expected = {'family_id': origins[row['state_id']]['family_id'], 'condition': origins[row['state_id']]['condition'],
                'input_contract_success': v['input_contract_success'],
                'baseline_success': lookup[row['state_id'], ()]['outcomes']['task_success'],
                'provide': bool(action['ordered_selected_memory_ids']),
                'delivered_memory_tokens': action['delivered_memory_tokens'],
                'worker_input_tokens': v['cost']['worker_input_tokens'], 'worker_output_tokens': v['cost']['worker_output_tokens'],
                'unsupported_specific_action_components': v['counts']['unsupported_specific_action']['true'],
                'unsupported_infeasibility_components': v['counts']['unsupported_infeasibility']['true']}
            if any(row[k] != value for k, value in expected.items()):
                raise ValueError('Selected action costs or error components differ from source')
            pool = original if row['pool'] == 'original' else set(examples)
            candidates = [(eid, r) for eid, r in examples.items() if eid in pool and r['state_id'] == row['state_id']]
            if row['policy'] in group_predictions:
                probabilities = group_predictions[row['policy']]
                ranked = sorted(candidates, key=lambda pair: (-(probabilities[pair[0]][1] - probabilities[pair[0]][2]), pair[1]['delivered_memory_tokens'], pair[0]))
                eid, best = ranked[0]
                mids = best['ordered_selected_memory_ids'] if probabilities[eid][1] > probabilities[eid][2] else []
                if mids != row['ordered_selected_memory_ids']:
                    raise ValueError('Saved probabilities select a different memory bundle')
                times = [group_timings[row['policy']][eid] for eid, _ in candidates]
                if (sum(p['selector_tokens'] for p in times) != row['selector_tokens']
                        or abs(sum(p['selector_seconds'] for p in times) - row['selector_scoring_seconds']) > 1e-8):
                    raise ValueError('Selector scoring cost differs from the actual candidate pool')
            elif row['policy'] == 'shortest_bundle':
                expected_bundle = min(candidates, key=lambda pair: (pair[1]['delivered_memory_tokens'], pair[0]))[1]['ordered_selected_memory_ids']
                if expected_bundle != row['ordered_selected_memory_ids']:
                    raise ValueError('Shortest-bundle policy changed')
            elif row['policy'] not in action['policy_aliases']:
                raise ValueError('Original baseline policy changed')
            policy_groups[row['pool'] + '/' + row['policy']].append(row)
        expected_policies = set(group_predictions) | {'none', 'fixed4', 'top2', 'top4', 'threshold', 'rule2', 'state_rule', 'shortest_bundle'}
        if set(policy_groups) != {pool + '/' + policy for pool in ('original', 'expanded') for policy in expected_policies}:
            raise ValueError('Policy comparison coverage changed')
        states = {r['state_id'] for r in examples.values()}
        if any(len(rs) != len(states) or {r['state_id'] for r in rs} != states for rs in policy_groups.values()):
            raise ValueError('Missing or duplicate selected state')
        if {k: policy_summary(v) for k, v in policy_groups.items()} != json.loads((path / 'policy-summary.json').read_text()):
            raise ValueError('Policy summary does not reproduce')
        targets = {r['example_id']: r for r in readl(data / 'targets.jsonl')}
        metrics = {}
        for pool_name, pool in (('original', original), ('expanded', set(examples))):
            for group, ps in group_predictions.items():
                if set(ps) != set(examples):
                    raise ValueError('Missing cross-fitted examples')
                metrics[pool_name + '/' + group] = classification({eid: ps[eid] for eid in pool}, targets, examples)
        # Float sums may follow another set traversal order after extraction.
        def close(a, b):
            if isinstance(a, dict):
                return isinstance(b, dict) and a.keys() == b.keys() and all(close(v, b[k]) for k, v in a.items())
            if isinstance(a, list):
                return isinstance(b, list) and len(a) == len(b) and all(close(x, y) for x, y in zip(a, b))
            if isinstance(a, float):
                return isinstance(b, (int, float)) and abs(a - b) < 1e-10
            return a == b
        if not close(metrics, json.loads((path / 'classification.json').read_text())):
            raise ValueError('Classification summaries do not reproduce')
        new_ids = set(examples) - original
        new_metrics = {group: classification({eid: ps[eid] for eid in new_ids}, targets, examples)
                       for group, ps in group_predictions.items()}
        if not close(new_metrics, json.loads((path / 'new-bundle-classification.json').read_text())):
            raise ValueError('New-only classification summaries do not reproduce')
        failed_worker_audit = None
        if (root / 'worker-compatibility-failure.json').exists():
            from evaluation.audit_memory_worker_failure import audit_failure
            failed_worker_audit = audit_failure(root / 'worker-local')
            if failed_worker_audit != json.loads((root / 'worker-failure-verification.json').read_text()):
                raise ValueError('Rejected worker audit does not reproduce')
        worker_audit = None
        if record['common_data'] == 'data-augmented':
            from evaluation.memory_contrast_outcomes import evaluate
            worker_audit = evaluate(root / 'design', root / 'worker-local', root / 'worker-recheck')
            for name in ('summary.json', 'decisions.jsonl', 'new-supervision.jsonl', 'verification.json', 'contrast-groups.json'):
                if (root / 'worker-recheck' / name).read_bytes() != (root / 'observations' / name).read_bytes():
                    raise ValueError('Actual worker judgments do not reproduce')
    return {'models': models, 'independent_head_predictions_reproduced': n, 'maximum_probability_error': max_error,
        'worker_outcomes_rechecked': worker_audit, 'rejected_worker_rechecked': failed_worker_audit, 'gpu_allocated': False,
        'scope': 'Saved vectors/heads, source inputs, and actual worker outcome reconstruction. Actual adapter re-encoding needs the separate full-review archive.'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('export', 'check'))
    p.add_argument('--path', type=Path, required=True)
    p.add_argument('--run-root', type=Path)
    a = p.parse_args()
    print(json.dumps(export(a.run_root, a.path) if a.command == 'export' else check(a.path), indent=2))
