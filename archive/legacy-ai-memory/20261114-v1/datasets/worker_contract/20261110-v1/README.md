# 작업자 계약 기능 점검

기존 36문제와 표면 변형 36문제, 두 출력 방식으로 총 144입력을 동결함. 각 자료의 정답 가능·불가능·정보 부족 항목은 각각 24개임. 이 자료는 독립 출처 일반화나 기억 선택기의 득실 학습 자료가 아님.

- `data/`: 작업자에게 업로드할 입력·설정·고정 가중치 근거·입력 해시만 포함함.
- `local/cases.json`: 사실·정답·누락 정보의 두 가능성을 검증하는 증거. 업로드하지 않음.
- `local/targets.json`: 출력 방식·자료 구분·허용 정답·참조 근거. 업로드하지 않음.
- `protocol.json`: 확인 기준, 표본의 한계, 원본 연결, 실행 전 동결 계약.
- `preflight.json`: 토큰 길이·자료 규모·실행 전 검사.

두 방식의 사용자 입력은 동일함. 기존 답변 형식과 근거 ID·상태·답변을 함께 내는 형식을 비교하며, 시스템 지침도 함께 달라짐. 과거 점검과 달리 두 방식 모두 기록 ID를 표시하고 출력 상한은 256토큰으로 같게 둠. 원래 64토큰 점검의 결과를 새 실행의 기준값으로 재사용하지 않음.

원본은 [이전 정책 후속 실험](../../../evaluation/results/travel-policy-followup-20261109-v1/README.md)의 공개 공유 ZIP에 있음. 생성·검산은 로컬 CPU에서 수행함.

```bash
python3 -m data_pipeline.prepare_worker_contract --output /tmp/worker-contract-design
python3 -m unittest evaluation.tests.test_worker_contract -v
```

정답을 사용한 프롬프트 선택·추가 학습은 하지 않음. 기능 확인 이후 실제 과거 기록에 적용할지는 [현재 연구 상태](../../../docs/current-status.md)의 기준에 따라 판단함.
