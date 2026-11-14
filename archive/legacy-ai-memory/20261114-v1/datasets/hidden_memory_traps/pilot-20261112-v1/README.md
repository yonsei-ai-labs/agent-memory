# 숨은 함정 기억 자료: 파일럿 패킷 (`hidden_memory_traps / pilot-20261112-v1`)

같은 기억의 적용성이 그 뒤에 일어난 일에 따라 달라지는 여행 예약 세션 자료임. 문제(쓰이지 않게 된 예약, 덧붙인 방문, 한 번뿐인 예외 등)는 평범한 말 속에 숨어 있고, 정답은 구조화된 대본에서 계산했음. 실제 득실은 같은 작업자에 기억을 제공·보류한 결과를 얻은 뒤 따로 계산함. 데이터의 문장은 영어임.

작업자를 아직 돌리지 않은 **실행 전 패킷**임. `utility`는 모두 null이고, 사람 검토 전이므로 `ambiguous`도 모두 null임.

**2026-11-12 검토 후 사용 안내:** [별도 검산](../../../docs/reports/2026-11-12-hidden-memory-traps-pilot-review.md)에서 92정답·3,468입력을 확인함. 아래 UNKNOWN·부가 메시지의 설명을 정정했고, 실제 전달 블록의 예산 초과는 아직 원본에 남아 있음. [데이터 역할·누적 실험안](../../../docs/ai-memory-data-cleanup-and-context-stream-plan-2026-11-12.md)에 후속 순서를 정리함. 이번 안내 수정은 manifest에 포함되지 않는 README만 바꾸며 원본 라벨·입력·protocol은 보존함.

- **생성 코드:** 브랜치 `hidden-traps-data`의 `experiments/hidden_traps/`(커밋 `953217b`, 테스트 71개). 배달 경로 자료처럼 코드는 main에 넣지 않았음. 만든 코드의 해시는 `protocol.json`의 `code_sha256`과 `code_files_sha256`에 있음.
- **설계와 구현 계획:** `docs/ai-memory-hidden-memory-traps-plan-2026-11-12.md`, `docs/ai-memory-hidden-memory-traps-build-plan-2026-11-12.md`(main 작업 트리, 미커밋).
- **Codex 인계:** [인계 노트](../../../docs/ai-memory-hidden-memory-traps-pilot-handoff-2026-11-12.md).

## 규모

| | 수 |
| --- | ---: |
| 세션 (원천 가족 하나씩) | 4 (Dallas, Buffalo, Seattle, San Diego) |
| 세션당 턴 | 53 |
| 결정 상태 | 92 |
| 사례 (결정 × 겉말 2 × 길이 3) | 552 |
| 서로 다른 모델 입력 | 3,468 (S 1,090, M 1,353, L 1,419; 단계 사이에 같은 입력은 한 번) |
| 블라인드 검토 문항 | 58 (숨긴 문장 틀 42개 전부, 결정 16개) |

| 함정 | 사람이 잘하는 일 | 상태 |
| --- | --- | ---: |
| void | "취소"라는 말 없이도 예약이 쓰이지 않게 된 것을 알아챔 | 12 |
| complement | 덧붙인 것과 바꾼 것을 구분함 | 8 |
| exception | 한 번뿐인 예외가 늘 가진 선호를 덮지 않게 함 | 8 |
| constraint | 오래전에 한 번 말한 선호를 떠올림 | 8 |
| history | 바뀐 뒤에도 처음 계획을 기억함 | 8 |
| lookalike | 이름이 거의 같은 두 사람을 구분함 | 8 |
| absent | 예약한 적 없는 것은 UNKNOWN이라고 함 | 8 |
| control | 평범한 참조(가깝게·멀리), 바뀐 예약의 참조, 첫 예약이 유지되는 조건부 참조 | 32 |

단순한 지름길로는 함정을 풀 수 없게 두 규칙을 두었음.

- **열린 예약은 베낄 수 없음.** "그 사람에게 맞는 아무 곳"을 묻는 예약은 그 사람의 다른 예약보다 앞에 옴(한 번뿐인 예외 예약만 그 앞). 그래서 그 사람의 이전 예약에는 허용되는 장소가 없고, 답은 선호에서만 나옴. 규칙은 메뉴의 절반 이하만 허용하므로 찍어서 맞힐 확률은 절반 이하임.
- **조건부 참조는 양쪽으로 갈림.** "Erik의 2일차 예약과 같게, Erik이 안 쓰면 Ana의 예약과 같게" 같은 참조 16개 가운데 8개는 첫 예약이 무효라 대체로 가고, 8개는 첫 예약이 유지됨. "언제나 대체"는 절반만 맞음.

## 들어 있는 것

| 파일 | 행 | 내용 |
| --- | ---: | --- |
| `source-inventory.jsonl` | 120 | 원천 가족 전체. 그룹, 이전 사용(`prior_use`), 이번 역할(pilot 4, main 20, sealed 40, not_used) |
| `sessions.jsonl` | 4 | 세션별 도시, 메뉴, 사람, 닮은 이름 짝, 선호 규칙 |
| `events.jsonl` | 212 | 대본 턴. 생성 구조이며 모델 입력이 아님 |
| `cases.jsonl` | 552 | 사례마다 요청 문장, 후보 은행(기록 ID), 조건별 입력 해시·토큰 수·전달한 기록 ID |
| `records.jsonl` | 1,536 | 기록 본문. 겉말·길이 단계마다 한 번씩 있고, 후보 은행이 여기를 가리킴 |
| `inputs-S.jsonl.gz`, `inputs-M.jsonl.gz`, `inputs-L.jsonl.gz` | 1,090 / 1,353 / 1,419 | 서로 다른 실제 모델 입력(메시지 목록). `input_sha256`으로 `cases.jsonl`과 이음 |
| `relations.jsonl` | 92 | 결정마다 맞는 답, 필요·무효·방해 기록, 후보별 관계와 `judge_with`, `needs_memory`, `ambiguous`·`utility`(null) |
| `contrasts.jsonl` | 496 | 드러낸↔숨긴, S↔M↔L, 결정 사이에 관계가 바뀌는 기록 |
| `review-packet.jsonl`, `review-key.jsonl` | 58, 58 | 정답을 가린 검토표와 그 정답. 결정 문항에는 작업자가 보는 카탈로그·과제 문장, 숨긴 S 기록, 요청이 있음 |
| `protocol.json` | | 생성 설정과 수, 단서어·금지어, 토크나이저·원천 파일·코드 파일의 sha256, 모델 입력에 넣지 않을 파일·필드(`never_model_input`), 같은 입력이 되는 조건 쌍(`identical_inputs`), 설계상 성립하는 것 |
| `manifest.json` | | 이 README를 뺀 모든 파일의 sha256과 크기 |

## 열의 뜻

**겉말 (`surface`)**
- `explicit`: 함정 사건을 과제의 말로 드러냄("Cancel …", "one-time exception").
- `hidden`: 일어난 일을 평범한 말로 씀. 숨긴 사건 턴에는 그 함정의 금지어가 없음. 숨긴 무효화의 금지어에는 과제 문장의 "use", "won't", "will not"도 들어 있어서, 과제 문장과 단어를 맞춰서는 풀 수 없음.
- 요청 문장은 두 겉말에서 글자까지 같음. 보완의 두 문장은 같은 방문을 말함.

**길이 단계 (`tier`)** (세션 마지막 결정 시점, 작업자 토크나이저, 숨긴 겉말)

아래 `기록 목록`은 카탈로그·과제·현재 질문까지 포함한 조건 입력 전체임. 주입하는 기억 블록만의 길이는 S 2,196–2,368, M 16,956–18,459, L 78,061–79,803토큰임. 모두 채팅 템플릿을 제외함.

| 단계 | 더한 것 | 전체 대화 | 기록 목록 |
| --- | --- | ---: | ---: |
| S | 없음 (대본 턴만) | 5.7k–5.8k | 3.3k–3.5k |
| M | 예약 턴마다 간단한 검색 결과, 앉은 자리마다 부가 메시지 3개 | 30k–32k | 18k–20k |
| L | 상세 검색 결과, 앉은 자리마다 긴 부가 메시지 10개 | 103k–105k | 79k–81k |

부가 메시지는 다른 사용자 말처럼 기록에 원문 그대로 들어감. 그래서 기록 조건도 단계와 함께 길어지고, 2k·8k 예산이 실제로 고르게 됨(단계별 184사례 중).

| | S | M | L |
| --- | ---: | ---: | ---: |
| `bm25_2k`가 기록 목록을 줄인 사례 | 39 | 180 | 184 |
| `bm25_8k`가 기록 목록을 줄인 사례 | 0 | 122 | 184 |
| 필요한 기록을 뺀 사례 (`bm25_2k` / `bm25_8k`) | 0 / 0 | 0 / 0 | 9 / 2 |

**조건 (`conditions`의 열쇠)**

| 조건 | 작업자가 받는 것 |
| --- | --- |
| `none` | 카탈로그, 과제, 현재 요청만 |
| `full_history` | 앞선 턴 전체를 대화로 (고정 과거의 답 포함) |
| `sitting_plus_records` | 지금 앉은 자리는 대화로, 앞 자리는 기록 목록으로 |
| `record_list` | 기록을 쌓은 목록 |
| `latest_surface` | 여행자·날짜마다 마지막 기록. 말에 나온 이름과 "day N"만 읽음 |
| `marked_surface` | 기록 목록. 같은 여행자·날짜의 뒤 기록에 단서어가 있으면 표시함 |
| `bm25_2k`, `bm25_8k` | BM25 상위 기록의 개별 토큰 합에 2,048 / 8,192 상한 적용. 실제 블록 초과는 아래 주의 참고 |
| `needed_only`, `invalid_removed` | 정답 라벨로 만든 진단 조건 (필요한 기록만 / 무효 기록만 뺌) |

**예산 수정 대기:** 머리말·줄바꿈을 포함한 실제 블록은 `bm25_2k` 254/552사례에서 최대 49토큰, `bm25_8k` 132/552에서 최대 50토큰 초과함. 같은 컨텍스트 예산 비교 전 생성기의 계산을 고치고 새 버전으로 입력·해시·확보율을 재생성해야 함. 위 검색 누락 9/2는 현재 원본의 집계임.

두 조건이 같은 입력이 되는 경우가 있음. `protocol.json`의 `identical_inputs`에 겉말·단계·조건 쌍별로 셌으므로, 실행할 때 입력마다 한 번만 돌리면 됨. 겉말마다(결정 92개):
- S에서 `bm25_8k`는 언제나 `record_list`와 같음 (92).
- `invalid_removed`는 무효 기록이 없는 결정 72개에서 `record_list`와 같음.
- `needed_only`는 absent 결정 8개에서 `none`과 같음.
- `full_history`는 첫 자리의 결정 9개에서 `sitting_plus_records`와 같음.
- `invalid_removed`가 `latest_surface`와 같은 것은 12개뿐이라, 이 자료에서는 다른 조건임.

**관계 (`relations.jsonl`)**
- `acceptable`: 칸(`dinner`, `accommodation`)별 맞는 장소 ID 목록, 또는 `["UNKNOWN"]`.
- `required_memory_ids`, `invalid_memory_ids`, `distractor_memory_ids`: 필요한 기록, 무효가 된 기록, 쓸 만해 보이지만 답이 아닌 기록. 닮은 이름 상대의 같은 날 예약은 그 사람을 묻는 모든 요청의 방해 기록임.
- `candidate_relations`: 기록마다 `relation`(needed·invalid·distractor·irrelevant)과 `judge_with`(그 후보를 판정하려면 함께 봐야 하는 기록).
- `needs_memory`: 현재 생성기는 답이 UNKNOWN이면(ref_void, absent) false로 둠. 사실상 답의 종류를 구분하는 필드이며 실제 제공 필요성·성공·득실 정답으로 쓰지 않음. UNKNOWN 12판단 중 4판단은 무효화 기록이 근거임. 그 기억이 실제 작업자의 잘못된 구체 답변을 줄일 수도 있음.

**독립 단위**는 세션(`session_id`)임. 한 세션의 결정들은 서로 독립이 아님.

## 주의할 점

- 인공 자료임. 여행 예약 한 영역, 영어, 숨긴 사건 종류마다 문장 틀 3개.
- **고정 과거:** 앞선 턴의 답은 대본의 정답으로 고정했음. 작업자가 직접 진행한 세션이 아님. 그래서 두 작업자가 같은 과거를 읽음.
- 라벨은 대본의 규칙을 따름. 사람의 독립 판정은 아직 없음.
- **설계상 성립하는 것** (`protocol.json`에도 있음):
  - 답이 UNKNOWN인 곳에서는 상수 UNKNOWN 출력이 정답임. 실제 `none` 작업자가 그 답을 내는지는 아직 측정하지 않았음. 원본 protocol의 해당 설명도 이 의미로 정정하여 읽음.
  - 숨긴 무효화 기록은 여행자와 날짜를 말하므로, `latest_surface`에서 무효가 된 예약을 대신함(무효화 12개 상태 모두에서 무효 예약을 주지 않음).
  - 보완 기록도 같은 방식으로 예약을 밀어냄(보완 8개 상태 모두에서 필요한 예약이 빠짐).
  - 숨긴 겉말에는 단서어가 없어서, `marked_surface`는 숨긴 무효화를 표시하지 않음. 숨긴 겉말의 표시 243개는 모두 변경 기록의 "replaces"에서 나옴.
- **부가 메시지**는 코드로 만든 표(날씨, 버스 시간표, 짐 목록, 운영 시간, 단체 대화)이고, 모델이 쓴 대화가 아님. 닮은 이름이 포함된 부가 기록 108행이 있으나 새 예약 사실을 주지는 않음. 이는 길이·표현별 기록 행 수이며 독립 경험 108개가 아님.
- **검색 결과**는 M·L의 예약 턴마다 같은 메뉴 목록임. 길이만 늘리고 새 후보는 없음.
- **원천 카탈로그의 이상:** 일부 장소 이름이 깨져 있고, 몇몇 요리 태그가 이상함(예: Baskin Robbins가 BBQ). 라벨은 작업자가 보는 속성을 따름.
- 토큰 수는 채팅 템플릿을 뺀 본문 토큰임.
- 별도 장부·입력 검산과 설계에 노출된 다른 모델의 문장 42개·판단 16개 검토를 완료함. 독립 인간 검토를 대체하지 않으며 `ambiguous`는 계속 null임. 봉인된 `new-b` 40가족과 첫 자료의 24가족(Codex 증강 11가족 포함)은 이 패킷에 쓰지 않았음.
- 이전 `memory_state_supplement/20261112-v3`는 다른 부모 manifest(`010189…`)에 기반함. 이 패킷(`a097c765…`)과 기억 ID만으로 결합하지 않음.

## 다시 만드는 법

브랜치 `hidden-traps-data`(커밋 `953217b`)를 체크아웃한 저장소 루트에서 실행함.

1. 작업자 토크나이저를 `runs/hidden-traps/worker-tokenizer/tokenizer.json`에 둠(git 밖). `datasets/memory_need/20261111-v1/source-results.zip` 안의 `worker-tokenizer/tokenizer.json`과 같은 파일임. sha256은 `protocol.json`의 `tokenizer_sha256`.
2. 다음을 실행함. tokenizers가 든 main의 가상 환경이 필요함.

```bash
/home/depa/ai-memory/.venv/bin/python -B -m unittest discover -s experiments/hidden_traps/tests -t .
/home/depa/ai-memory/.venv/bin/python -B -m experiments.hidden_traps.build --role pilot --out <새 폴더>
```

빌드는 약 13분 걸림. 같은 코드면 모든 파일의 해시가 이 폴더의 `manifest.json`과 같음. 이 폴더 자체의 검사는 같은 브랜치에서 `packet.validate_packet('<이 폴더>')`로 함(해시, 미래 기록, 입력별 정답 충돌, 금지어, 문장 틀 포함 여부).
