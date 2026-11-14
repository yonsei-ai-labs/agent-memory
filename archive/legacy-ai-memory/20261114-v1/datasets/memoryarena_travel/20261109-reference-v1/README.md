# MemoryArena 여행 참조 관계 — 자체 분할 v1

**참조 계획을 정확한 과거 기록으로 가정한 관계 판독 자료**다. 실제 작업자의 기억 생성·도구 행동·기억 제공 효과를 측정한 자료가 아니다. 공식 MemoryArena 전체 test 성적과 구분한다.

| 단위 | Train | Dev | Test |
| --- | ---: | ---: | ---: |
| 기본 계획 | 30 | 7 | 8 |
| 원래 연속 과제 묶음 | 180 | 42 | 48 |
| 명시적 참조 요청 | 4,288 | 940 | 1,153 |
| 요청–기억 후보 쌍 | 25,728 | 5,640 | 6,918 |

각 요청에 후보 6개, 주석된 참조 대상 1개가 있다. `reuse_reference`는 같은 선택을 참조하는 경우, `comparison_reference`는 가격·평점·유형 등의 비교 기준으로 참조하는 경우, `not_referenced`는 해당 사람·날짜·항목이 아닌 기록이다. 마지막 라벨은 실제 작업에 손해라는 뜻이 아니다. 다른 사람의 기록이 우연히 같은 호텔·식당 값을 담을 수도 있다.

원본 270개 묶음은 45개 기본 계획을 각각 6번 사용한다. 이름이나 질문 표현이 달라도 같은 기본 계획은 같은 분할에 둔다. 원래 TravelPlanner train의 계획과도 45개 모두 일치하므로 별도 독립 경험으로 더하지 않는다. 이 자료의 수만 건을 수만 개의 독립 여행 경험이라고 부르지 않는다.

이 분할은 새로운 기본 계획에 대한 검사다. 질문 생성 양식·어휘·여행 분야까지 달라지는 독립 평가가 아니다. 명시된 사람·날짜·항목을 찾는 과제이므로, 높은 학습 성적도 암묵적 기억 적용 조건이나 내용의 실제 득실을 배웠다는 증거로 확대하지 않는다.

학습 입력은 현재 여행자·여행 길이, 원문에서 추출한 참조 문장, 후보의 사람·날짜·항목·값이다. 전체 여행 요청 중 명시적 참조 문장만 다루며, 문장 추출까지 학습한 전체 선택기가 아니다. 기본 계획과 **현재 차례보다 앞선 공개 참조 계획**을 기억으로 쓴다. 현재·미래 계획 정답은 입력에 없다. 관계 평가 정답은 GPU 업로드에서 제외하지만, 이전 차례의 참조 계획은 이 통제 조건의 입력이라는 점을 구분한다.

주석은 보수적인 문법 규칙으로 생성했다. 이름·날짜·항목이 불명확한 문장은 제외하고 이유를 [summary](summary.json)에 남겼다. 자료를 만든 동일한 AI 에이전트가 train/dev의 50개 요청을 원문과 사례별로 대조했으며, 사람이나 독립 주석자의 검토는 없다. 기록의 `manually_reviewed`는 자동 해시 검사와 구분한 사례별 대조라는 뜻이며 사람 검수를 의미하지 않는다. [검토 기록](annotation-review.json)에는 같은 호텔 선택과 같은 방 규칙 참조의 구분, `day-6`를 현재 목표 날짜로 잘못 읽던 문제의 수정 확인이 있다. Test 내용을 읽어 주석 규칙을 고치지는 않았다.

[원문 연결 검사와 개발 기준선](data-checks-and-dev-baselines-deterministic.json)은 38,286개 입력의 출처·과거 시점·원문 구간을 재검산했다. 분할 간 정확히 같은 입력 쌍은 0개다. 주석 규칙을 다시 실행한 기준선은 구성상 100%이며, 이는 일관성 확인이다. 학습 모델이 단순 규칙보다 필요하다는 독립 증거가 아니다. [최초 검사](data-checks-and-dev-baselines.json)도 보존한다. 최종 평가 때 발견한 BM25 합산 순서에 따른 동점 차이는 [수정 기록](../../../evaluation/results/memoryarena-travel-references-20261109-v1/evaluation-amendment-bm25-order.json)에 남겼으며 원본·분할·주석·생성기 해시는 바뀌지 않았다.

공개 데이터는 [ZexueHe/memoryarena](https://huggingface.co/datasets/ZexueHe/memoryarena/tree/da1a37c8b19280e18627ca01cf368195a5e1d92e), 논문은 [He et al., MemoryArena (2026)](https://arxiv.org/abs/2602.16313)다. 데이터 카드의 CC BY 4.0 표기와 인용 안내를 따른다. 관련 원본은 [TravelPlanner](https://huggingface.co/datasets/osunlp/TravelPlanner/tree/8736504ecfc31b7f8b7e40122873c337e83fff7c)다.

## 재현과 학습 내보내기

큰 생성 파일은 Git에 복사하지 않고, [원본 명세](source-manifest.json)·[분할](split-plan.json)·[전체 파일 해시](artifact-index.json)를 공유한다. 저장소 루트에서 다음을 실행하면 약 6.2 MB의 공개 JSONL을 내려받고 동일한 생성 파일을 복원한다. 로컬 원본이 있으면 `--source`로 지정할 수 있다.

```bash
python3 -m data_pipeline.reproduce_travel_references \
  --output training/memory_selector/data/travel-reference-source-reproduced
python3 training/memory_selector/prepare_travel_references.py \
  --source training/memory_selector/data/travel-reference-source-reproduced \
  --output training/memory_selector/data/travel-references-reproduced
```

기존 출력 디렉터리를 덮어쓰지 않는다. 생성기나 원본·분할·주석이 바뀌면 해시 검사가 거부한다. 수정은 별도 버전으로 만든다.

GPU 없는 출처 검산과 개발 기준선:

```bash
python3 -m evaluation.travel_reference_data \
  --source training/memory_selector/data/travel-reference-source-reproduced \
  --raw training/memory_selector/data/downloads/memoryarena-travel-da1a37c.jsonl \
  --output /tmp/travel-reference-data-checks.json
```

기본값은 train/dev만 채점한다. Test 기준선은 개발 선택이 끝난 학습 실행을 `--completed-run`에 지정한 뒤 평가한다. [학습 전 설계](../../../docs/ai-memory-travel-selector-training-2026-11-08.md)와 [완료한 학습·평가](../../../docs/reports/2026-11-09-memoryarena-travel-references.md)를 함께 읽는다.
