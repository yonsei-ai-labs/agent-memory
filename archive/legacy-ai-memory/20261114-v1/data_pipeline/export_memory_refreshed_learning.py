"""Share the four-arm comparison and reconstruct it from saved inputs/vectors."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

from data_pipeline.memory_decision_dataset import ROOT, readl, write, digest
from data_pipeline.export_memory_state_ablation import deterministic_zip
from data_pipeline.export_memory_worker_refresh import check as check_worker
from evaluation.audit_memory_fit_diagnostic import metric_equal
from evaluation.memory_refreshed_learning import audit_saved, summarize, load
from evaluation.memory_state_ablation import choose_bundle
from training.memory_selector.run_memory_contrast_worker import sha_file

CORE = ('config.json', 'protocol.json', 'data-manifest.json', 'environment.json',
        'weight-provenance.json', 'run-status.json', 'text-index.jsonl', 'source-snapshot.zip')
MODEL = ('head.npz', 'held-vectors.npz', 'train-vectors.npz', 'predictions.jsonl', 'train-predictions.jsonl',
         'fit.json', 'curve.json', 'adapter-changes.json', 'exposure.json', 'state-exposure.json')


def export(root, output):
    if output.exists():
        raise FileExistsError(output)
    saved = audit_saved(root / 'data', root / 'training')
    if load(root / 'replay/verification.json')['status'] != 'complete':
        raise ValueError('Checkpoint replay not complete')
    files, checkpoints = {}, {}
    for name in CORE:
        files['training/' + name] = root / 'training' / name
    for model in saved['fits']:
        name = model['name']; folder = root / 'training' / name
        for leaf in MODEL:
            files[f'training/{name}/{leaf}'] = folder / leaf
        for path in [folder / 'optimizer.pt', *sorted((folder / 'adapter').iterdir())]:
            if path.is_file():
                checkpoints[str(path.relative_to(root))] = path
    for section in ('data', 'evaluation', 'replay', 'training/tokenizer'):
        for path in (root / section).rglob('*'):
            if path.is_file():
                files[str(path.relative_to(root))] = path
    for name in ('pre-fit-report.md', 'preflight.json', 'data-tests.log', 'loss-tests.log',
                 'training.log', 'checkpoint-replay.log', 'evaluation.log', 'resource-accounting.json',
                 'complete_and_audit.py', 'continuation-status.json', 'early-saved-artifact-check.json',
                 'evaluation-preparation-tests.log'):
        files['execution/' + name] = root / name
    for path in (ROOT / 'docs/reports/data/memory-refreshed-learning-20261110').glob('*.json'):
        files['protocol-evidence/' + path.name] = path
    for name in ('data_pipeline/prepare_memory_refreshed_learning.py', 'data_pipeline/export_memory_refreshed_learning.py',
                 'training/memory_selector/train_memory_refreshed_learning.py',
                 'evaluation/memory_refreshed_learning.py', 'evaluation/audit_memory_refreshed_models.py',
                 'evaluation/tests/test_memory_refreshed_learning.py'):
        files['verification-source/' + name] = ROOT / name
    if any(not path.is_file() for path in files.values()):
        raise ValueError('Missing actual run/audit evidence')
    output.mkdir(parents=True)
    deterministic_zip(output / 'predictions.zip', files)
    if (output / 'predictions.zip').stat().st_size >= 95 * 1024**2:
        raise ValueError('Shared archive exceeds normal Git limit')
    write(output / 'checkpoint-sha256.json', {name: sha_file(path) for name, path in sorted(checkpoints.items())})
    metric_names = sorted(p.name for p in (root / 'evaluation').iterdir() if p.suffix == '.json' and p.name != 'manifest.json')
    for name in metric_names:
        shutil.copy2(root / 'evaluation' / name, output / name)
    write(output / 'shared-manifest.json', {'archive_sha256': sha_file(output / 'predictions.zip'),
        'files_sha256': {name: sha_file(path) for name, path in sorted(files.items())},
        'metric_files': metric_names, 'models': 24,
        'checkpoint_manifest_sha256': sha_file(output / 'checkpoint-sha256.json'),
        'worker_shared_archive_sha256': sha_file(ROOT / 'evaluation/results/memory-worker-refresh-20261110-v1/predictions.zip'),
        'actual_adapters_and_optimizers_in_shared_zip': False})
    full = {**files, **checkpoints, 'checkpoint-sha256.json': output / 'checkpoint-sha256.json'}
    deterministic_zip(root / 'full-review.zip', full)
    write(output / 'full-review-location.json', {'path': str((root / 'full-review.zip').resolve()),
        'sha256': sha_file(root / 'full-review.zip'), 'bytes': (root / 'full-review.zip').stat().st_size, 'in_git': False})
    return check(output)


def check(path):
    manifest = load(path / 'shared-manifest.json')
    if sha_file(path / 'predictions.zip') != manifest['archive_sha256'] or sha_file(path / 'checkpoint-sha256.json') != manifest['checkpoint_manifest_sha256']:
        raise ValueError('Shared evidence archive/manifest changed')
    worker = ROOT / 'evaluation/results/memory-worker-refresh-20261110-v1'
    if sha_file(worker / 'predictions.zip') != manifest['worker_shared_archive_sha256']:
        raise ValueError('Worker measurement evidence changed')
    check_worker(worker)
    checkpoints = load(path / 'checkpoint-sha256.json')
    with zipfile.ZipFile(path / 'predictions.zip') as z, tempfile.TemporaryDirectory() as tmp:
        if len(z.namelist()) != len(set(z.namelist())) or set(z.namelist()) != set(manifest['files_sha256']):
            raise ValueError('Incomplete shared learning archive')
        for name, sha in manifest['files_sha256'].items():
            if Path(name).is_absolute() or '..' in Path(name).parts or digest(z.read(name)) != sha:
                raise ValueError('Unsafe or changed shared member')
        root = Path(tmp); z.extractall(root)
        freeze = load(root / 'protocol-evidence/pre-fit-freeze.json')
        if sha_file(root / 'data/manifest.json') != freeze['data_manifest_sha256']:
            raise ValueError('Data differs from pre-fit freeze')
        plan_name = 'docs/reports/2026-11-10-memory-refreshed-learning.md'
        if sha_file(root / 'execution/pre-fit-report.md') != freeze['files_sha256'][plan_name]:
            raise ValueError('Pre-fit plan snapshot differs')
        for name, sha in freeze['files_sha256'].items():
            if name != plan_name and sha_file(ROOT / name) != sha:
                raise ValueError('Frozen learning configuration/source changed')
        saved = audit_saved(root / 'data', root / 'training')
        examples = {r['example_id']: r for r in readl(root / 'data/examples.jsonl')}
        originals = set(load(root / 'data/original-example-ids.json'))
        metrics = summarize(root / 'data', saved)
        for name, expected in metrics.items():
            actual = readl(root / 'evaluation' / name) if name.endswith('.jsonl') else load(root / 'evaluation' / name)
            if not metric_equal(actual, expected):
                raise ValueError('Metrics differ from reconstructed saved predictions: ' + name)
        replay = load(root / 'replay/verification.json')
        model_names = {f['name'] for f in saved['fits']}
        if replay['status'] != 'complete' or len(replay['models']) != 24 or {m['model'] for m in replay['models']} != model_names:
            raise ValueError('Actual checkpoint audit coverage differs')
        if load(root / 'replay/data-manifest.json') != load(root / 'data/manifest.json'):
            raise ValueError('Checkpoint audit used another dataset')
        for item in replay['models']:
            name = item['model']; folder = root / 'training' / name
            for leaf, sha in item['checkpoint_sha256'].items():
                relative = f'training/{name}/{leaf}'
                if relative in manifest['files_sha256']:
                    if sha_file(folder / leaf) != sha:
                        raise ValueError('Saved model differs from actual checkpoint audit')
                elif checkpoints.get(relative) != sha:
                    raise ValueError('Actual adapter/optimizer manifest differs')
            ps = readl(root / 'replay' / name / 'predictions.jsonl')
            old = readl(folder / 'predictions.jsonl')
            if [p['example_id'] for p in ps] != [p['example_id'] for p in old]:
                raise ValueError('Actual replay identities differ')
            if sha_file(root / 'replay' / name / 'predictions.jsonl') != item['replayed_predictions_sha256'] or sha_file(root / 'replay' / name / 'held-vectors.npz') != item['replayed_vectors_sha256']:
                raise ValueError('Actual checkpoint output changed')
            import numpy as np
            saved_p = np.array([p['probabilities'] for p in old]); new_p = np.array([p['probabilities'] for p in ps])
            error = float(np.abs(saved_p - new_p).max())
            if error > 2e-5 or not np.array_equal(saved_p.argmax(1), new_p.argmax(1)):
                raise ValueError('Actual checkpoint predictions differ')
            old_vectors = np.load(folder / 'held-vectors.npz')['vectors']
            new_vectors = np.load(root / 'replay' / name / 'held-vectors.npz')['vectors']
            vector_error = float(np.abs(old_vectors - new_vectors).max())
            if vector_error > 2e-4 or not metric_equal(error, item['max_probability_error']) or not metric_equal(vector_error, item['max_vector_error']):
                raise ValueError('Actual checkpoint numerical error report differs')
            old_probs = {p['example_id']: p['probabilities'] for p in old}
            new_probs = {p['example_id']: p['probabilities'] for p in ps}
            checked = 0
            for pool in ('original', 'expanded'):
                for sid in sorted({examples[eid]['state_id'] for eid in old_probs}):
                    candidates = [examples[eid] for eid in old_probs if examples[eid]['state_id'] == sid
                                  and (pool == 'expanded' or eid in originals)]
                    if choose_bundle(candidates, old_probs) != choose_bundle(candidates, new_probs):
                        raise ValueError('Actual checkpoint changes a memory policy choice')
                    checked += 1
            if checked != item['original_and_expanded_choices_verified'] or len(ps) != item['classes_identical']:
                raise ValueError('Actual replay choice/class counts differ')
        for name in manifest['metric_files']:
            if (path / name).read_bytes() != (root / 'evaluation' / name).read_bytes():
                raise ValueError('Public metric differs from archived evaluation')
        verification = load(path / 'verification.json')
        if (verification['models'] != 24 or verification['held_checkpoint_predictions'] != 3264
                or verification['saved_head_predictions'] != sum(r['rows'] for r in saved['head_checks'])
                or not metric_equal(verification['head_checks'], saved['head_checks'])):
            raise ValueError('Published verification counts differ')
    return {'models': 24, 'cross_fitted_held_predictions': 3264,
        'independent_train_and_held_head_predictions': sum(r['rows'] for r in saved['head_checks']),
        'worker_state_action_labels_reconstructed': 504, 'training_input_token_sequences_verified': 408,
        'new_worker_calls': 0, 'gpu_used': False,
        'scope': 'CPU saved-vector/head and worker-output reconstruction. Actual adapter replay record included; replaying weights needs full-review.zip.'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('export', 'check'))
    p.add_argument('--path', type=Path, required=True)
    p.add_argument('--root', type=Path)
    a = p.parse_args()
    if a.command == 'export' and a.root is None:
        p.error('--root is required for export')
    print(json.dumps(export(a.root, a.path) if a.command == 'export' else check(a.path), indent=2))
