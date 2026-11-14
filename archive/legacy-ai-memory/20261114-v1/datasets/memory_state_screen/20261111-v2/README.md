# 현재 상태 대조 자료 v2

v1의 카탈로그 이름·속성 안 구분자만 이스케이프한 측정 버전임. 실제 선택기 입력과 관계 주석은 동일함. v1의 작업자 실행은 없었음.

이미 사용한 train 6가족의 실제 과거 선택 기록을 사용한 36상태 진단임. 현재 요청·관측·실패 상태는 합성 개입이며 독립적인 새 출처가 아님.

- `cases.jsonl`: 목표·현재 관측·기억 은행·제한. 실제 득실 정답이 없음.
- `annotations.jsonl`: 필요한 기록·허용 답·대조 기억. 실제 득실은 `null`로 남김.
- `source-provenance.json`: 원래 공유 산출물·카탈로그·생성 기록·고정 모델 해시.
- `protocol.json`, `manifest.json`: 작업자 실행 전에 고정한 범위와 데이터 해시.

관계 주석이 실제 효용을 대신하지 않음. 동일 목표·은행의 상태 쌍과 모든 근거가 현재 보이는 대조를 유지함. 간단한 공개 상태 규칙도 필수 기록을 고를 수 있으므로 신경망 필요성 평가에 충분한 자료는 아님.

```bash
python -m data_pipeline.prepare_memory_state_screen --output /tmp/state-screen-design
```

목표 판독기 준비는 `data_pipeline.score_memory_state_screen`을 사용하며, 기존 로컬 reader 가중치와 학습 환경이 추가로 필요함. 이 단계는 CPU에서 실행함. 작업자 메시지 내보내기는 `data_pipeline.export_memory_state_screen_inputs`를 사용함. Colab 실행 명령은 GPU를 실제 할당하므로 결과 재집계와 구분해야 함.

상세 계획과 결과는 [보고서](../../../docs/reports/2026-11-11-memory-state-screen.md)에 정리함.
