# 전체 질문과 과거 기억 은행: 명시적 관계 검색

2026-11-09에 동결한 MemoryArena 여행 참조 이력의 파생 자료다. 질문 전체에서 가리키는 여러 과거 기록을 찾는 과제이며, 실제 기억의 득실 라벨은 없다.

| 구분 | 기본 계획 | 전체 질문 | 정답 제거 진단 포함 |
| --- | ---: | ---: | ---: |
| train | 30 | 813 | 1,626 |
| dev | 7 | 189 | 378 |
| test | 8 | 219 | 438 |

기억 은행은 원래 질문당 26–336개다. 전체 47,573개 기억 ID는 행별 공개 참조 기록이며 독립 경험 47,573개를 뜻하지 않는다. 원래 요청과 시점 이전 기록을 보존했다. 이전 6후보 실험과 같은 기본 계획 분할이므로 test는 새 출처 확인 집합이 아니다.

`queries.jsonl`에는 원래 질문과 후보 ID를, `memories.jsonl`에는 과거 공개 계획의 슬롯 기록을 둔다. `supervision.jsonl`은 별도 정답이다. `provenance.jsonl`에 원문 참조 위치·대상 사람/날짜/항목이 있다. `split-plan.json`은 동일한 기본 계획을 묶는다.

`data-audit.json`은 원시 자료로부터 입력과 이력을 전수 재구성한 결과다. `annotation-review.json`은 train/dev 18질문·75참조 표본 검토이며 독립 이중 주석은 아니다. `artifact-index.json`의 `frozen_before_training` 상태가 생성 당시 draft 상태를 기록한 `summary.json`에 우선한다. 동결 파일을 덮어쓰지 않는다.

원래 질문에 대한 정답 기록을 모두 제거한 `--missing` 행은 보류 진단용이다. 학습 내보내기는 이 행을 모두 제외한다. 원본과 제거본을 동시에 제공하면 두 후보 집합의 차이에서 정답이 드러나기 때문이다. test 라벨도 내보내지 않는다.

원시 자료는 [ZexueHe/memoryarena](https://huggingface.co/datasets/ZexueHe/memoryarena/tree/da1a37c8b19280e18627ca01cf368195a5e1d92e)의 `group_travel_planner/data.jsonl`이다. He et al.의 [MemoryArena](https://arxiv.org/abs/2602.16313) 및 원본 TravelPlanner를 출처로 표기하며 데이터 카드의 CC BY 4.0 안내를 따른다. 원문 SHA와 범위 제한은 `contract.json`에 있다. 공개 참조 계획을 사용했으므로 실제 에이전트가 생성한 경험이나 공식 MemoryArena 점수와 구분한다. 명시적 참조 주석 규칙은 구성상 완전한 기준선이며 학습 모델의 필요성을 입증하지 않는다.

큰 JSONL은 원본 명세·생성기·고정 해시로 공유한다. 아래 첫 명령은 공개 원본을 내려받아 입력·정답·근거를 재생성하고 모든 해시를 대조한다. 기존 파일은 덮어쓰지 않는다. 원본이 로컬에 있으면 `--source <원본>`을 지정한다.

```bash
python3 -m data_pipeline.reproduce_travel_retrieval \
  --output /tmp/travel-retrieval-source
python3 training/memory_selector/prepare_travel_retrieval.py \
  --source /tmp/travel-retrieval-source \
  --output /tmp/travel-retrieval-export
python3 -m unittest training.memory_selector.tests.test_travel_retrieval -q
```

실험 조건은 [동결 계획](../../../docs/ai-memory-full-request-retrieval-2026-11-09.md)에 있다. GPU 실행은 비용을 발생시키므로 내보내기·검사와 분리되어 있다.

완료 결과와 검산은 [보고서](../../../docs/reports/2026-11-09-full-request-memory-retrieval.md), GPU 없는 결과 재계산은 [공유 예측](../../../evaluation/results/travel-full-request-retrieval-20261109-v1/README.md)을 따른다.

새 학습을 실행하려면 Colab CLI 인증 후 다음을 사용한다. 이 명령은 전용 A100을 새로 할당하며, 완료·실패 뒤 해당 세션을 종료한다. 기존 결과 검산에는 필요하지 않다.

```bash
python3 training/memory_selector/run_colab.py --profile travel-retrieval \
  --data /tmp/travel-retrieval-export \
  --gpu A100 --transport console --max-runtime-seconds 3900 \
  --output training/memory_selector/outputs/travel-retrieval-new-run
```
