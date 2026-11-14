"""Share the 9B worker's same-prompt, fixed-policy transfer experiment."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import zipfile
from data_pipeline.export_api_worker_utility import sha, read, write, ROOT


def export(worker, judged, frozen, original_share, output):
    if output.exists(): raise FileExistsError(output)
    audit = read(worker / 'audit.json'); result = read(worker / 'artifacts/results.json')
    if audit['status'] != 'passed' or not read(worker / 'run-status.json')['stopped'] or result['generated_jobs'] != 284:
        raise ValueError('Completed and audited 9B transfer required')
    summary = read(judged / 'summary.json'); policy = read(judged / 'policy-results.json')
    if summary['allowed_splits'] != ['test'] or summary['files_sha256']['predictions'] != sha(worker / 'artifacts/predictions.jsonl'):
        raise ValueError('Unexpected transfer judgment source')
    if policy['frozen_transfer_policy_sha256'] != sha(frozen): raise ValueError('Transfer choices changed')
    output.mkdir(parents=True); reproduction = output / 'reproduction'; reproduction.mkdir()
    with zipfile.ZipFile(worker / 'upload.zip') as archive:
        for name in ['jobs.jsonl', 'protocol.json']: (output / name).write_bytes(archive.read('data/' + name))
        for name in ['generate_api_worker.py', 'prepare_api_worker.py']: (reproduction / name).write_bytes(archive.read(name))
    with (output / 'model-outputs.jsonl').open('w') as stream:
        for line in (worker / 'artifacts/predictions.jsonl').read_text().splitlines():
            row = json.loads(line)
            stream.write(json.dumps({k: v for k, v in row.items() if k not in ['input_token_ids', 'output_token_ids', 'rendered_prompt']},
                                    ensure_ascii=False, allow_nan=False) + '\n')
    for name in ['judgments.jsonl', 'policy-results.json']: shutil.copyfile(judged / name, output / name)
    shutil.copyfile(judged.with_suffix('.prejudge.json'), output / 'transfer-before-judging.json')
    shutil.copyfile(frozen, output / 'transfer-policy-freeze.json')
    for name in ['data-reference.json', 'policy-selection.json', 'relation-selection.json', 'semantic-selection.json', 'development-policy-choice.json']:
        shutil.copyfile(original_share / name, output / name)
    for name in ['api_worker_judge.py', 'api_worker_interpreter.py', 'api_worker_judge_serialized.py']:
        shutil.copyfile(ROOT / 'data_pipeline' / name, reproduction / name)
    write(output / 'provenance.json', {'worker_run_id': worker.name, 'config': result['config'],
        'environment': read(worker / 'artifacts/environment.json'),
        'weight_provenance': read(worker / 'artifacts/weight-provenance.json'), 'original_audit': audit,
        'original_artifacts_sha256': {name: sha(worker / name) for name in ['upload.zip', 'results.zip', 'upload-manifest.json', 'run-status.json']},
        'original_predictions_sha256': sha(worker / 'artifacts/predictions.jsonl'),
        'judgment_summaries': {'test': summary}, 'transport_runtime_stopped': True,
        'base_model_weights_shared': False, 'raw_token_arrays_shared': False,
        'original_4b_share_manifest_sha256': sha(original_share / 'manifest.json')})
    names = sorted(str(p.relative_to(output)) for p in output.rglob('*') if p.is_file())
    write(output / 'manifest.json', {'format': 'authored-api-worker-utility-v1',
        'files_sha256': {name: sha(output / name) for name in names}, 'exporter_sha256': sha(Path(__file__)),
        'state_count': 28, 'base_generations': 280, 'repeat_generations': 4})
    from evaluation.verify_api_worker_transfer import verify
    checked = verify(output); write(output / 'verification.json', checked)
    (output / 'README.md').write_text(f'''# 같은 기억 선택을 Qwen3.5-9B로 옮긴 결과

4B 실험과 동일한 확인 28상태·280개 prompt에 고정한 Qwen3.5-9B를 적용했다. 추가 4개는 동일 prompt 반복이다. 모든 선택기의 행동을 9B 생성 전에 동결했고, 9B 결과로 다시 학습하거나 선택하지 않았다. 자료는 같은 API 상태이므로 독립 새 출처 확인이 아니다.

```bash
python3 -m evaluation.verify_api_worker_transfer {output.relative_to(ROOT)}
```

원본 가중치 없이 prompt·기록된 실행·28개 정책의 784개 결정을 검산한다. 무기억 18/28, 사전 주 선택기 16/28, 개발에서 고른 보정층 16/28, 선호 학습 두 seed 각각 20/28, 4B 개발에서 고른 고정 절차 21/28이다. 학습한 선택기가 고정 기준선을 능가했다고 말할 수 없다.

Qwen 상태를 입력한 정책은 4B 상태에서 미리 고른 행동을 옮겼다. 9B hidden state로 바꾸지 않았으며, 실제 배포라면 별도 4B 특징 추출 비용이 필요하다. 이 기록으로 해당 native 정책의 가벼운 9B 배포를 주장하지 않는다.
''')
    print(json.dumps(checked, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    for name in ['worker', 'judged', 'frozen', 'original-share', 'output']: p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args(); export(a.worker.resolve(), a.judged.resolve(), a.frozen.resolve(), a.original_share.resolve(), a.output.resolve())
