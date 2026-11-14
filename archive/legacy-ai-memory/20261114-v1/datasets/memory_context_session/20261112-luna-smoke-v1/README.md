# Luna 누적/분리 세션: 4문항 세팅 테스트

**기존 설계 가족 1개의 4질문만 사용하는 연결·입력·측정 점검 자료임.** 새 독립 평가 자료나 학습 데이터가 아님. [실행 결과와 정량 기준](../../../docs/reports/2026-11-12-luna-session-setup.md)을 함께 읽음.

| 항목 | 내용 |
| --- | --- |
| 부모 | `hidden_memory_traps/pilot-20261112-v1`, manifest `a097c765…` |
| 출처 | 기존 설계 세션 `58357e6d94c7d840607cf928`(Buffalo), 이전 사용 이력이 있는 1가족 |
| 질문 선택 | 가장 작은 session ID에서 absent/open_book/history/ref_changed의 첫 질문, 시간순 정렬 |
| 질문 시점 | 12 / 16 / 39 / 47. 전체 질문 중 4개만 묻는 희소 일정임 |
| 공개 관측 | 42기록을 원문 손실 없이 79청크로 구성함. 각 시점 이전의 관측만 사용함 |
| 제외 | 대본의 과거 질문 답으로 만든 예약 확인 17기록 |
| 주 결정·국소 대조 | 2세션 모드×2전달 조건×4질문=16결정, 같은 상태의 제공/보류 8쌍 |
| 모델·경로 | OpenRouter `openai/gpt-6-luna`, OpenAI 제공자, reasoning `none` |
| 예산 | 최근 대화만 4k / 최근 2k+BM25 기억 2k. **o200k_base 기준**임 |

`observations.jsonl`과 `questions.jsonl`은 공개 데이터임. `labels.jsonl`의 답 집합·관계 ID, `protocol.json`과 manifest는 모델 입력으로 주지 않음. 내부 source ID와 모델에 보이는 중립적인 기록/청크 ID를 구분함. 모델 답은 최근 대화로만 누적하고 확인된 예약 사실로 은행에 넣지 않음.

`session.json`, `observations.jsonl`, `questions.jsonl`, `labels.jsonl`, `config.json`, `protocol.json`을 manifest로 고정함. 이 README는 manifest에서 제외함. 생성/실행 코드가 바뀌면 새 버전으로 재생성해야 함.

**v1에서 확인한 한계:** 이전 질문·답변이 긴 부가 기록에 밀려 8개 모드 대응 중 6개에서 실제 입력이 같았음. 또한 선호 기반 신규 추천을 조회형 질문으로 바꾸는 지시가 모호할 수 있음. 현재 결과를 보존하며, 문구와 누적 이력 노출을 보완하기 전 본실험으로 확대하지 않음.

원본 token 수는 Luna의 네이티브 수와 다름. 현재 OpenRouter 경로의 사전 계산 endpoint는 404이며, 로컬 tiktoken에도 Luna 전용 매핑이 없어 기준 인코딩을 명시함. 실제 provider 입력은 별도 공통 상한 6,144토큰으로 사후 검사함. 네이티브 4k를 정확히 사전 맞춘 실험이라고 하지 않음.

```bash
# CPU 테스트: 외부 호출 없음
.venv/bin/python -B -m unittest tests.test_context_session_pilot

# 모의 실행: UNKNOWN을 내는 가짜 작업자이며 성능 평가가 아님
.venv/bin/python -B -m evaluation.context_session_pilot test \
  --packet datasets/memory_context_session/20261112-luna-smoke-v1 \
  --output /tmp/context-session-dry-run
```

실제 실행은 `--live`를 명시해야 하며 이 구현은 4질문보다 큰 패킷을 거부함. 현재 v1은 위 한계가 확인된 보존본이므로 같은 코드를 반복 실행해 유리한 결과를 고르지 않음. `config.json`의 호출·비용 상한과 원문·입력·스냅샷 해시를 함께 보존함.
