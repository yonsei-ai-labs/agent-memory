"""Export authored worker prompts, generated code and judgments, without weights."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text())
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def export(worker,train_dev,test,utility,output):
    if output.exists():raise FileExistsError(output)
    status=read(worker/'run-status.json');audit=read(worker/'audit.json');result=read(worker/'artifacts/results.json')
    if status['status']!='complete' or not status['stopped'] or audit['status']!='passed' or result['protocol']['pilot']:
        raise ValueError('Only a completed audited full worker run may be shared')
    fence=read(test/'selection-before-judging.json')
    if fence['utility_selection_sha256']!=sha(utility/'policy-selection.json') or fence['relation_selection_sha256']!=sha(utility/'relation-selection.json'):
        raise ValueError('Worker test was not judged after this policy freeze')
    for path,splits in [(train_dev,['train','dev']),(test,['test'])]:
        summary=read(path/'summary.json')
        if summary['allowed_splits']!=splits or summary['files_sha256']['predictions']!=sha(worker/'artifacts/predictions.jsonl'):
            raise ValueError('Judged split/source differs')
    output.mkdir(parents=True);reproduction=output/'reproduction';reproduction.mkdir()
    with zipfile.ZipFile(worker/'upload.zip') as archive:
        for name in ['jobs.jsonl','protocol.json']:(output/name).write_bytes(archive.read('data/'+name))
        for name in ['generate_api_worker.py','prepare_api_worker.py']:(reproduction/name).write_bytes(archive.read(name))
    excluded={'input_token_ids','output_token_ids','rendered_prompt'}
    with (output/'model-outputs.jsonl').open('w') as stream:
        for line in (worker/'artifacts/predictions.jsonl').read_text().splitlines():
            row=json.loads(line);stream.write(json.dumps({k:v for k,v in row.items() if k not in excluded},ensure_ascii=False,allow_nan=False)+'\n')
    with (output/'judgments.jsonl').open('w') as stream:
        for path in [train_dev,test]:stream.write((path/'judgments.jsonl').read_text())
    for name in ['api_worker_judge.py','api_worker_interpreter.py','api_worker_judge_serialized.py']:shutil.copyfile(ROOT/'data_pipeline'/name,reproduction/name)
    for name in ['policy-selection.json','relation-selection.json']:shutil.copyfile(utility/name,output/name)
    if (utility/'semantic-selection.json').exists():shutil.copyfile(utility/'semantic-selection.json',output/'semantic-selection.json')
    if (utility/'development-policy-choice.json').exists():shutil.copyfile(utility/'development-policy-choice.json',output/'development-policy-choice.json')
    shutil.copyfile(test/'selection-before-judging.json',output/'selection-before-judging.json')
    data=ROOT/'datasets/api_transfer/20261108-v1'
    write(output/'data-reference.json',{'source':str(data.relative_to(ROOT)),'artifact_index_sha256':sha(data/'artifact-index.json'),
          'splits_sha256':{s:sha(data/(s+'.jsonl')) for s in ['train','dev','test']},
          'interpretation':'Authored API wrapper tasks. States, actions, and fixtures are dependent; these are not natural SWE episodes.'})
    write(output/'provenance.json',{'worker_run_id':worker.name,'config':result['config'],'environment':read(worker/'artifacts/environment.json'),
        'weight_provenance':read(worker/'artifacts/weight-provenance.json'),'original_audit':audit,
        'original_artifacts_sha256':{name:sha(worker/name) for name in ['upload.zip','results.zip','upload-manifest.json','run-status.json']},
        'original_predictions_sha256':sha(worker/'artifacts/predictions.jsonl'),
        'original_judgments_sha256':{'train_dev':sha(train_dev/'judgments.jsonl'),'test':sha(test/'judgments.jsonl')},
        'judgment_summaries':{'train_dev':read(train_dev/'summary.json'),'test':read(test/'summary.json')},
        'native_features':{'phase':'before_generated_tokens_and_without_memory','states':result['native_state_features'],
                           'extra_prefill_seconds':result['native_feature_extra_prefill_seconds'],'binary_features_shared':False},
        'transport_runtime_stopped':True,'base_model_weights_shared':False,'raw_token_arrays_shared':False})
    files=sorted(str(p.relative_to(output)) for p in output.rglob('*') if p.is_file())
    write(output/'manifest.json',{'format':'authored-api-worker-utility-v1','files_sha256':{n:sha(output/n) for n in files},
        'exporter_sha256':sha(Path(__file__)),'state_count':96,'base_generations':960,'repeat_generations':4})
    from data_pipeline.verify_api_worker_utility import verify
    checked=verify(output);write(output/'verification.json',checked)
    (output/'README.md').write_text('''# 고정 작업자에서 측정한 기억 제공의 득실

Qwen3.5-4B의 고정 revision/BF16/greedy와 v3 출력 계약으로 96상태×10행동을 실행했다. 추가 4개는 동일 prompt 재현 검사다. 작업자는 기억의 절차를 바꿔 사용할 수 있다. 학습 50·개발 18·확인 28상태이며, 확인의 실행 판정은 효용 head와 관계 정책을 고정한 뒤 했다.

`jobs.jsonl`은 실제 업로드한 정답 없는 prompt, `model-outputs.jsonl`은 생성한 코드와 토큰 수·지연, `judgments.jsonl`은 두 fixture와 가능한 현재 구현에서 얻은 실행 판정이다. 생성 코드의 임의 Python 실행 대신 공개된 제한 AST 실행기를 사용했다. 미지원 코드는 별도로 표시하며 엄격한 성공에는 포함하지 않는다.

```bash
python3 -m data_pipeline.verify_api_worker_utility DATASET_DIRECTORY
```

추가 패키지 없이 prompt/출처 해시, 코드 연결, 기록된 반환값·원본 변화·통과 판정을 검산한다. 이는 모델을 다시 생성하거나 코드를 새로 실행하는 검사가 아니다. 새 실행에는 원래 API 자료의 pinned runtime 환경이 필요하다. 모델 가중치·저장 hidden state·원래 token ID 배열은 Git에 포함하지 않았으며 전체 감사를 위해 원본 upload.zip/results.zip이 필요하다.

하나의 API 구현에서 파생된 상태·네 후보·제공 방식·두 fixture를 독립 경험으로 세지 않는다. 이 자료는 작은 저작 API 작업의 실제 worker 득실이며 자연 SWE 해결률을 대신하지 않는다. 세 번의 학습 pilot 중 형식 계약이 고정된 v3 전체 실행만 배포 데이터로 사용했다.
'''.replace('DATASET_DIRECTORY',str(output.relative_to(ROOT))))
    print(json.dumps(checked,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for name in ['worker','train-dev','test','utility','output']:p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();export(a.worker.resolve(),a.train_dev.resolve(),a.test.resolve(),a.utility.resolve(),a.output.resolve())
