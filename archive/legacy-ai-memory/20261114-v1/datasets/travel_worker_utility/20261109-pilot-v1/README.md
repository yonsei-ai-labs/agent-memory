# 여행 선택의 실제 기억 효용 — 구성한 하위 결정 파일럿

[실행 전 설계](../../../docs/ai-memory-travel-worker-utility-plan-2026-11-09.md)를 따른다. 원문 요청 22개(개발 6, 평가 16), 기본 계획 11개(3/8), 과거 슬롯 132개다. 이 숫자를 독립 여행 132개나 362개로 해석하지 않는다.

현재 요청은 MemoryArena 원문 문장이다. 과거 작업은 원래 도시의 실제 벤치 식당 표에서 구성한 메뉴로 새로 수행한다. 실제 생성한 과거 식당 선택을 동결한 후, 현재 필요한 정보가 과거에만 있는 상태와 현재도 보이는 상태에서 5개 기억 정책을 비교한다. 생성 이전에는 실제 효용 정답이 없다. 현재 정답은 생성된 과거 선택에 상대적인 모든 조건으로 계산한다.

- `cases.json`: 원문 참조 요청·현재 도시 목록·이전 사람/날짜/식사 슬롯. 원래 현재 정답 값은 없음.
- `source-jobs.json`: 모델이 새로 수행할 과거 선택 과제. 원래 미래 요청을 포함하지 않음.
- `catalog.json`: 사용 도시와 과거 메뉴에 필요한 공개 식당 데이터 행.
- `judgments.json`: 원문 비교의 구조화 주석. 로컬 채점용이며 GPU 업로드에서 제외.
- `protocol.json`: 표본 선택·정보 조건·정책·원본 해시·해석 범위.
- `manifest.json`: 생성 자료 해시와 생성 코드 해시. README 등 설명 파일은 별도.

선택한 22개 원문·참조 대상·조건식을 생성 전 같은 AI 에이전트가 대조했다. 사람/독립 주석자의 검토는 없다. 같은 후보를 두 상태·여러 정책에서 실행하므로 행 수를 독립 표본 수로 세지 않는다. 평가 계획은 앞선 선택기 학습에서 제외된 8개지만 이전 관계 진단에서 이미 평가한 계획이며, 새로운 독립 여행 출처가 아니다.

원본: [MemoryArena 데이터](https://huggingface.co/datasets/ZexueHe/memoryarena/tree/da1a37c8b19280e18627ca01cf368195a5e1d92e) (CC BY 4.0), [고정 공개 식당 표](https://github.com/ZexueHe/MemoryArena/blob/6cd9de14b71915e39ac742a20dc33785e14b6aab/env/env_systems/travel_planner_env/database/restaurants/clean_restaurant_2022.csv), [TravelPlanner](https://github.com/OSU-NLP-Group/TravelPlanner). 이 파생 과제는 원래 전체 여행 계획 과제와 다르며 공식 벤치 성적으로 보고하지 않는다.

## 완료 결과와 재검산

[4B·9B 결과](../../../docs/reports/2026-11-09-travel-worker-utility.md)에서 실제 과거 132선택을 고정하고 현재 230회씩 실행했다. 9B는 과거를 다시 생성하지 않았으며 두 작업자 합계 새 생성은 592회다. 학습 선택의 참조 정확성과 실제 득실을 구분한다. 원문 주석은 이 폴더, 실제 선택 이후의 성공·비용은 아래 결과 묶음에 있다.

```bash
python3 -m evaluation.export_travel_worker verify evaluation/results/travel-worker-utility-4b-20261109-v1
python3 -m evaluation.export_travel_worker verify evaluation/results/travel-worker-utility-9b-20261109-v1
```

표준 라이브러리만 사용한다. `pre-execution-lock.json`은 최초 실행 전 자료·설정·실행 코드, `evaluation-code-lock.json`은 최초 채점 전 주 채점 코드, `9b-transfer-lock.json`은 4B 결과를 본 뒤 9B 생성 전에 고정한 변경을 기록한다. 나중에 재사용 지원을 추가한 코드는 당시 `upload.zip` 사본과 구분한다.

## 입력 재생성과 새 실행

아래는 이미 공유된 결과를 읽는 데 필요하지 않은 재생성 절차다. 먼저 [여행 관계 자료 재현](../../memoryarena_travel/20261109-reference-v1/README.md)으로 `travel-reference-source-reproduced`를 만들고, 위 고정 commit의 식당 CSV를 별도 내려받는다. `--csv`에는 그 파일 경로를 넣는다. 원래 데이터 폴더를 덮어쓰지 않는다.

```bash
python3 -m data_pipeline.travel_worker_pilot \
  --source training/memory_selector/data/travel-reference-source-reproduced \
  --csv /path/to/clean_restaurant_2022.csv \
  --output training/memory_selector/data/travel-worker-source-reproduced
```

생성된 `manifest.json`의 자료·생성기 해시를 이 폴더와 대조한다. `protocol.json`의 원본 식당 CSV 해시도 같아야 한다. 이후 준비에는 [앞선 완료 학습](../../../docs/reports/2026-11-09-memoryarena-travel-references.md)의 **실제 upload/results와 CE best checkpoint**가 필요하다. Git에 가중치가 포함된 것으로 가정하지 않는다. 회수한 실행을 아래 경로에 놓았을 때:

```bash
python3 training/memory_selector/prepare_travel_worker.py \
  --source datasets/travel_worker_utility/20261109-pilot-v1 \
  --reader-run training/memory_selector/outputs/memoryarena-travel-training-20261109-a100 \
  --reference-source training/memory_selector/data/travel-reference-source-reproduced \
  --output training/memory_selector/data/travel-worker-reproduced
```

아래 명령은 **새 Colab GPU를 할당해 모델을 추론하고 사용량이 발생한다.** 저장 예측 검산과 다르다. 완료·실패 모두 자기 세션을 종료하고 실제 `stopped` 상태를 확인한다.

```bash
python3 training/memory_selector/run_colab.py --profile travel-worker \
  --data training/memory_selector/data/travel-worker-reproduced \
  --config training/memory_selector/configs/travel-worker-v1.json \
  --transport console --gpu A100 --max-runtime-seconds 2100 \
  --output training/memory_selector/outputs/travel-worker-new-run
```

9B 전이는 완료·종료된 4B 실행과 동일한 준비 자료로 `prepare_transfer`를 호출한다. 현재 작업 코드만으로 함수 호출을 준비할 수 있고 추가 라이브러리는 필요 없다.

```python
import sys
from pathlib import Path
sys.path.insert(0, "training/memory_selector")
from prepare_travel_worker import prepare_transfer
prepare_transfer(
    Path("training/memory_selector/data/travel-worker-reproduced"),
    Path("training/memory_selector/outputs/travel-worker-new-run"),
    Path("training/memory_selector/data/travel-worker-9b-reproduced"),
)
```

그다음 같은 `travel-worker` 실행에서 `--data`는 새 9B 자료, `--config`는 `training/memory_selector/configs/travel-worker-9b-v1.json`, `--output`은 다른 새 경로를 지정한다. 4B와 선택한 기억·현재 메시지가 모두 같아야 생성을 시작한다. 실제 실행에서는 호환성 dev 검사 36쌍과 입력 길이 상한도 확인했으며 절단하지 않았다.
