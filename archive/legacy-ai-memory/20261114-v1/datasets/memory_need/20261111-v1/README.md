# 현재 필요한 기억의 상태 대조 자료

[TravelPlanner 요청·MemoryArena 카탈로그 명세](../../travel_state_transfer/20261109-v1/README.md)의 미사용 train 24가족에서 72개 과거 계획을 실제 Qwen3.5-9B로 생성함. 원문 응답을 보존하고 항목 수 오류 등을 정답으로 복구하지 않음. 유효하고 서로 다른 저녁·숙소 기록을 만들지 못한 train 1가족은 제외하고 대체하지 않음. 최종 23가족을 학습 11·개발 6·확인 6으로 사용함. 기존 원천 dev 20·test 40은 사용하지 않음.

- 138상태: 학습 66·개발 36·확인 36. 46개의 상태 쌍과 46개의 이미 충분한 상태를 포함함.
- 원천 선택 1,080항목 중 유효 카탈로그 ID 993개. 원래 요청 조건 충족 710개, 최저가·동률 규칙까지 충족 442개임. 과거 요청 성공과 실제로 기록한 선택 사실은 구별함.
- 필요성 관계 6,102행 중 양성 138개임. 관련 있지만 비활성 분기인 기록, 활성 분기이지만 이미 관측된 기록이 각각 138개임. 파생 행을 독립 경험 수로 세지 않음.
- `cases.jsonl`은 목표·공개 상태·기억 은행임. `labels-{train,dev,confirmation}.jsonl`은 필요 기록·현재 작업 정답을 별도 저장함. 학습기는 확인 정답을 읽지 않음.
- `record-relations.jsonl`에는 관계·관측 여부·필요성과 원천 위치가 있음. 실제 득실은 아직 측정 전이므로 null임. 후속 측정 결과로 이 파일을 덮어쓰지 않음.
- `source-results.zip`은 실제 과거 생성 출력·tokenizer·실행 설정·원천 가중치 provenance임. `source-record-audit.jsonl`은 원래 요청 조건과 사실의 유효성을 따로 검토한 결과임.

현재 관측과 실패 상태는 통제 개입임. 두 문장 틀과 카탈로그를 공유하는 자료로, 자연 에이전트 실패 로그나 다른 도메인의 평가 자료가 아님. 카탈로그는 원천의 모의 자료이며 실제 여행 정보로 해석하지 않음. 공개 상태 규칙이 구조상 필요 기록을 고를 수 있으므로 신경망 필요성을 입증하는 시험대도 아님.

```bash
python3 -m evaluation.memory_need_data_quality \
  --source datasets/memory_need_sources/20261111-v1 \
  --data datasets/memory_need/20261111-v1 \
  --output /tmp/memory-need-quality.json
```

학습·작업 평가 계획은 [실험 노트](../../../docs/reports/2026-11-11-memory-need-learning.md)를 참조함. README는 설명 파일이며 생성 데이터 해시는 `manifest.json`으로 고정함.
