# 외부 지식 갱신 대조 자료 v1

공개 벤치마크 두 개의 지식 갱신 문항으로 만든 자료임. 같은 질문에 옛 값 기록, 새 값 기록, 둘 다, 아무것도 주지 않았을 때 작업자의 답이 어떻게 달라지는지 재는 데 씀. **작업자를 실행한 결과는 들어 있지 않음.** 입력, 조건, 채점 기준만 있으며 실제 득실 라벨이 없음.

설계와 사용 순서는 [문서](../../../docs/ai-memory-external-update-data-2026-11-11.md)에 있음.

## 출처와 규모

| 출처 | 원 문항 | 사용 문항 | 독립 단위 | 분할 |
| --- | ---: | ---: | --- | --- |
| [BEAM](https://github.com/mohammadtavakoli78/BEAM) `knowledge_update`, 크기 100K·500K·1M | 180 | 121 | 대화 81개 | confirm 64문항·44대화, dev 29·18, train 28·19 |
| [LongMemEval-S 정제판](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned) `knowledge-update` (보류 문항 제외) | 72 | 47 | 문항 47개 | design 47 |

- BEAM은 이 저장소가 처음 쓰는 자료임. 대화 단위로 confirm·train·dev를 2:1:1로 나눴고, 크기 안에서 대화 ID의 해시 순서로 정해 결과와 무관함.
- LongMemEval-S 500문항은 이전 [외부 검색 평가](../../../docs/reports/2026-11-10-selector-size-and-transfer-validation.md)에서 결과를 본 자료라 `design`으로만 표시함. 설계와 예행에 쓰고 확인 주장에는 쓰지 않음.
- BEAM 10M 크기(10대화)는 파일 구조가 다르고 1GB라 이번 버전에서 뺐음.

제외한 84문항의 사유는 `exclusions.jsonl`에 있음.

| 제외 사유 | BEAM | LongMemEval-S |
| --- | ---: | ---: |
| 옛 정보·새 정보 ID가 없거나 겹침 | 17 | — |
| 채점 기준이 짧은 값 하나가 아님 | 26 | — |
| 근거 발화가 양쪽 세션에 표시돼 있지 않음 | — | 2 |
| 정답이 문장이라 값으로 가려낼 수 없음(5낱말 초과) | — | 6 |
| 새 근거에 정답 값이 없음 | 14 | 16 |
| 옛 근거에도 정답 값이 있음 | 1 | 1 |
| 근거 발화가 512토큰 초과 | 1 | — |

## 파일

| 파일 | 내용 |
| --- | --- |
| `cases.jsonl` | 문항 168개. 질문, 질문 날짜, 후보 기록 8–13개(시간순). 기록의 역할과 정답은 없음 |
| `annotations.jsonl` | 문항별 새 근거·옛 근거·방해 기록의 ID, 정답 기준(`accept`), 옛 값(`stale`), 어휘 순위 |
| `decisions.jsonl` | 문항 × 조건 8개 = 1,344행. 조건마다 전달할 기록과 작업자 입력의 `job_id` |
| `jobs.jsonl` | 서로 다른 작업자 입력 1,206개(`job_id`, `messages`). 같은 메시지는 한 번만 둠 |
| `selector-inputs.jsonl` | 선택 모델용 (질문, 후보 기록) 1,451행. 라벨 없음 |
| `selector-labels.jsonl` | 위 행의 역할(`new`·`old`·`distractor`)과 `needed`(새 근거만 1) |
| `exclusions.jsonl` | 제외한 문항과 사유 |
| `protocol.json`, `source-provenance.json`, `manifest.json` | 규칙·매개변수, 원본 revision과 해시, 파일 해시와 집계 |

정답·역할은 `annotations.jsonl`과 `selector-labels.jsonl`에만 있음. 작업자에게 올리는 것은 `jobs.jsonl`의 메시지뿐임.

## 조건

| 조건 | 현재 대화에 보이는 것 | 과거 기록으로 주는 것 |
| --- | --- | --- |
| `none` | — | — |
| `new_only` | — | 새 근거 |
| `old_only` | — | 옛 근거 |
| `both` | — | 옛 근거와 새 근거 |
| `current_new` | 새 근거 | — |
| `current_new_plus_old` | 새 근거 | 옛 근거 |
| `lexical_top2` | — | 질문과 어휘가 가장 겹치는 후보 2개 |
| `latest_of_top2` | — | 그 2개 중 더 나중 것 |

- 앞의 여섯은 근거 표시로 정한 조건이고, 뒤의 둘은 근거 표시를 보지 않는 단순 정책임.
- `current_new`와 `current_new_plus_old`는 이미 답을 아는 상태에 낡은 기록을 더하는 비교임. 새 근거 발화를 현재 대화로 옮겨 만든 통제 개입이며 자연 발생한 대화가 아님.
- 기록은 항상 오래된 순서로 보이고 ID나 역할 표지가 없음. 시스템 지시문은 “서로 다르면 가장 최근 진술이 현재 사실”이라고 알려 줌. 지시문 전문은 [프로토콜](../../../training/memory_selector/external_updates_protocol.py)에 있음.

## 채점

심판 모델 없이 문자열로 채점함. [채점 코드](../../../evaluation/external_updates.py)

- 성공: 답에 `accept`의 값이 있음. 숫자 하나짜리 정답(`number`)은 그 숫자가 다른 숫자의 일부가 아닌 형태로 있으면 됨. 날짜·시각·여러 숫자(`tokens`)는 해당 토큰이 모두 있어야 함. 그 밖(`text`)은 구절 전체가 있어야 함.
- 옛 값 언급: 답에 `stale` 값이 있음. BEAM 70문항에만 정의됨. 원 자료의 혼동 설명에 나온 옛 숫자가 옛 근거에 있고 새 근거에는 없을 때만 넣었음.
- `UNKNOWN`: 답 전체가 UNKNOWN임.

| 분할 | 문항 | 숫자 정답(대화) | 토큰 정답 | 구절 정답 | 옛 값 정의(대화) | 근거 조건 6개의 호출 | 조건 8개의 호출 |
| --- | ---: | --- | ---: | ---: | --- | ---: | ---: |
| beam/confirm | 64 | 44 (31) | 20 | 0 | 35 (27) | 384 | 483 |
| beam/dev | 29 | 23 (17) | 6 | 0 | 18 (12) | 174 | 206 |
| beam/train | 28 | 22 (17) | 6 | 0 | 17 (14) | 168 | 213 |
| longmemeval_s/design | 47 | 33 (33) | 9 | 5 | 0 | 282 | 304 |

숫자 정답 문항이 가장 믿을 만함. 토큰·구절 정답은 표현이 조금만 달라도 실패로 채점될 수 있어(“6 sets of 20 steps”에 “6 sets”라고 답한 경우) 보조 분석으로 두는 것이 맞음. 채점기는 `--answer-kind number`로 숫자 정답만 집계할 수 있음.

## 명령

```bash
# 다시 만들기 (BEAM 저장소 사본, LongMemEval-S 파일, 작업자 tokenizer가 필요함)
python -m data_pipeline.prepare_external_updates prepare \
  --beam <BEAM 사본> --longmemeval <longmemeval_s_cleaned.json> \
  --tokenizer <Qwen3.5-9B tokenizer.json> --output <새 폴더> \
  --beam-revision b2da22eac88bb0874c64665f13457eb99835774a \
  --longmemeval-revision 98d7416c24c778c2fee6e6f3006e7a073259d48f

# 작업자 업로드 폴더 만들기 (메시지만 들어감)
python -m data_pipeline.prepare_external_updates export \
  --data datasets/external_memory_updates/20261111-v1 --output <업로드 폴더> \
  --worker-provenance datasets/memory_state_screen/20261111-v2/source-provenance.json \
  --split design --condition none new_only old_only both current_new current_new_plus_old

# 작업자 출력 채점 (job_id와 text가 있는 predictions.jsonl)
python -m evaluation.external_updates \
  --data datasets/external_memory_updates/20261111-v1 --predictions <predictions.jsonl> --answer-kind number
```

같은 원본으로 다시 만들면 모든 파일의 바이트가 같음. `manifest.json`의 해시로 확인함.

## 라이선스

BEAM 자료는 CC BY-SA 4.0, LongMemEval은 MIT임. 이 폴더의 기록 본문은 두 자료의 발화를 그대로 옮긴 것이므로, 저장소를 공유하거나 자료를 다시 배포할 때 출처 표시와 BEAM 부분의 동일 조건을 지켜야 함.

## 한계

- 실제 작업자 결과가 없음. 작업자가 새 근거를 받고 이 문항들을 푸는지도 아직 모름.
- `needed`와 역할은 원 자료의 근거 표시를 옮긴 것이며, 기록을 줬을 때의 효과를 잰 값이 아님.
- 방해 기록은 같은 대화(BEAM)나 다른 세션(LongMemEval-S)에서 질문과 어휘가 겹치는 사용자 발화임. 정답 값과 옛 값이 든 발화는 뺐지만, 원 자료가 표시하지 않은 더 이전 값이 들어 있을 수 있음.
- BEAM의 근거 표시에는 잡음이 있음. 표시된 새 발화에 답이 없는 14문항을 제외한 것이 그 예임.
- BEAM의 시간 표지는 대화 구간 단위라 옛 기록과 새 기록의 날짜가 같은 경우가 많음. 순서는 기록 번호로만 전달됨.
- 문자열 채점은 심판 모델 채점보다 엄격하거나 느슨할 수 있음. 결과를 쓰기 전에 표본을 사람이 확인해야 함.
