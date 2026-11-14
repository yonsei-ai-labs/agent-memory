# 동일 입력의 선택기 회귀 평가

[실험 계획과 결과](../../../docs/reports/2026-11-11-memory-selector-regression.md).

기존 여행 219질문·8기본 계획과 상태 선택 확인 36상태·6가족을 사용함. 상태 자료의 원문·일반 문장·무관한 조회 실패/성공 문장은 같은 사실의 세 표현이며 독립 경험을 늘린 자료가 아님. 새 자연 발생 출처나 원천 dev/test는 사용하지 않음.

`cases.jsonl`은 모델 입력·동일 후보·상한, `labels.jsonl`은 별도 평가 정답, `rules.json`은 공개 텍스트 규칙의 선택, `models.json`은 기존/목표/상태 checkpoint 다섯 개와 고정 해시임. 신경망 추론은 정답과 규칙을 파싱하거나 선택에 사용하지 않음. 무결성 해시는 이 파일들도 검사하므로 실행 기록의 `labels_read=false`는 정답 내용을 이용하지 않았다는 뜻임. `protocol.json`은 비교·지표·예산을 실행 전에 고정함.

모든 모델이 동일한 현재 상태를 읽음. 기존 여행에는 참조 지시문과 필요성 지시문을 각각 적용하고 두 결과를 유지함. 규칙은 후보 안의 기억만 선택하며 자료의 문장 틀을 알고 있음. 주석 규칙과 독립된 범용 모델의 성능 상한으로 해석하지 않음.

필요한 원본과 기존 모델이 있는 로컬 환경에서 재구성:

```bash
python3 -m data_pipeline.prepare_memory_selector_regression --output /tmp/memory-selector-regression-data
python3 -m unittest evaluation.tests.test_memory_selector_regression -v
```

실제 추론은 별도 GPU 작업이며 추가 학습은 없음:

```bash
python3 -m evaluation.run_memory_selector_regression --data datasets/memory_selector_regression/20261111-v1 --output /tmp/memory-selector-regression-inference
```

checkpoint와 base 모델의 위치는 `models.json` 및 기존 [공유 모델 복구 안내](../../../evaluation/results/memory-need-20261111-v1/README.md)를 따름. 이 초기 모델 복구 명령만으로 선택된 추가 학습 checkpoint까지 원래 `runs/` 경로에 자동 복원하지는 않음.
