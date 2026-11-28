# 기본 테스트

저장·조회·노출 기록, 세션 격리, 전송 비용과 실패, 실험 배정과 채점을 작은 입력으로 검증한다. 기본 실행에 필요한 자료는 저장소에 포함되어 있다.

```bash
uv run python -m unittest discover -s tests
```

[과제 간 전이 검사](test_study_transfer.py)는 메모와 빈 은행의 배정, 중단된 판단, 결과가 빠진 쌍의 점수를 확인한다. 실제 리테일·항공·BFCL 어댑터 연결을 바꿨을 때는 [전이 통합 검사](native_transfer.py)에서 해당 사례만 선택할 수 있다. 이 검사는 고정 벤치 체크아웃과 모의 모델을 사용한다.

```bash
uv run --extra tau2 python -m unittest tests.native_transfer.NativeTransferTests.test_airline_transfer
```

완료된 실험의 원본을 다시 분석할 때는 해당 보고서의 고정 코드와 [재현 안내](../docs/development/historical-reproduction.md)를 따른다.
