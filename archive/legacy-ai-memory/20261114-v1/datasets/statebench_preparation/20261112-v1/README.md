# STATE-Bench 학습 기록과 선택 평가 준비

공개 train 실행 기록 300개·관측 접두부 860개·도구 관측 후보 2,842개를 수집한 준비 자료임. 원본은 모의 업무 환경에서 생성된 에이전트 실행 기록이며 실제 고객 로그가 아님. 실제 득실 라벨·선택 정답·새 모델 학습 결과는 없음. [검토 보고서](../../../docs/reports/2026-11-12-statebench-preparation.md)

- 원천: [Microsoft STATE-Bench](https://github.com/microsoft/STATE-Bench/tree/5644b1838d96bc4483da29642d058ecaa6f80f7f), revision `5644b1838d96bc4483da29642d058ecaa6f80f7f`.
- 원천 이용 조건: MIT. `source-snapshot.zip`에 원본 LICENSE와 파일별 해시를 포함함.
- 분할: 공식 train 내부에서 기억 출처 239개·개발 대상 61개. 같은 정규화 첫 요청을 묶음. 의미상 모든 과제 가족의 중복 제거를 보장하지 않음.
- 공식 test 문제와 환경 JSON은 포함하지 않음. 공개 자료의 전체 train 기록 수와 새 학습의 감독 사례 수를 혼동하지 않음.
- 후보의 `review_status=unreviewed`를 유지함. 사용자 턴 뒤의 정답 행동·최종 성공·효용을 임의로 라벨링하지 않음.

## 파일

| 파일 | 내용 |
| --- | --- |
| `protocol.json` | 원래 연구 질문, 비교 조건, 입력 경계, 단계별 실행 범위 |
| `audit.json` | 300개 기록 구조·도구 신호·중복 집계 |
| `sources.jsonl` | 원본 경로·해시·그룹·분할과 구조 집계 |
| `content-review.jsonl` | 학습 출처 12개 지정 구간의 해석·근거 위치·한계 |
| `review-sampling.json` | 성적을 사용하지 않은 내용 검토 표집 규칙 |
| `source-snapshot.zip` | 원본 train 기록, 런타임 코드, 공개 문서·프로토콜·LICENSE 및 해시 |
| `prepared.zip` | 860개 접두부, 2,842개 관측 후보, 정책 응답, 고정 분할·해시 |
| `cpu-diagnostics.zip` | 개발 접두부의 CPU 선택·예산 검사. 실제 모델 성적 아님 |
| `verification.json` | 공유 사본의 자료·선택 로그 검산 및 테스트 결과 |
| `verification-tests.txt` | 공유 사본의 공식 런타임을 사용한 17개 검사 로그 |
| `runtime-packages.txt` | 런타임 연결 검사에 사용한 Python 3.12 환경의 설치 버전 |
| `execution-dependencies.json` | 개발 출처에 대응하는 초기 과제·환경 파일의 존재 확인. 내용 열람·재실행 아님 |
| `manifest.json` | 공유 파일 크기와 SHA-256 |

원본 assistant 메시지는 여러 도구 왕복과 최종 설명을 묶음. 메시지 중간의 과거 상태를 추측해 만들지 않고, 종료 마커를 포함하지 않는 사용자 메시지까지의 접두부만 저장함. 현재 상태 접두부에 미래 행동을 넣지 않음.

관측 후보에는 이전 과제의 식별자와 금액이 들어 있음. 새로운 고객의 현재 사실로 적용하거나 오류 기록을 자동 양성으로 학습하면 안 됨. 이 묶음은 검토 완료한 실험용 기억 은행이 아님.

## 원천부터 다시 준비

저장소 루트에서 실행함. 자료 준비·검산은 Python 표준 라이브러리만 사용함.

```bash
python3 -m data_pipeline.statebench_data acquire --work runs/statebench-reproduction
python3 -m data_pipeline.statebench_data prepare --work runs/statebench-reproduction
python3 -m data_pipeline.statebench_data verify --work runs/statebench-reproduction
python3 -m unittest tests.test_statebench -v
```

`acquire`는 별도 git 캐시를 사용하고 허용한 train·코드·문서만 작업 사본에 품. 만들어진 자료가 있으면 덮어쓰지 않고 `verify`를 사용함. 모델 호출·GPU·test 실행은 없음.

## 공유 압축만 검산

```bash
python3 - <<'PY'
import hashlib, json, zipfile
from pathlib import Path
p = Path('datasets/statebench_preparation/20261112-v1')
manifest = json.loads((p / 'manifest.json').read_text())
for name, info in manifest['files'].items():
    assert hashlib.sha256((p / name).read_bytes()).hexdigest() == info['sha256'], name
work = Path('runs/statebench-shared-copy')
work.mkdir(exist_ok=False)
for name in ['source-snapshot.zip', 'prepared.zip', 'cpu-diagnostics.zip']:
    with zipfile.ZipFile(p / name) as archive:
        archive.extractall(work)
print(work)
PY
python3 -m data_pipeline.statebench_data verify --work runs/statebench-shared-copy
```

`tokenizers`가 설치된 환경에서 저장 선택 567개의 출처·입력·전달 문자열·토큰도 다시 검산할 수 있음. 검색 점수나 작업 성공률을 재평가하는 명령은 아님.

```bash
.venv/bin/python -m evaluation.verify_statebench_preparation --work runs/statebench-shared-copy --tokenizer /path/to/tokenizer.json
```

공식 런타임 연결 검사는 별도 Python 3.12 환경에서 재현함. 이 검사는 모의 작업자와 합성 도구 환경을 쓰며 API 인증이 필요하지 않음.

```bash
uv venv --python 3.12 runs/statebench-shared-copy/venv
uv pip install --python runs/statebench-shared-copy/venv/bin/python -e runs/statebench-shared-copy/upstream pytest
runs/statebench-shared-copy/venv/bin/python -m unittest tests.test_statebench tests.test_statebench_integration -v
```

CPU 선택 진단의 재실행은 `tokenizers`가 설치된 환경과 Qwen3.5-4B의 고정 tokenizer가 필요함. 가중치나 GPU는 필요하지 않음.

```bash
.venv/bin/python -m evaluation.prepare_statebench --work runs/statebench-reproduction --tokenizer /path/to/tokenizer.json
```

tokenizer revision은 `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`임. 실제 작업 평가에는 별도의 작업 모델 연결과 공식 GPT-5.4 평가 클라이언트가 필요하며, 본 자료를 준비했다는 이유로 task 성공률이나 학습 효용을 주장하지 않음.

CPU 진단에서는 은행 후보 2,297개 중 825개가 단독으로도 예산을 넘음. 상태 입력도 최대 7,991토큰임. 후속 실행 전에 근거를 보존하는 기억 단위와 선택기 입력 상한을 정해야 하며, 이 관측 후보를 그대로 학습 정답으로 사용하지 않음.
