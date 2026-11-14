# 형식 노출과 관계 대조의 학습 자료

기존 [부분 관측 자료](../../memory_partial_records/20261111-v1/README.md)의 train 출처와 원문을 재사용하는 원인 분해 실험임. 새 원문 수집·새 독립 평가 출처를 추가하지 않음. [학습 전 고정 설계](../../../docs/design-data/memory-augmentation-ablation-20261112-v1/README.md)를 따름.

| 학습 조건 | 항목 | 고유 선택기 입력 | 출처 그룹 | 후보 쌍 | 양성 쌍 |
| --- | ---: | ---: | ---: | ---: | ---: |
| repeat | 400 | 235 | 30 | 876 | 330 |
| format | 400 | 395 | 30 | 716 | 310 |
| relation | 400 | 395 | 30 | 716 | 310 |

세 조건의 공통 자료는 원래 20출처의 atomic 160상태와 이전 학습 anchor 80상태임. 나머지 160상태에서 repeat는 원래 atomic을 반복하고, format과 relation은 동일한 compound 기억을 사용함.

Format/relation은 행마다 목표·기억·원문·후보별 정답이 같고, 일부 관측 상태 80행에서 관측 내용이 달라짐. Format에서는 현재 관측을 제거하여 필요한 기록이 있는 무관측 상태로 만들며, relation은 일부 사실을 이미 알고 있어도 다른 정보가 필요할 수 있는 원래 상태를 유지함. 두 조건 모두 atomic 공통 자료에서 부분 관측을 학습함. 양성/음성 수와 원래 손실 가중치가 정확히 같음을 감사함.

Format에서 반복될 수 있는 무관측 상태는 의미가 같은 요청 문장 네 개로 구분하며 relation에도 같은 문장을 사용함. 문장 틀별 양성/음성 수는 각각 25/15로 같음. 고유 입력은 실제 모델에 전달되는 문자열을 기준으로 세며 ID 차이를 새 입력으로 세지 않음. 관측의 의미를 바꾸므로 입력 토큰 수까지 정확히 같다고 주장하지 않음.

`dev-*`와 `test-*`는 원래 파일을 바이트 그대로 복사함. 학습과 평가 출처는 겹치지 않지만 원래 test의 결과를 이미 확인했으므로 이번에는 기존 평가를 이용한 진단으로 해석함. Test 384상태의 독립 출처는 12개임. 실제 효용 주석 `utility`는 계속 `null`이며, 작업자 출력으로 관계 라벨을 바꾸지 않음.

```bash
.venv/bin/python -B -m data_pipeline.augmentation_ablation_data audit
.venv/bin/python -B -m unittest tests.test_augmentation_ablation tests.test_partial_records
```

`manifest.json`은 학습 입력·라벨·프로토콜·부모 자료 해시를 고정함. 자료의 원문 구절과 출처 예약은 부모 자료에 연결되어 있음. 학습 중 원본 자료와 가중치를 수정하지 않음.
