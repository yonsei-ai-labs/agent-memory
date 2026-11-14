# 관계 자료 인수 계약 v1

구현은 [memory_data_intake.py](../../../data_pipeline/memory_data_intake.py)임. 학습·실행 전 관계 자료의 기계적 조건을 확인하는 계약이며, 자연어 의미나 실제 기억의 효과를 인증하는 것은 아님.

## 패킷 구조

JSON 한 개에 다음 배열을 둠. 외부 제작본이 JSONL 여러 파일이면 필드를 보존하는 adapter로 결합할 수 있음. 원본 자체를 덮어쓰지 않음.

| 필드 | 주요 항목 |
| --- | --- |
| `sources` | `source_id`, 증강 전 `root_family_id`, `split`, `new_to_project`, `prior_use` |
| `events` | `event_id`, `source_id`, `scenario_id`, 사건 단계 `step`, 실제 입력에서 인용할 `public_text` |
| `cases` | `state_id`, `source_id`, 판단 시점 `decision_step`, 후보 범위 `candidate_scope`, `inputs` |
| `inputs` | 후보별 `memory_id`, 완성된 판독 문자열 `text`, UTF-8 SHA256 `sha256` |
| `relations` | `state_id`, `memory_id`, `judgment`, 근거 위치 배열 `evidence`, `utility: null` |

최상위 `version`은 `memory-relation-intake-v1`, `stage`는 `pre_execution`임. `split`은 `train/dev/evaluation` 중 하나임. `judgment`는 `needed/not_needed/insufficient`이며 정보 부족에는 `missing_premises`도 명시함.

각 근거는 `{event_id, start, end}`임. `text[start:end]`가 해당 사건의 `public_text`와 정확히 같아야 함. 현재 원형의 오프셋 단위는 Python 문자열의 문자 인덱스이며 UTF-8 바이트나 토큰 인덱스가 아님.

사건의 `scenario_id`는 해당 `state_id` 또는 여러 변형이 공유하는 과거인 `*`임. 같은 시각이라도 다른 가정의 상태에만 속한 사건을 가져오지 않음. 판단 시점 뒤의 사건을 정답 근거로 사용하지 않음.

현재 v1은 한 상태의 근거가 같은 `source_id`에 속하는 패킷을 검사함. 여러 원천을 연결하는 자료는 원천 연결과 분할을 먼저 정의한 adapter가 필요함. 같은 뿌리 가족을 이름만 바꿔 다른 분할로 보내는 경우를 거부함.

## 검사 범위

- ID 중복·후보/관계 행 누락·판독 문자열 해시.
- 원 가족의 분할 중복과 같은 실제 입력의 분할 중복·상충 판정.
- 선언한 근거의 원천·시나리오·시간·문자 위치가 실제 판독 입력에 대응하는지.
- 이미 사용한 원천이라는 선언과 `new_to_project`의 충돌.
- 작업자 실행 전 실제 효용을 `null`로 유지하는지.

과거 사용 이력의 진실성은 별도 [원천 목록](../../reports/data/memory-data-intake-20261112-v1/source-inventory.jsonl)과 대조해야 함. 패킷에 선언하지 않은 과거 사용 이력을 이 도구가 자동으로 발견하지는 않음. 선언한 근거 외에 필요한 전제가 모두 있는지, 간접 서술의 의미가 대본과 일치하는지, 정답 정보가 공개 문장에 부적절하게 끼었는지는 별도 내용 검토 대상임.

`input_scope`는 현재 `rendered_pair_text_before_model_tokenization`임. 따라서 토크나이저·길이 절단 이후에도 근거가 남았는지는 모델 연결 시 다시 검사해야 함. 입력에서 대본의 정답 필드를 제외하는 renderer도 별도로 확인해야 함.

## 사용

```bash
.venv/bin/python -B -m data_pipeline.memory_data_intake \
  --check-current-augmentation

.venv/bin/python -B -m data_pipeline.memory_data_intake \
  --packet docs/design-data/memory-data-intake-20261112-v1/example-packet.json
```

결과는 `mechanical_checks_passed`이며 학습 승인이나 독립 인간 검토 통과가 아님. 기존 증강 자료에는 임시 adapter를 적용하여 검사하고 원 데이터는 수정하지 않음. 예제 패킷은 스키마 설명용이며 실제 수집 사례로 세지 않음.
