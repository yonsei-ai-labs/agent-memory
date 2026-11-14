# 신규 문제군의 현재 상태별 기억 제공 실험 명세

이 폴더는 실행 결과를 보기 전에 고정한 **입력 명세**다. 120개의 실제 과거 실행 기록이 이미 수집됐다는 뜻은 아니다. 설계와 평가 경계는 [실험 노트](../../../docs/ai-memory-travel-state-transfer-2026-11-09.md)를 따른다.

- train/dev/test: 출발지·목적지·기간 기준 60/20/40개 문제군. 첫 수집은 train 중 `pilot=true` 12개.
- `families.json`: 공개 요청의 출처·분할·카탈로그 메뉴. 원래 요청은 출처 정보이며 전체 원래 요청을 해결하는 실험이 아니다.
- `source-audit.json`: 원본 revision·SHA, 제외 사유, 선택하지 않은 적격 문제군.
- `source-specifications.zip`: 위 자료와 `cases.json`, `source-jobs.json`, `catalog.json`, `manifest.json`을 포함하는 전체 입력. 과거 선택의 정답이나 미래 worker 출력은 없다.
- `manifest.json`: 압축 내부 JSON의 고정 SHA와 생성기 SHA. `archive-sha256.json`은 압축 파일 SHA다.

공개 원본: [TravelPlanner](https://huggingface.co/datasets/osunlp/TravelPlanner/tree/8736504ecfc31b7f8b7e40122873c337e83fff7c), 카탈로그는 [MemoryArena](https://github.com/ZexueHe/MemoryArena) checkout의 `env/env_systems/travel_planner_env/database`에 있는 식당·숙소 CSV다. 파일별 SHA는 `source-audit.json`에 있다. 원본 이름과 수치의 출처를 유지하며 여행자 이름·후속 질문·현재 가용성 변화는 합성 통제 요소다. 원본 프로젝트의 라이선스와 이용 조건은 그대로 적용된다.

GPU 없이 풀고 검증하기:

```bash
python3 - <<'PY'
import hashlib, json, zipfile
from pathlib import Path
p = Path('datasets/travel_state_transfer/20261109-v1')
for name, expected in json.loads((p / 'archive-sha256.json').read_text()).items():
    assert hashlib.sha256((p / name).read_bytes()).hexdigest() == expected
with zipfile.ZipFile(p / 'source-specifications.zip') as z:
    manifest = json.loads(z.read('manifest.json'))
    for name, expected in manifest['sha256'].items():
        assert hashlib.sha256(z.read(name)).hexdigest() == expected
    destination = Path('/tmp/travel-state-source-v1')
    destination.mkdir(exist_ok=False)
    z.extractall(destination)
print('Input archive verified:', destination)
PY
```

처음부터 재생성하려면 pinned 공개 train/validation CSV와 카탈로그를 준비하고 다음을 실행한다. 파일 내용이 원본 SHA와 다르면 중단한다. 생성 후 공유 `manifest.json`의 데이터 해시와 대조한다.

```bash
python3 data_pipeline/travel_state_transfer.py \
  --source /path/to/pinned-travelplanner-csvs \
  --database /path/to/pinned/travel_planner_env/database \
  --output /tmp/travel-state-regenerated
```

첫 GPU 수집은 실제 이전 모델 archive가 있어야 한다. 큰 가중치를 가진 로컬 `outputs/`는 Git 공유 대상이 아니다. 같은 이름의 다른 checkpoint로 대체하지 않는다.

```bash
python3 training/memory_selector/prepare_travel_state.py \
  --source /tmp/travel-state-source-v1 \
  --prior-run training/memory_selector/outputs/travel-candidate-search-20261109-a100 \
  --original-source datasets/travel_retrieval/20261109-full-request-v1 \
  --config training/memory_selector/configs/travel-state-pilot-v1.json \
  --output training/memory_selector/data/travel-state-pilot-v1
python3 training/memory_selector/run_colab.py \
  --profile travel-state --gpu A100 --transport console \
  --max-runtime-seconds 3900 \
  --output training/memory_selector/outputs/travel-state-pilot-20261109-a100
```

두 번째 명령은 Colab 자원을 실제 할당한다. 컨트롤러는 완료/실패 시 자기 세션을 종료하고 상태를 저장한다. 입력 검증 및 모의 종료 테스트는 GPU를 할당하지 않는다.
