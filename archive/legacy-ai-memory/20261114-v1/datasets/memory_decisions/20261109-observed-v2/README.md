# 현재 상태와 관측된 기억 제공 결과 v2

기존 travel 후속 실행을 선택기 학습·진단에 사용할 수 있도록 다시 구성함. **새 자연 경험·새 모델 실행·독립 평가 자료는 추가하지 않았음.** 12개 기존 train family의 96개 상태와 333개 관측된 기억 전달 묶음임. 정책 이름이 달라도 입력이 같은 실행은 합쳤으며, 고유 작업자 입력은 309개임.

전체 연구 방향은 어떤 기억이 어떤 상황에서 득·실을 만드는지 측정하고 설명·예측하는 것임. 이 자료와 `contract.json`의 질문은 그중 작업자·기억 저장소·컨텍스트 상한을 고정하고 상태를 고려한 선택을 비교하는 실험 방향을 나타냄. 전체 연구의 핵심 질문을 확정하는 선언은 아님. 현재 자료에는 목표와 도구 관측이 있으나, 순차적 진행 기록과 실제 실패 이력은 없음. 이 자료만으로 실패 이력 활용이나 여러 단계 에이전트의 최종 성공 향상을 주장할 수 없음.

| 파일 | 용도 |
| --- | --- |
| `state-inputs.jsonl` | 선택 시점의 현재 요청·관측, 사용 가능한 기억 ID |
| `memories.jsonl` | 실제 과거 생성 기록 540개. 과거 가격 등이 있는 제공용 근거 문장 |
| `supervision.jsonl` | 실제 전달한 기억의 순서, 작업자 실행 ID, 성공·오류·비용, 무기억 대비 관측 차이 |
| `provenance.jsonl` | 원본 사례·조건·출처 해시. 모델 입력으로 렌더링하지 않음 |
| `exploratory-family-folds.json` | 동일 family의 모든 상태·행동을 함께 남기는 12개 탐색용 분할 |
| `contract.json` | 고정 작업자·예산·입력 범위·연구 질문·평가 한계 |
| `manifest.json` | 파일·원본·변환 코드 해시 |

`data_pipeline.memory_decision_dataset.render_selection`과 `render_bundle`로 입력을 구성함. 렌더러는 현재 관측과 기억 내용만 사용하며, 성공·정답·조건 이름·정책 이름은 전달하지 않음. 이 입력을 기존에 학습한 모델이 이미 사용했다는 의미는 아님. **이번 회차에는 재학습하지 않았음.**

`false_infeasibility`는 공통 판정 근거에서 가능한 선택지가 있는데 NONE이라고 응답한 경우임. `unsupported_infeasibility`는 필요한 과거 근거가 제공되지 않았는데 NONE이라고 응답한 경우임. 둘은 다른 축임. 실제 가능성을 모르는 NONE 83개는 전자의 `null`로 유지함. 미시도 기억 묶음도 미측정으로 두며, 0점으로 채우거나 구성 기억 각각에 묶음 효과를 복사하지 않음.

작업자는 원래의 Qwen3.5-9B와 프롬프트를 고정함. 기억 전달 상한은 그 작업자의 저장된 토크나이저로 512토큰이며, 실제 최댓값은 254임. 동일 예산은 **공통 상한**이지 부족한 내용을 padding으로 채우는 규칙이 아님. 전체 기억 정책은 이 상한을 초과하므로 제외함. 선택기 계산 비용은 기존 자료에 없어 `null`이며 0으로 취급하지 않음.

같은 상태에서 여러 행동을 실행한 자료이므로 333행을 독립 사례처럼 무작위 분할하면 안 됨. 관측된 행동의 성공 168/333은 정책 비교 점수가 아님. 행동 수와 선택 방식이 다르고 같은 실행이 여러 상태 표현과 연결될 수 있음. 정책 평가는 상태별 한 행동과 고정 작업자 실행을 연결해야 하며, 새 선택이 기존 관측 묶음에 없으면 별도 실행이 필요함.

저장된 토크나이저를 읽을 수 있는 프로젝트 Python 환경에서 재생성함. 모델 로딩·다운로드·GPU·유료 호출은 발생하지 않음.

```bash
.venv/bin/python -m data_pipeline.memory_decision_dataset --output /tmp/memory-decisions-v2-new
.venv/bin/python -m unittest evaluation.tests.test_memory_outcome_labels evaluation.tests.test_memory_remediation -v
```

원본: [후속 실행 묶음](../../../evaluation/results/travel-policy-followup-20261109-v1/README.md). [보완 보고서](../../../docs/reports/2026-11-09-data-remediation-implementation.md)에 완료한 내용과 남은 검증을 정리함.
