"""Share and independently verify the bounded selector fitting diagnostic."""
import argparse
import json
from pathlib import Path
import tempfile
import zipfile

import numpy as np

from data_pipeline.memory_decision_dataset import ROOT, readl, write, digest
from data_pipeline.export_memory_state_ablation import deterministic_zip
from evaluation.audit_memory_fit_diagnostic import audit_saved, read, metric_equal
from evaluation.memory_fit_diagnostic import metrics
from evaluation.memory_contrast_loss import head_probabilities
from training.memory_selector.run_memory_contrast_worker import sha_file


def verify_replay(root, checkpoints, summary):
    report = read(root / 'replay/verification.json')
    if report['status'] != 'complete' or report['auditor_sha256'] != sha_file(ROOT / 'evaluation/audit_memory_fit_diagnostic.py'):
        raise ValueError('Actual checkpoint audit incomplete or code changed')
    if report['base_provenance'] != read(root / 'training/weight-provenance.json'):
        raise ValueError('Replay used different base weights')
    actual = {r['name']: r for r in report['models']}
    expected = {r['name'] for r in summary['models']}
    if set(actual) != expected or len(actual) != len(report['models']):
        raise ValueError('Incomplete actual checkpoint coverage')
    rows, targets, actions = [readl(root / 'data' / n) for n in ('examples.jsonl', 'targets.jsonl', 'all-supervision.jsonl')]
    for item in summary['models']:
        name = item['name']; verified = actual[name]
        source, replay = root / 'training' / name, root / 'replay' / name
        for file, key in [('fit.json', 'fit_sha256'), ('head.npz', 'head_sha256')]:
            if sha_file(source / file) != verified[key]:
                raise ValueError('Actual checkpoint input changed')
        if checkpoints[f'training/{name}/optimizer.pt'] != verified['optimizer_sha256']:
            raise ValueError('Optimizer provenance changed')
        for file, sha in verified['adapter_sha256'].items():
            if checkpoints[f'training/{name}/adapter/{file}'] != sha:
                raise ValueError('Adapter provenance changed')
        for file, sha in verified['replay_sha256'].items():
            if sha_file(replay / file) != sha:
                raise ValueError('Actual checkpoint output changed')
        ps = readl(replay / 'predictions.jsonl')
        vectors = np.load(replay / 'vectors.npz')['vectors']
        probabilities = np.array([p['probabilities'] for p in ps])
        if [p['example_id'] for p in ps] != [r['example_id'] for r in rows]:
            raise ValueError('Replay identity order changed')
        recalculated = head_probabilities(vectors, np.load(source / 'head.npz'))
        if not np.isfinite(recalculated).all() or np.max(np.abs(recalculated - probabilities)) > 2e-5:
            raise ValueError('Replay head probabilities changed')
        result = metrics(rows, targets, actions, ps)
        if not metric_equal(result, read(replay / 'metrics.json')) or result['choices'] != item['metrics']['choices']:
            raise ValueError('Replay metrics/choices differ')
        old_ps = np.array([p['probabilities'] for p in readl(source / 'predictions.jsonl')])
        ve = float(np.max(np.abs(vectors - np.load(source / 'vectors.npz')['vectors'])))
        pe = float(np.max(np.abs(probabilities - old_ps)))
        if ve != verified['max_vector_error'] or pe != verified['max_probability_error'] or ve > 2e-4 or pe > 2e-5:
            raise ValueError('Replay discrepancy differs')
        if (verified['predictions'] != 22 or verified['identical_choices'] != 7 or
                verified['optimizer_steps_verified'] != 128 or verified['actual_adapter_deltas_verified'] != 112 or
                not verified['initial_adapter_reconstructed'] or not verified['initial_head_reconstructed']):
            raise ValueError('Checkpoint audit coverage incomplete')


def export(root, output):
    if output.exists():
        raise FileExistsError(output)
    summary = audit_saved(root / 'data', root / 'training')
    files, checkpoint_files = {}, {}
    for section in ('data', 'training', 'replay'):
        for path in (root / section).rglob('*'):
            if not path.is_file():
                continue
            name = str(path.relative_to(root))
            if path.name == 'optimizer.pt' or path.parent.name == 'adapter':
                checkpoint_files[name] = path
            else:
                files[name] = path
    for name in ('training.log', 'replay.log'):
        files[name] = root / name
    checkpoints = {n: sha_file(p) for n, p in sorted(checkpoint_files.items())}
    verify_replay(root, checkpoints, summary)
    freeze = ROOT / 'docs/reports/data/memory-fit-diagnostic-20261110/pre-fit-freeze.json'
    files['pre-fit-freeze.json'] = freeze
    for name in ('evaluation/audit_memory_fit_diagnostic.py', 'data_pipeline/export_memory_fit_diagnostic.py'):
        files['audit-source/' + name] = ROOT / name
    output.mkdir(parents=True)
    write(output / 'summary.json', summary)
    write(output / 'checkpoint-sha256.json', checkpoints)
    files['summary.json'] = output / 'summary.json'
    deterministic_zip(output / 'predictions.zip', files)
    if (output / 'predictions.zip').stat().st_size > 90 * 1024**2:
        raise ValueError('Shared archive too large')
    write(output / 'shared-manifest.json', {'archive_sha256': sha_file(output / 'predictions.zip'),
        'files_sha256': {n: sha_file(p) for n, p in sorted(files.items())},
        'checkpoint_manifest_sha256': sha_file(output / 'checkpoint-sha256.json'),
        'full_checkpoints_in_shared_zip': False})
    deterministic_zip(root / 'full-review.zip', {**files, **checkpoint_files,
        'checkpoint-sha256.json': output / 'checkpoint-sha256.json'})
    write(output / 'full-review-location.json', {'path': str((root / 'full-review.zip').resolve()),
        'sha256': sha_file(root / 'full-review.zip'), 'bytes': (root / 'full-review.zip').stat().st_size, 'in_git': False})
    return check(output)


def check(path):
    manifest = read(path / 'shared-manifest.json')
    if sha_file(path / 'predictions.zip') != manifest['archive_sha256']:
        raise ValueError('Prediction archive changed')
    if sha_file(path / 'checkpoint-sha256.json') != manifest['checkpoint_manifest_sha256']:
        raise ValueError('Checkpoint manifest changed')
    with zipfile.ZipFile(path / 'predictions.zip') as z, tempfile.TemporaryDirectory() as tmp:
        names = z.namelist()
        if len(names) != len(set(names)) or set(names) != set(manifest['files_sha256']):
            raise ValueError('Duplicate or incomplete archive')
        for name, sha in manifest['files_sha256'].items():
            if Path(name).is_absolute() or '..' in Path(name).parts or digest(z.read(name)) != sha:
                raise ValueError('Unsafe or changed archive member')
        root = Path(tmp); z.extractall(root)
        result = audit_saved(root / 'data', root / 'training')
        if not metric_equal(result, read(root / 'summary.json')) or not metric_equal(result, read(path / 'summary.json')):
            raise ValueError('Published summary differs from independently reconstructed metrics')
        freeze = read(root / 'pre-fit-freeze.json')
        if sha_file(root / 'data/manifest.json') != freeze['data_manifest_sha256']:
            raise ValueError('Data differs from pre-fit freeze')
        verify_replay(root, read(path / 'checkpoint-sha256.json'), result)
        return {'models': len(result['models']), 'saved_head_predictions_replayed': 88,
            'intermediate_predictions_recounted': 792, 'actual_checkpoint_replay_records_verified': 88,
            'primary_pass_fits': sum(m['metrics']['primary_pass'] for m in result['models']),
            'scope': 'Training examples only; this command uses no GPU and verifies the stored actual-checkpoint audit.'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['export', 'check'])
    p.add_argument('--root', type=Path)
    p.add_argument('--path', type=Path, required=True)
    a = p.parse_args()
    if a.command == 'export' and a.root is None:
        p.error('--root is required for export')
    print(json.dumps(export(a.root, a.path) if a.command == 'export' else check(a.path), indent=2))
