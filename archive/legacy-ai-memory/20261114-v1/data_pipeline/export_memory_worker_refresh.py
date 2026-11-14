"""Share fresh raw outputs and reproduce every label without GPU access."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

from data_pipeline.memory_decision_dataset import ROOT, readl, write, digest
from data_pipeline.export_memory_state_ablation import deterministic_zip
from evaluation.audit_memory_fit_diagnostic import metric_equal
from evaluation.memory_worker_refresh import evaluate
from evaluation.analyze_memory_worker_refresh import analyze
from training.memory_selector.run_memory_contrast_worker import sha_file


def load(path):
    return json.loads(path.read_text())


def verify_dataset(path):
    manifest = load(path / 'manifest.json')
    if manifest['measurement_version'] != 'memory-worker-refresh-20261110-v1':
        raise ValueError('Unexpected measurement version')
    for name, sha in manifest['files_sha256'].items():
        if sha_file(path / name) != sha:
            raise ValueError('Refreshed dataset file changed: ' + name)
    for name, sha in manifest['source_sha256'].items():
        if sha_file(ROOT / name) != sha:
            raise ValueError('Refreshed dataset source changed: ' + name)
    rows, targets, actions = [readl(path / name) for name in ('examples.jsonl', 'targets.jsonl', 'all-supervision.jsonl')]
    if (len(rows), len(targets), len(actions)) != (408, 408, 504):
        raise ValueError('Incomplete published learning dataset')
    if [r['example_id'] for r in rows] != [r['example_id'] for r in targets]:
        raise ValueError('Input/target identities differ')
    if any(r['measurement_version'] != manifest['measurement_version'] for r in targets + actions):
        raise ValueError('Mixed measurement versions')
    return manifest


def export(root, run, output, dataset):
    if output.exists() or dataset.exists():
        raise FileExistsError('Use new result and dataset destinations')
    measured = root / 'measured'
    verify_dataset(measured)
    if load(run / 'run-status.json')['status'] != 'complete':
        raise ValueError('Incomplete worker run')
    files = {}
    for prefix, folder in [('design', root / 'design'), ('dataset', measured)]:
        for p in folder.rglob('*'):
            if p.is_file():
                files[prefix + '/' + str(p.relative_to(folder))] = p
    for name in ('run-status.json', 'upload.zip', 'upload-manifest.json'):
        files['worker/' + name] = run / name
    # Only allowlisted public artifacts; no credentials/session-state/notebook files.
    for name in ('config.json', 'protocol.json', 'environment.json', 'worker-provenance.json',
                 'results.json', 'predictions.jsonl', 'repeat-predictions.json', 'pip-freeze.txt'):
        files['worker/artifacts/' + name] = run / 'artifacts' / name
    for p in (run / 'artifacts/worker-tokenizer').iterdir():
        if p.is_file():
            files['worker/artifacts/worker-tokenizer/' + p.name] = p
    for p in (ROOT / 'docs/reports/data/memory-worker-refresh-20261110').glob('*.json'):
        files['protocol-evidence/' + p.name] = p
    for name in ('worker-colab-controller.log', 'preflight-tests.log', 'evaluator-tests.log', 'resource-accounting.json'):
        files['execution-evidence/' + name] = root / name
    for name in ('data_pipeline/prepare_memory_worker_refresh.py', 'evaluation/memory_worker_refresh.py',
                 'evaluation/analyze_memory_worker_refresh.py',
                 'data_pipeline/export_memory_worker_refresh.py', 'evaluation/tests/test_memory_worker_refresh.py'):
        files['verification-source/' + name] = ROOT / name
    if any(not p.is_file() for p in files.values()):
        raise ValueError('Missing actual execution or audit evidence')
    output.mkdir(parents=True)
    dataset.mkdir(parents=True)
    for p in measured.iterdir():
        if p.is_file():
            shutil.copy2(p, dataset / p.name)
    deterministic_zip(output / 'predictions.zip', files)
    write(output / 'shared-manifest.json', {'archive_sha256': sha_file(output / 'predictions.zip'),
        'files_sha256': {name: sha_file(p) for name, p in sorted(files.items())},
        'dataset_path': str(dataset.relative_to(ROOT)) if dataset.is_absolute() else str(dataset),
        'worker_weights_in_archive': False,
        'measurement_version': 'memory-worker-refresh-20261110-v1'})
    for name in ('summary.json', 'policy-summary.json', 'verification.json', 'stability.json'):
        shutil.copy2(measured / name, output / name)
    return check(output)


def check(path):
    manifest = load(path / 'shared-manifest.json')
    if sha_file(path / 'predictions.zip') != manifest['archive_sha256']:
        raise ValueError('Refreshed result archive changed')
    relative_dataset = Path(manifest['dataset_path'])
    if relative_dataset.is_absolute() or '..' in relative_dataset.parts:
        raise ValueError('Unsafe published dataset path')
    public_dataset = ROOT / relative_dataset
    verify_dataset(public_dataset)
    with zipfile.ZipFile(path / 'predictions.zip') as z, tempfile.TemporaryDirectory() as tmp:
        if len(z.namelist()) != len(set(z.namelist())) or set(z.namelist()) != set(manifest['files_sha256']):
            raise ValueError('Incomplete refreshed result archive')
        for name, sha in manifest['files_sha256'].items():
            if Path(name).is_absolute() or '..' in Path(name).parts or digest(z.read(name)) != sha:
                raise ValueError('Unsafe or changed refreshed archive member')
        root = Path(tmp); z.extractall(root)
        saved_manifest = verify_dataset(root / 'dataset')
        if saved_manifest != load(public_dataset / 'manifest.json'):
            raise ValueError('Shared/published dataset manifest differs')
        freeze = load(root / 'protocol-evidence/pre-collection-freeze.json')
        if sha_file(root / 'design/manifest.json') != freeze['design_manifest_sha256']:
            raise ValueError('Design differs from pre-collection freeze')
        evaluate(root / 'design', root / 'worker', root / 'recomputed')
        for name in saved_manifest['files_sha256']:
            if (root / 'dataset' / name).read_bytes() != (public_dataset / name).read_bytes():
                raise ValueError('Published dataset differs from archived evidence')
            a, b = root / 'dataset' / name, root / 'recomputed' / name
            actual, expected = (readl(a), readl(b)) if name.endswith('.jsonl') else (load(a), load(b))
            if not metric_equal(actual, expected):
                raise ValueError('Refreshed labels/metrics differ from actual raw output reconstruction: ' + name)
        if not metric_equal(load(root / 'protocol-evidence/content-review.json'), analyze(root / 'dataset', root / 'design')):
            raise ValueError('Content review differs from measured dataset')
        for name in ('summary.json', 'policy-summary.json', 'verification.json', 'stability.json'):
            if not metric_equal(load(path / name), load(root / 'recomputed' / name)):
                raise ValueError('Published result differs from reconstructed output')
        return {'fresh_unique_outputs_verified': 480, 'stability_repeats_verified': 4,
            'state_action_labels_reconstructed': 504, 'nonempty_learning_examples': 408,
            'states': 96, 'families': 12, 'independent_sql_states': 96,
            'old_current_outputs_reused_for_targets': 0, 'new_model_execution': False,
            'scope': 'Recomputed raw saved outputs and labels; no GPU model replay.'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['export', 'check'])
    for name in ('root', 'run', 'dataset'):
        p.add_argument('--' + name, type=Path)
    p.add_argument('--path', type=Path, required=True)
    a = p.parse_args()
    if a.command == 'export' and any(x is None for x in (a.root, a.run, a.dataset)):
        p.error('--root, --run and --dataset are required for export')
    print(json.dumps(export(a.root, a.run, a.path, a.dataset) if a.command == 'export' else check(a.path), indent=2))
