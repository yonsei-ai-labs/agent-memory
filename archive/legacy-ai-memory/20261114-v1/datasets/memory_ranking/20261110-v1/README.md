# 제공 후 성공과 후보 순위의 제한된 학습 진단

기존 [408개 기억 묶음](../../memory_decisions/20261110-refresh-v1/README.md)의 입력과 관측 작업 성공 라벨을 그대로 참조함. 새 작업자 호출·라벨 변경·독립 경험 추가는 없음.

- `config.json`, `protocol.json`: 이진 성공 학습 / 이진 성공+순위 손실의 두 조건과 자원 한도.
- `folds.json`: 기존 3개 바깥 가족 분할. 각 학습 8가족 안에서 해시로 고정한 6가족 학습·2가족 검증.
- `state-inputs.jsonl`: 후보 기억을 제거한 96개 현재 상태. 무기억 성공 판독층 전용 입력.
- `state-targets.jsonl`: 별도 무기억 관측 정답. 신경망 입력에 포함하지 않음.
- `manifest.json`: 사용한 기존 자료와 설정의 해시.

6개 내부 LoRA 학습에서 8·16·24 epoch 중 하나를 정하고, 바깥 학습 8가족에서 두 초기값으로 새로 적합함. 바깥 12개 LoRA의 모든 결과를 보고하며 좋은 모델만 고르지 않음. 무기억 판독층은 고정 base의 상태 표현에서 별도로 학습함.

내부 검증은 각 16상태이며 후보 성공이 다른 상태가 7·7·6개임. 현재 12가족은 이미 탐색한 개발 자료이므로 이 비교를 새 출처 확인 결과로 해석하지 않음.

```bash
python3 -m data_pipeline.prepare_memory_direct_ranking check \
  --path datasets/memory_ranking/20261110-v1
```

위 검사는 GPU를 사용하지 않음. 실제 학습은 [계획·결과 노트](../../../docs/reports/2026-11-10-memory-direct-ranking.md)의 별도 명령을 사용함.
