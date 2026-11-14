# 현재 상태와 과거 기억의 관계 증강

[설계·구성 결과](../../../docs/reports/2026-11-12-memory-state-augmentation.md) · [생성·검증 코드](../../../data_pipeline/augment_memory_states.py)

기존 `memory_need/20261111-v1`의 **학습용 11가족만** 사용한 관계 학습 자료임. 과거 기록 477개는 원문 그대로 보존하고 현재 상태·관측·표현을 통제하여 바꿈. 새 독립 출처·실제 작업자 득실·모델 학습 결과는 추가되지 않음.

| 구성 | 수 |
| --- | ---: |
| 기존 학습 출처 | 11가족 |
| 현재 상태 | 1,056 = 11 × 기본/대안 분기 2 × 확인 사실 부분집합 16 × 표현 3 |
| 실제 고유 학습 입력 | 4,224쌍: 상태마다 목표에 언급된 기본·대안 기록 4개 |
| 전체 은행 후보 관계 | 45,792쌍 |
| 기록이 필요한 상태 / 불필요한 상태 | 792 / 264 |
| 실제 득실 라벨 | 0. `utility`, `actual_gain_or_harm`은 모두 `null` |
| 독립 검토 대기 사례 | 24개, 11출처 포함 |

`needed_now`는 해당 과거 사실을 현재 답에 보충해야 한다는 관계 정답임. 실제 제공 이득을 뜻하지 않음. `inactive_branch`도 현재 선택해야 할 분기가 아니라는 뜻이며 과거 사실이 거짓이거나 기억 제공이 반드시 해롭다는 라벨이 아님.

## 파일

| 파일 | 내용 |
| --- | --- |
| `banks.jsonl` | 11개 은행. 원래 기록과 기본·대안의 기록 ID, 원 카탈로그를 정규화하여 보존 |
| `cases.jsonl` | 목표·현재 상태·확인한 사실. 은행은 `bank_id`로 연결 |
| `labels.jsonl` | 필요한 기록, 활성/비활성 참조, 후보별 관계 역할, 실제 득실 미측정 표시 |
| `training-pairs.jsonl` | 핵심 4참조의 감독 4,224쌍. 기존 상태 판독기의 실제 pair 입력 해시 포함 |
| `contrasts.jsonl` | 상태 변경 528, 관측 추가 2,112, 동등 표현 704쌍 |
| `review-packet.jsonl` | 목표·현재 관측·핵심 후보만 있는 검토용 24사례. 정답·분기 코드·원 state ID 제외 |
| `review-key.jsonl` | 위 검토표의 원 상태와 규칙 정답. 독립 판단 후 대조하는 파일 |
| `protocol.json`, `manifest.json`, `audit.json` | 범위·원자료/코드/생성 자료 해시·검산 결과 |

은행 전체를 각 상태마다 복제하지 않음. 기존 판독기 입력에 필요한 경우 다음처럼 결합함.

```python
from data_pipeline.augment_memory_states import materialized_cases, pair_text

cases = materialized_cases()
example_input = pair_text(cases[0], cases[0]["bank"][0])
```

검증한 pair 형식은 `memory_need_learning.pair_text(..., 'state')`임. 최신 다른 실험의 trainer나 Colab profile에 자동으로 연결한 것은 아님. 다른 checkpoint의 입력 형식으로 학습할 때는 그 형식에서 고유 입력·누출·길이를 다시 확인해야 함.

## 생성과 검사

저장소 루트의 Python 환경으로 실행함. CPU만 사용하며 모델·토크나이저·네트워크는 필요하지 않음.

```bash
.venv/bin/python -B -m data_pipeline.augment_memory_states audit

.venv/bin/python -B -m data_pipeline.augment_memory_states build \
  --data /tmp/ai-memory-state-augmentation-rebuild

.venv/bin/python -B -m unittest tests.test_memory_state_augmentation
```

생성 대상 폴더가 이미 있으면 덮어쓰지 않고 종료함. 검사에서 원 기록 변경, 비학습 출처, 공개 관측과 메타데이터 불일치, 같은 모델 입력의 상충 라벨, 표현 변경 시 정답 변화, 실제 효용 라벨 복사를 거부함.

## 해석과 사용 범위

- 1,056상태와 4,224쌍은 **11개 기존 출처의 변형**임. 독립 경험이 그만큼 늘어난 것이 아님.
- 다른 실패 사건이 있는 로그를 포함하고 목표의 기본·대안 절 순서를 바꿈. 실패 단어의 유무나 첫 번째 참조만으로 판정하는 편법을 줄이는 대조임.
- 은행 순서만 바꾸면 후보별 pair encoder 입력이 같으므로 별도 학습 표본으로 증식하지 않음.
- 전체 은행에서 항상 불필요라고 답해도 97.69%, 핵심 참조에서도 75% 정확도임. 필요 근거 확보·불필요 제공·정확한 집합·같은 기억의 상태 대조를 함께 평가해야 함.
- 자료는 규칙으로 생성되어 공개 규칙이 정답을 재현함. 신경망 선택기의 필요성이나 실제 성공 향상의 증거가 아님.
- 기존 평가 자료는 그대로 두며 봉인된 `new-b` 자료를 읽거나 증강하지 않음. 사람 검토와 새 작업자 득실 측정은 아직 없음.
