# 원문 기반 부분 관측·복합 기록 대조

같은 두 사실을 두 기록으로 나누거나 하나로 합치고, 현재 요청과 이미 확인한 사실에 따라 제공 필요성을 주석한 자료임. 이전 모델이 한 정보만 확인되어도 복합 기록 전체를 보류하는 오류를 보강하기 위한 통제 실험 자료임.

| 구간 | 상태 항목 | 출처 그룹 | 후보 쌍 | 제공 필요 쌍 |
|---|---:|---:|---:|---:|
| control 학습 | 400 | 30 | 876 | 330 |
| augmented 학습 | 400 | 30 | 716 | 310 |
| 개발 | 96 | 6 | 144 | 66 |
| 평가 | 384 | 12 | 576 | 264 |

학습 두 조건은 현재 요청·관측·출처와 업데이트 수가 같음. control은 20개 출처의 8개 상태에서 atomic 기록을 두 번 제시하고, augmented는 한 번을 compound 기록으로 바꿈. 양쪽에 같은 기존 train 상태 80개를 추가함. 이 때문에 항목 수와 고유 입력 수는 다름(control 235, augmented 395). 복제 항목을 새 경험으로 세지 않음.

새 출처는 train 12·dev 6·test 12개이며, 나머지 학습 원문은 이전 train 출처임. test 384상태는 12개 출처의 8개 요청·관측 조건 × 2개 기록 구성 × 2개 관측 표현임. Prose 관측 표현은 학습과 개발에 없음.

- `*-cases.jsonl`: 모델 입력. 요청·관측·후보 본문만 선택기 입력에 사용함. 출처 ID·정답·필드 커버리지 등의 주석은 입력하지 않음.
- `*-labels.jsonl`: 현재 필요한 정보, 관측된 정보, 원문별 정보 범위, 최소 충분 집합, 정답 필드. `utility`는 비어 있음. 관계 라벨을 실제 작업 득실로 바꾸지 않음.
- `source-specs.json`: 검토한 38개 원문과 76개 구절, 역할, 정답, 원문 위치. Compound는 두 원문 구절의 통제된 결합이며 자연스러운 원문 문장 그대로라고 주장하지 않음.
- `source-reservation.json`: 주석 전 출처 분할과 제외 집합, 원본 다운로드 주소·SHA256. 원래 혼합 세션의 모든 배경 출처를 포함한 연결 그룹으로 분리함.
- `protocol.json`, `manifest.json`: 추론 전 고정한 설계와 자료 해시.

원자료는 [Mem2ActBench의 고정 revision](https://github.com/Cantaloupe-M/Mem2ActBench/tree/c935c65be9aa19c930b60657c0c4b2b7ea13a34a)의 공개 대화 자료임. 이 자료는 새 자연 에이전트 궤적이 아니며, 현재 관측은 통제하여 작성함. 근거 읽기와 주석은 구축 에이전트가 수행했으며 독립 인간 이중 검토가 아님. 모든 후보 은행이 512토큰에 들어가므로 실제 검색 경쟁을 검증하는 자료로 사용하지 않음.

[상세 설계](../../../docs/design-data/memory-partial-records-20261111-v1/README.md)를 참고함. 검사:

```bash
.venv/bin/python -m data_pipeline.partial_record_data audit
.venv/bin/python -m unittest tests.test_partial_records
```
