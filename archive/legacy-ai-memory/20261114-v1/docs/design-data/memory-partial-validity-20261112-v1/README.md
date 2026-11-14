# 일부 정보가 갱신된 기억 연구를 위한 기존 결과 재집계

기존 47문항의 저장 채점을 다시 집계한 자료임. 새로운 의미 주석·관계 라벨·작업자 성적은 아님. 원래 CSV와 실험 자료는 수정하지 않음.

[연구안](../../ai-memory-partially-valid-memories-plan-2026-11-12.md)과 [이전 가림 실험](../../reports/2026-11-11-external-update-masking.md)에 해석을 연결함.

`recount.py`는 원본 CSV·스크립트의 해시, 출처별 정답·보류·오답, 같은 문항의 개선·악화 ID를 출력함. `evidence-recount.json`은 이번 실행 결과임. 기록된 family ID는 LME 32개·BEAM 11개이며, 이를 의미상 독립성이 검증된 43가족으로 확대하지 않음.

저장소 루트에서 새 추론 없이 확인할 수 있음.

```bash
python3 docs/design-data/memory-partial-validity-20261112-v1/recount.py
```
