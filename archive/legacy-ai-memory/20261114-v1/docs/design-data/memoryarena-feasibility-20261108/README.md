# MemoryArena 실행 전 검토 근거

[판단과 제안](../../ai-memory-memoryarena-feasibility-2026-11-08.md)을 뒷받침하는 기록이다. 모델의 벤치 성적이나 학습 자료가 아니다. 높은 진단 점수는 불완전한 제출이 통과하는 동작을 재현한 것이며 기억 성능의 개선을 뜻하지 않는다.

| 파일 | 역할 |
| --- | --- |
| [intake-plan.json](intake-plan.json) | 다운로드·내용 열람 전에 정한 범위와 제외 작업 |
| [data-inventory.json](data-inventory.json) | 고정 다운로드 URL·크기·해시·묶음/하위 과제 수 |
| [asset-inventory.json](asset-inventory.json) | 별도 제품 DB의 파일 크기와 미확인 비행 DB |
| [offline_probes.py](offline_probes.py) | Python 표준 라이브러리만 사용하는 재현 코드 |
| [offline-checks.json](offline-checks.json) | 원본 코드/검사 코드 해시와 실제 진단 출력 |

코드는 `ZexueHe/MemoryArena@6cd9de14b71915e39ac742a20dc33785e14b6aab`, 데이터는 `ZexueHe/memoryarena@da1a37c8b19280e18627ca01cf368195a5e1d92e`다. [공식 데이터 카드](https://huggingface.co/datasets/ZexueHe/memoryarena/blob/da1a37c8b19280e18627ca01cf368195a5e1d92e/README.md)의 CC BY 4.0 표기와 논문 인용 안내를 따른다. upstream 코드와 전체 원본 데이터는 이 묶음에 복사하지 않는다.

내용까지 살펴본 여행 `id=1`과 쇼핑 `id=0`은 탐색용이다. 다른 행은 해시·개수·스키마·배경 중복 확인에만 사용했다. 채점 진단은 첫 여행 묶음과 그 합성 복제본만 사용하며, 임시 제출 파일은 실행 후 삭제한다. `learner_export_allowed=false`, 실제 제공/보류 결과 0개다.

검사 범위는 의도적으로 좁다. 데이터 로더에서 외부 다운로드 import를 빼고 이미 고정한 첫 행을 반환한다. 원래 채점·환경·프롬프트 메서드의 본문은 유지한다. 모델과 도구 실행기는 가짜 응답·실행 금지 객체로 대체하며, HTTP 서버와 실제 도구 호출은 실행하지 않는다. 따라서 패키지 설치나 전체 벤치 실행 성공을 검증하지 않는다. `none`의 프롬프트 검사는 runner의 인자 AST와 agent 메서드를 함께 확인한다.

저장된 결과의 `expected_persons`/`expected_groups`는 우리 진단이 기대한 제출 수이고, `counted_*`는 원판 채점기가 실제 사용한 분모다. 두 값의 차이가 누락 처리 문제다. 후속 학습 준비에서 전체 `base_person` 객체뿐 아니라 이름·질문 표현을 제외한 `daily_plans`도 비교하도록 보완했다. 서로 다른 기본 계획은 45개이고 각 계획이 6개 행에서 반복된다. 유사 계획 템플릿의 독립성까지 확인한 것은 아니다.

## 재현

저장소 루트에서 실행한다. 아래 준비는 공개 코드와 약 12.3 MB의 JSONL만 다운로드한다. 이미 이 revision을 내려받았다면 준비를 반복하지 않는다. 대형 DB·모델 가중치·패키지 설치와 API 키는 필요 없다.

```bash
git clone --no-checkout https://github.com/ZexueHe/MemoryArena.git training/memory_selector/outputs/memoryarena-feasibility-20261108/upstream
git -C training/memory_selector/outputs/memoryarena-feasibility-20261108/upstream checkout --detach 6cd9de14b71915e39ac742a20dc33785e14b6aab
python3 - <<'PY'
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

root = Path('training/memory_selector/outputs/memoryarena-feasibility-20261108/data')
inventory = json.loads(Path('docs/design-data/memoryarena-feasibility-20261108/data-inventory.json').read_text())
for entry in inventory['files']:
    destination = root / entry['path']
    if destination.exists():
        payload = destination.read_bytes()
    else:
        with urlopen(entry['url'], timeout=60) as response:
            payload = response.read(entry['bytes'] + 1)
    if len(payload) != entry['bytes'] or hashlib.sha256(payload).hexdigest() != entry['sha256']:
        raise ValueError('Unexpected data: ' + entry['path'])
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
print('Five pinned files verified')
PY
python3 docs/design-data/memoryarena-feasibility-20261108/offline_probes.py \
  --upstream training/memory_selector/outputs/memoryarena-feasibility-20261108/upstream \
  --data-root training/memory_selector/outputs/memoryarena-feasibility-20261108/data \
  --output /tmp/memoryarena-offline-checks.json
diff -u docs/design-data/memoryarena-feasibility-20261108/offline-checks.json /tmp/memoryarena-offline-checks.json
```

JSON에 시각이나 임시 디렉터리 이름을 넣지 않아 같은 코드와 데이터의 결과를 비교할 수 있다. 다른 upstream revision에는 이 검사를 그대로 적용하지 않고, 변경을 확인한 뒤 새 검토 기록을 만든다.

이번 문서 추가와 함께 기존 공유 데이터 74행과 저장 예측 444개도 검산했다. 평가 테스트 30개는 통과했고, 학습 관련 테스트 112개 중 107개는 통과·5개는 CPU Torch가 없는 환경이라 skip됐다. 기존 모델을 다시 학습하거나 실제 Colab에 연결한 검사는 아니다.
