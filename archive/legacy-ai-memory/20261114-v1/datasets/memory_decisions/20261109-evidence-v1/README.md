# 근거·실제 득실 분리용 보조 자료

기존 [관측 결과 v2](../20261109-observed-v2/README.md)를 보존하면서, 선택 직전 근거와 이후 작업자 결과를 별도로 평가하도록 추가한 자료임. **기존 12계열·96상태·333관측 묶음의 재구성이며 새 출처나 학습 성과가 아님.**

| 파일 | 의미 |
| --- | --- |
| `selector-inputs.jsonl` | 원래 현재 문맥을 목표와 현재 관측으로 나눔. 후보 순서·허용 은행은 원본과 같음 |
| `state-reference-locations.jsonl` | 지정 과거 기록이 현재 관측·후보·은행 중 어디에 있는지 확인한 진단용 메타데이터 |
| `bundle-reference-coverage.jsonl` | 실제 관측 묶음 333개의 지정 기록 확보·새로 추가·현재 중복을 계산함. `semantic_sufficiency`는 모두 `null`임 |
| `evidence-outcome-summary.json` | 근거 지표를 만든 뒤 기존 성공 결과와 결합한 정책별 평가. 입력·근거 주석에 사용하지 않음 |
| `summary.json` | 규모·기록 위치·후보 개수 편법 검사 |
| `contract.json`, `manifest.json` | 입력 범위·한계·원본과 생성 코드 해시 |

지정 ID를 전달했다고 의미적으로 충분한 것은 아니며, 지정 ID가 없다고 대체 근거까지 없는 것도 아님. `null`은 미검토를 뜻함. 현재 중복은 실제 무익/유해 라벨이 아님. 실제 득실은 원본 `supervision.jsonl`에서 상태 ID와 **순서 있는** 전달 ID 목록으로 별도 연결함. 실행하지 않은 묶음은 `None`으로 유지함.

## 입력 비교

`data_pipeline.memory_evidence_review.render_selector`의 `goal`과 `goal_and_observations`는 선택기가 현재 관측을 보는지만 바꿈. 모델 입력에는 정답 요구 목록·근거 위치·충분성·작업 결과·조건 이름을 넣지 않음. 후보 기억과 순서는 같음. `memory_evidence_dataset.worker_messages`는 두 비교 모두에서 기존 작업자의 목표·현재 관측을 그대로 재현함.

현재 입력은 정적인 목표·관측임. 실제 진행·실패 이력을 새로 만든 것이 아님. 새로운 원천을 편입하려면 원천별 선택 시점·공개 관측·도구 계약·분할을 확인하는 어댑터가 추가로 필요함. 현재 검사는 여행 자료의 같은 출처 계열과 과거 round 조건을 적용하며 임의의 공유 은행 수집기로 사용하지 않음.

## 발견한 편법과 사용 범위

필요한 두 기록을 삭제한 상태의 후보 수는 25·43·61개, 나머지는 27·45·63개임. `후보 수 % 3 == 0`으로 지정 기록의 은행/현재 관측 존재 여부를 **96/96** 구별함. 자료를 본 뒤 발견한 설계 진단이며 모델 성능이나 의미적 충분성의 정확도가 아님.

따라서 이 자료에서 높은 충분성 예측 점수가 나와도 관계 학습으로 해석할 수 없음. 새 자료는 삭제 대신 같은 수의 검토된 비지지 기록으로 교체하는 대조와 토큰 길이 통제를 별도로 구성하고, 대체 근거가 남았는지 다시 검토해야 함. 공통 전달 상한 512토큰을 적용하는 정책 비교와 이 편법 통제는 다른 목적임. 원본 상태·과거 실행 결과는 덮어쓰지 않음.

새 확인 자료 확보 전에는 재현·입력 분리·오류 진단에 사용함. 두 개의 독립 출력 성분을 함께 채우는 현재 과제를 한 답에 여러 기억을 결합해야 하는 추론 과제로 부르지 않음.

## 재현

저장소 루트에서 Python 표준 라이브러리만으로 검사·재생성 가능함. 새 출력 경로를 사용함.

```bash
python3 -m data_pipeline.memory_evidence_dataset check --path datasets/memory_decisions/20261109-evidence-v1
python3 -m data_pipeline.memory_evidence_dataset export --path /tmp/memory-evidence-new
python3 -m unittest evaluation.tests.test_memory_evidence -v
```

원문 span을 읽고 작성한 [임시 검토 8건과 새 출처 검토 규칙](../../../docs/design-data/memory-evidence-review-20261109-v1/README.md)은 자동 지표와 분리함. 독립 검수·확정 학습 정답은 아직 없음. 다음 순서는 [연구 계획](../../../docs/ai-memory-next-steps-feasibility-2026-11-09.md)에 정리함.
