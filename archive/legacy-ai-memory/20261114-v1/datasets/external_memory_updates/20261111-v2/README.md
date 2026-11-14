# 외부 기억 갱신 대조 자료 v2

v1의 168문항과 원문 기록·출처 분할을 보존하고, 현재 관측에 새 근거가 있을 때 반복 기록 또는 대상값을 알려 주지 않는 과거 기록을 더하는 대조를 추가함. 새 에이전트 경험을 수집한 자료가 아니며, 공개 대화 벤치마크를 변형한 통제 자료임.

[평가 계획](../../../docs/ai-memory-external-update-validation-2026-11-11.md)과 [문항별 내용 검토](../../../docs/design-data/external-update-validation-20261111-v2/content-reviews.jsonl)에 실행 대상과 제외 사유가 있음. 이번 실행은 수량 문항 95개를 원문 검토해 남긴 설계 47개와 확인 33개임. 나머지 행이 자료 폴더에 남아 있다고 검토·실행된 것은 아님.

- `cases.jsonl`, `annotations.jsonl`, `source-provenance.json`: v1과 바이트 단위로 같음. 주석의 오류를 덮어쓰지 않고 문항별 검토와 실행 제외 목록으로 관리함.
- `controls.jsonl`: 방해 기록 ID, 기록 수·렌더링 토큰 길이, 개수·길이 일치 여부, 방해 조건 생성 가능 여부임.
- `control-overrides.jsonl`: 원문 검토에서 방해 기록 후보를 좁힌 사유임. 모델 출력에 따라 선택하지 않음.
- `decisions.jsonl`, `jobs.jsonl`: 조건과 실제 작업자 입력을 연결함. 조건별 정답은 입력에 없음.
- `protocol.json`, `manifest.json`: 예산·비교 조건과 파일 해시임.

모든 기억 예산은 8기록·1,024토큰임. 길이를 맞춘 분석은 개수가 같고 두 대조 묶음 길이가 옛 묶음의 `max(16, 25%)` 이내인 문항에 한함. 새 근거보다 앞선 방해 기록이 부족하면 그 조건은 생성하지 않음. 방해 기록은 같은 주제를 언급할 수 있으나 질문의 대상값을 알려 주지 않는지 별도로 검토함.

v2 채점은 [보수적 수량 판정기](../../../evaluation/external_update_validation.py)를 사용함. 값 포함 여부와 실제 답을 구분하고 부정·복수 값·설명은 검토 대상으로 남김. v1 점수 및 공개 벤치마크의 공식 점수와 직접 비교하지 않음. 이 폴더에는 작업자 출력이나 실제 득실 정답이 없음.

```bash
python -m data_pipeline.prepare_external_update_controls prepare \
  --source datasets/external_memory_updates/20261111-v1 \
  --tokenizer <원본 작업자 tokenizer.json> --output <새 폴더> \
  --overrides datasets/external_memory_updates/20261111-v2/control-overrides.jsonl

python -m data_pipeline.prepare_external_update_controls export \
  --data datasets/external_memory_updates/20261111-v2 --output <업로드 폴더> \
  --content-reviews docs/design-data/external-update-validation-20261111-v2/content-reviews.jsonl \
  --worker-provenance datasets/memory_state_screen/20261111-v2/source-provenance.json \
  --phase design
```

원 출처의 라이선스·revision은 [v1 README](../20261111-v1/README.md)와 `source-provenance.json`을 따름. BEAM 데이터는 CC BY-SA 4.0, LongMemEval-S 정제판은 MIT로 기록돼 있음.
