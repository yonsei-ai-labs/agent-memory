# 같은 작업자 환경에서 재측정한 기억 득실

기존 12개 개발 가족·24요청·96상태에서 504개 상태–행동을 측정한 고정 버전임. 480개 고유 입력과 안정성 반복 4개를 같은 Qwen3.5-9B 프로세스에서 새로 생성함. 원래 333행과 추가 171행의 결과를 모두 재측정했으며 이전 실행 출력은 정답에 재사용하지 않았음.

408개 비어 있지 않은 후보의 학습 입력과 네 범주 정답을 별도 파일로 제공함. 개선 74·악화 23·둘 다 성공 139·둘 다 실패 172개임. 독립 경험 408개가 아니며, 새 독립 가족은 0개임. 원래 후보 부분집합은 237개임.

| 파일 | 역할 |
| --- | --- |
| `contract.json` | 모델·revision·라벨·입력·분할·해석 계약 |
| `examples.jsonl` | 모델에 보낼 목표·관측·기억 문자열과 묶음/가족 메타데이터 |
| `targets.jsonl` | 새 무기억 결과와 제공 결과로 만든 네 범주 정답 |
| `all-supervision.jsonl` | 무기억 96개 포함 전체 504배정의 실제 출력·다축 결과·비용 |
| `original-example-ids.json` | 원래 후보 237개 식별자; 확장 비교의 부분집합 |
| `folds.json` | 기존 3개 가족 분할; 독립 최종 test 아님 |
| `contrast-groups.json` | 57상태의 네 칸 대조와 실제 결과; 관계 비트는 효용 정답 아님 |
| `summary.json`, `by-condition.json` | 전체·조건별 후보 비교와 클래스 분포 |
| `policy-summary.json` | 기존 정책의 선택을 새 작업자 출력에 연결한 결과 |
| `old-vs-fresh.json`, `stability.json` | 이전 출력과의 차이·실행 내 네 반복 |
| `verification.json`, `manifest.json` | 검산 결과·원시 출력/코드/데이터 해시 |

모델에는 `examples.jsonl`의 `texts.goal_and_observations`만 입력함. `goal`은 기존 입력 ablation용이므로 전체 상태를 사용하는 주 비교 입력과 혼동하지 않음. 조건명·관계 비트·정답·사후 출력은 추가 입력으로 넣지 않음. `observation_id`는 새 측정 버전을 포함하고 `generation_id`는 메시지의 동일성을 나타냄. 같은 메시지가 여러 배정에 연결되어도 독립 실행으로 세지 않음.

`class_id` 순서는 0 둘 다 실패, 1 개선, 2 악화, 3 둘 다 성공임. 작업 성공을 기준으로 한 관측 라벨이며 근거 준수·선택 비용과 별도임. 악화 23묶음 중 무기억도 근거를 준수해 성공했던 경우는 7묶음·2상태·2가족임. 무기억의 근거 없는 정답 일치가 깨진 나머지 사례와 구분해서 보고함. 미측정 행동은 음성으로 채우지 않음.

기존 `prepare_memory_contrast_learning --observations`는 이전 측정 라벨에 추가 결과를 합치는 경로이므로 이 자료의 내보내기에 사용하지 않음. 여기서는 같은 실행에서 얻은 전체 라벨과 `build_rows`만 사용함. 이후 `prepare_memory_refreshed_learning`을 통해 새 측정 계약을 검증하는 학습 내보내기를 연결했음.

비어 있지 않은 후보끼리 실제 성공이 다른 상태가 7→28개, 비교쌍이 16→177개로 늘었음. 관측 후보의 최선 성공 합계는 여전히 58/96임. 자료 생성 뒤 [24회 선택기 학습과 검산](../../../docs/reports/2026-11-10-memory-refreshed-learning.md)을 완료했음. 확장 후보의 선택 성공은 40–43/96으로, 검색 상위 2개·참조 규칙 55/96보다 낮았음. 고정 `contract.json`의 학습 미실행 표기는 **자료 생성 당시** 상태이며 덮어쓰지 않음. 실제 진행/실패 이력·새 출처 일반화·MemoryArena 전체 종단 평가는 포함하지 않음. [현재 데이터 리뷰](../../../docs/reports/2026-11-10-memory-training-data-review.md)에 규모·내용 편중·라벨 한계와 보완 순서를 정리함.

저장소 루트에서 [공유 결과 안내](../../../evaluation/results/memory-worker-refresh-20261110-v1/README.md)의 CPU 환경을 준비한 뒤 검산함.

```bash
python -m data_pipeline.export_memory_worker_refresh check \
  --path evaluation/results/memory-worker-refresh-20261110-v1
```

이 검사는 원시 저장 출력과 tokenizer에서 504라벨을 다시 구성함. GPU나 원래 로컬 `runs/`가 필요하지 않음. 모델 가중치 순전파를 다시 실행하는 검사는 아님. 파일을 수정해 재생성하지 말고 새 측정·새 계약은 별도 버전으로 보존함. [결과 보고서](../../../docs/reports/2026-11-10-memory-worker-refresh.md)에 사례·편법·남은 약점·다음 비교를 정리함.
