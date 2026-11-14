"""Share exact saved predictions, features and heads; retain full weights locally."""
import argparse
import json
import math
from pathlib import Path
import zipfile

from data_pipeline.memory_decision_dataset import ROOT, readl, write, digest
from data_pipeline.memory_evidence_dataset import BASE

CORE = ('config.json', 'protocol.json', 'data-manifest.json', 'environment.json', 'weight-provenance.json',
        'token-lengths.json', 'run-status.json', 'text-index.jsonl', 'frozen-vectors.npz', 'frozen-encoding-times.json')
MODEL_FILES = ('head.npz', 'held-vectors.npz', 'predictions.jsonl', 'train-predictions.jsonl',
               'fit.json', 'curve.json', 'adapter-changes.json', 'exposure.json')


def deterministic_zip(path, files):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, source in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 9, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, source.read_bytes())


def export(run_root, evaluation, output):
    if output.exists():
        raise FileExistsError(output)
    art, data = run_root / 'artifacts', run_root / 'data'
    status = json.loads((art / 'run-status.json').read_text())
    if status['status'] != 'complete':
        raise ValueError('Only a completed run may be shared')
    files = {f'artifacts/{name}': art / name for name in CORE}
    files.update({f'data/{p.name}': p for p in data.iterdir() if p.is_file()})
    files.update({f'evaluation/{p.name}': p for p in evaluation.iterdir() if p.is_file()})
    files.update({f'launch-source/{p.relative_to(run_root / "source")}': p for p in (run_root / 'source').rglob('*') if p.is_file()})
    files['final-evaluator/evaluation/memory_state_ablation.py'] = ROOT / 'evaluation/memory_state_ablation.py'
    files['final-evaluator/evaluation/verify_memory_state_adapters.py'] = ROOT / 'evaluation/verify_memory_state_adapters.py'
    files['final-evaluator/evaluation/tests/test_memory_state_ablation.py'] = ROOT / 'evaluation/tests/test_memory_state_ablation.py'
    files['source-outcomes/supervision.jsonl'] = BASE / 'supervision.jsonl'
    files['source-outcomes/provenance.jsonl'] = BASE / 'provenance.jsonl'
    for name in ('launch-manifest.json', 'environment-lock.txt', 'input-ambiguity.json', 'action-space-diagnostic.json',
                 'adapter-replay.json', 'tokenizer-audit.py', 'tokenizer-audit.json',
                 'local-validation.log', 'training.log'):
        files[name] = run_root / name
    for name in status['completed_models']:
        files.update({f'artifacts/{name}/{leaf}': art / name / leaf for leaf in MODEL_FILES})
    for p in files.values():
        if not p.is_file():
            raise ValueError('Missing export evidence: ' + str(p))
    output.mkdir(parents=True)
    archive = output / 'predictions.zip'
    deterministic_zip(archive, files)
    # Large adapter/optimizer files stay in the original run and in a separate
    # complete review archive; they are never silently claimed to be in Git.
    checkpoint_files = {str(p.relative_to(art)): digest(p.read_bytes()) for p in sorted(art.rglob('*'))
                        if p.is_file() and (p.name == 'optimizer.pt' or 'adapter' in p.parts)}
    write(output / 'checkpoint-sha256.json', checkpoint_files)
    write(output / 'shared-manifest.json', {'archive_sha256': digest(archive.read_bytes()),
        'files_sha256': {name: digest(p.read_bytes()) for name, p in sorted(files.items())},
        'checkpoint_manifest_sha256': digest((output / 'checkpoint-sha256.json').read_bytes()),
        'full_checkpoints_in_shared_zip': False,
        'full_local_review_archive': str(run_root / 'full-review.zip'),
        'base_worker_source_sha256': digest((ROOT / 'evaluation/results/travel-policy-followup-20261109-v1/predictions.zip').read_bytes())})
    for name in ('policy-summary.json', 'classification.json', 'paired-input-comparison.json', 'fits.json', 'verification.json'):
        (output / name).write_bytes((evaluation / name).read_bytes())
    complete = dict(files)
    for path in art.rglob('*'):
        if path.is_file() and (path.name == 'optimizer.pt' or 'adapter' in path.parts):
            complete['artifacts/' + str(path.relative_to(art))] = path
    complete['checkpoint-sha256.json'] = output / 'checkpoint-sha256.json'
    deterministic_zip(run_root / 'full-review.zip', complete)
    write(output / 'full-review-location.json', {'path': str(run_root / 'full-review.zip'),
          'sha256': digest((run_root / 'full-review.zip').read_bytes()),
          'bytes': (run_root / 'full-review.zip').stat().st_size, 'in_git': False})
    return check(output)


def check(path):
    import numpy as np
    record = json.loads((path / 'shared-manifest.json').read_text())
    archive = path / 'predictions.zip'
    if digest(archive.read_bytes()) != record['archive_sha256']:
        raise ValueError('Changed shared archive')
    if digest((path / 'checkpoint-sha256.json').read_bytes()) != record['checkpoint_manifest_sha256']:
        raise ValueError('Changed checkpoint manifest')
    import io
    verified, maximum_error = 0, 0.0
    with zipfile.ZipFile(archive) as z:
        if len(z.namelist()) != len(set(z.namelist())) or set(z.namelist()) != set(record['files_sha256']):
            raise ValueError('Incomplete or duplicate shared files')
        for name, sha in record['files_sha256'].items():
            if digest(z.read(name)) != sha:
                raise ValueError('Changed shared member: ' + name)
        status = json.loads(z.read('artifacts/run-status.json'))
        config = json.loads(z.read('artifacts/config.json'))
        folds = json.loads(z.read('data/folds.json'))
        examples = {r['example_id']: r for r in map(json.loads, z.read('data/examples.jsonl').decode().splitlines())}
        expected_names = {f'{kind}--{view}--fold{fold["fold"]}--seed{seed}' for kind in ('frozen', 'lora')
                          for view in config['views'] for fold in folds for seed in config['seeds']}
        if status['status'] != 'complete' or set(status['completed_models']) != expected_names:
            raise ValueError('Incomplete experiment grid')
        for name in sorted(expected_names):
            prefix = f'artifacts/{name}/'
            fit = json.loads(z.read(prefix + 'fit.json'))
            rows = [json.loads(l) for l in z.read(prefix + 'predictions.jsonl').decode().splitlines()]
            expected = {eid for eid, r in examples.items() if r['family_id'] in folds[fit['fold']]['held_families']}
            if len(rows) != len(expected) or {r['example_id'] for r in rows} != expected:
                raise ValueError('Wrong held-out prediction identities')
            x = np.load(io.BytesIO(z.read(prefix + 'held-vectors.npz')))['vectors'].astype(np.float64)
            w = np.load(io.BytesIO(z.read(prefix + 'head.npz')))
            hidden = x @ w['net.0.weight'].T + w['net.0.bias']
            hidden = 0.5 * hidden * (1 + np.vectorize(math.erf)(hidden / math.sqrt(2)))
            logits = hidden @ w['net.2.weight'].T + w['net.2.bias']
            p = np.exp(logits - logits.max(1, keepdims=True))
            p /= p.sum(1, keepdims=True)
            saved = np.array([r['probabilities'] for r in rows])
            if not np.isfinite(saved).all() or not np.isfinite(p).all() or saved.shape != p.shape:
                raise ValueError('Invalid saved predictions')
            error = float(np.abs(p - saved).max())
            if error > 2e-5 or not np.array_equal(p.argmax(1), saved.argmax(1)):
                raise ValueError('Independent head reconstruction failed')
            maximum_error = max(maximum_error, error)
            verified += len(rows)
        for name in ('policy-summary.json', 'classification.json', 'paired-input-comparison.json', 'fits.json', 'verification.json'):
            if (path / name).read_bytes() != z.read('evaluation/' + name):
                raise ValueError('Public summary differs from original analysis')
    return {'models': len(expected_names), 'reconstructed_predictions': verified,
            'maximum_probability_error': maximum_error, 'uses_gpu': False,
            'scope': 'Exact archive hashes and saved-vector/head replay. LoRA re-encoding requires the full local checkpoints.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['export', 'check'])
    parser.add_argument('--path', type=Path, required=True)
    parser.add_argument('--run-root', type=Path)
    parser.add_argument('--evaluation', type=Path)
    args = parser.parse_args()
    result = check(args.path) if args.command == 'check' else export(args.run_root, args.evaluation, args.path)
    print(json.dumps(result, indent=2))
