# 전달 경로 실험 자료 (`delivery_path_validity / 20261112-v1`)

같은 작업자에게 같은 과거를 여러 형태로 줬을 때 답이 어떻게 달라지는지 잰 실험의 자료임. 주는 형태는 자기 대화, 복사한 로그, 기록 세 형식(쌓기·무효 표시·최신), 필요한 기록, 다른 모델이나 학습 선택기가 고른 기록임. 작업자는 Qwen3.5-9B와 Qwen3.5-27B임. 실험은 브랜치 `worktree-delivery-path-experiment`의 `experiments/delivery_path/`에서 했음. 그 브랜치의 마지막 상태는 태그 `archive/delivery-path-experiment-20261112`로 남겼음. 이 폴더는 `export_dataset.py`가 로컬 실행 폴더에서 만든 것임(`manifest.json`의 `source_commit`). 결과의 해석은 [실험 보고서](../../../docs/reports/2026-11-12-delivery-path-experiment.md)에 있고, 더 자세한 노트는 태그의 `experiments/delivery_path/docs/`에 있음.

**다른 자료와 합치지 않은 별도 자료임.** 학습용으로 만든 것이 아님. 우선 평가와 진단에 쓰는 것을 권함.

## 들어 있는 것

| 파일 | 행 | 내용 |
| --- | ---: | --- |
| `travel/sessions.jsonl` | 28 | 40요청 여행 세션(파일럿 8, 본 실행 20). 요청마다 종류·여행자·날짜·장소·참조 대상이 있음 |
| `travel/catalog.jsonl` | 529 | 세션이 쓰는 카탈로그 장소(식당·숙소) |
| `travel/turns.jsonl` | 2,240 | 작업자별로 세션을 직접 진행한 기록. 요청마다 작업자의 답, 그 답에서 쓴 기록(쌓기 형식), 세션 끝 시점의 무효 표시 형식, 세션 끝에 아직 유효한지 |
| `travel/labels.jsonl` | 2,240 | 그 작업자의 앞선 답을 기준으로 한 요청별 정답 |
| `travel/outcomes.jsonl` | 15,740 | 채점한 (요청, 조건)마다 준 기록 ID, 작업자 출력, 해석한 답, 성공 여부, 틀린 답이 따른 것 |
| `travel/selections.jsonl` | 2,320 | 작업자에게 준 선택. 9B나 27B의 선택(원 출력 포함), 또는 학습 선택기의 선택 |
| `public/items.jsonl` | 90 | LongMemEval-S 58문항, BEAM 32문항. 질문, 정답, 길이별 기록 수와 옛·새·필요한 기록 ID |
| `public/outcomes.jsonl` | 5,706 | 공개 자료의 채점 행(문항, 길이, 조건) |
| `public/selections.jsonl` | 1,412 | 공개 자료에서 작업자에게 준 선택 |
| `public/*-review-20261112.jsonl` | 72, 35 | 손으로 검토한 문항별 판정 규칙(맞는 값·다른 값의 정규식)과 제외 사유 |
| `selector/` | | main 학습 선택기(Harrier 0.6B와 LoRA, `haug`·`hreplay`)의 쌍별 점수 25,410개, 고른 기록, 재현 확인(쌍 문장은 뺐음) |
| `stage1/new-a-20261112/` | | 1단계 확인 실행(새 가족 43개)의 결과를 그대로 복사한 것: 원 출력, 채점 행, 요약, 세션 기록, 예상 판정 |
| `audits/` | 78개 파일 | 모든 실행의 검산 결과. 세션 정지, 반복 동일, 토큰 단위 재현을 확인함 |

행 수와 해시는 `manifest.json`에 있음.

## 열의 뜻

**공통**
- `worker`: `qwen3.5-9b`는 리비전 `c2022362`, `qwen3.5-27b`는 `fc05daec`임. greedy, thinking 끔, 출력 최대 64토큰으로 입력당 한 번 실행했음.
- `set`: `pilot`, `main`(공개 자료는 `main-full` = 약 11만 토큰 길이).

**조건 (`condition`)**

| 조건 | 작업자가 받은 것 |
| --- | --- |
| `none` | 현재 요청만 |
| `own_session` | 자기 대화. 앞선 요청과 자기 답이 앞 턴에 있음 |
| `quoted_log` | 같은 대화를 새 세션의 한 메시지 안에 인용한 것 |
| `records_append` | 앞선 결정·취소마다 기록 하나씩. 아무 표시 없음 |
| `records_marked` | 같은 기록. 나중에 바뀌거나 취소된 결정에 "[No longer in force: …]"를 붙임 |
| `records_latest` | 여행자·날짜마다 유효한 결정(또는 취소) 하나만 |
| `needed_records` | 정답 라벨이 필요하다고 한 기록만 |
| `selected_by_X` | X가 고른 기록만 새 세션에서 줌 |
| `own_session+selected_by_X` | 자기 대화 + X가 고른 기록 |

`X`는 `9b`, `27b`(둘 다 `solve_first` 지시문), `haug`, `hreplay`(학습 선택기)임.

**여행 정답 (`travel/labels.jsonl`)**
- `type`: `book`, `change`, `cancel`, `ref`(`distance`가 `near`·`far`), `ref_changed`, `ref_conditional`(`first_cancelled`이면 앞 예약이 취소됨).
- `required_memory_ids`: 이 요청에 필요한 기록.
- `counter_memory_ids`: 무효가 된 기록. 따르면 틀림.
- `acceptable`: 맞는 장소 ID.
- `answerable`: 작업자가 앞에서 잘못 예약해 정답이 없는 요청은 `false`이고, 채점하지 않음.

**틀린 답이 따른 것 (`followed`)**
- 여행: `void`(무효가 된 결정), `other_decision`(다른 사람이나 다른 날의 결정), `fresh`(기록에 없는 새 장소), `withheld`(UNKNOWN).
- 공개 자료: `correct`, `counter`(다른 값, 지금 값 문항이면 옛 값), `both`, `withheld`, `other`.

**독립 단위**는 여행에서는 세션(`session_id`), LongMemEval-S에서는 문항, BEAM에서는 대화(`family_id`)임. 한 세션 안의 요청들은 서로 독립이 아님.

## 주의할 점

- 여행 세션은 변경·취소·조건부 참조를 일부러 넣어 만든 인공 과제이고, 문장 틀은 한 가지임.
- 여행의 `records_marked`와 `records_latest`는 라벨로 만든 것이 아님. 여행자·날짜를 열쇠로 한 단순한 기록 규칙이 세션의 사건에서 만든 것임. 공개 자료의 두 형식은 손 검토의 옛·새 구분으로 만든 것임. `needed_records`는 정답 라벨임.
- `selected_by_9b`와 `selected_by_27b`는 "먼저 풀고 근거를 지목하라"는 지시로 만든 선택임. 고르는 모델의 풀이가 섞여 있으므로, 27B의 선택은 기억 선택의 공정한 비교가 아니라 상한 참고값으로 읽어야 함.
- 학습 선택기는 현재 관측을 "(No current observations.)"로 비워 두고 채점했음. 그 모델의 관측과의 관계 판단은 쓰이지 않은 조건임. `haug`의 문턱은 main 실험이 고정한 0.15가 아니라 0.5로 썼음.
- 공개 자료의 대화 원문은 넣지 않았음. 기록 ID는 `<question_id>:<session>:<turn>` 형식이고, 원문은 각 원본(LongMemEval-S cleaned 파일, BEAM 대화 파일)에서 같은 코드(`lme.py`, `beam.py`)로 다시 만들 수 있음.
- 실행과 분석을 한 사람(Claude)이 했고, 독립 검산은 아직 없음. 1단계의 시험용 가족 40개(`new-b`)는 쓰지 않았음.

## 이 자료로 할 수 있는 것

- **기록 사이 관계의 평가 자료.** 요청마다 앞선 기록 수십 개(사람·날짜만 다른 비슷한 기록) 가운데 무엇이 필요하고 무엇이 무효인지 라벨이 있음. 관계를 판단하는 선택기를 평가하는 데 쓸 수 있음. 쌍 단위 평가라면 `travel/turns.jsonl`의 기록과 `travel/labels.jsonl`의 ID를 이으면 됨.
- **어떤 기억이 작업자를 속이는지.** `travel/outcomes.jsonl`의 `followed`와 `delivered_memory_ids`를 보면 됨.
- **형태별 비교 재계산.** 결과 노트의 모든 수는 `outcomes` 표만으로 다시 계산할 수 있음.

## 다시 만드는 법

태그 `archive/delivery-path-experiment-20261112`를 체크아웃한 저장소 루트에서 다음을 실행함. 실행 폴더가 있어야 함.

```bash
python -m experiments.delivery_path.export_dataset --output <새 폴더>
```

로컬 실행 폴더(`runs/delivery-path`, 15.0GB, 항목 14,400개)의 전체 묶음은 main 트리의 `runs/delivery-path-archive-20261112/runs-delivery-path.tar`에 있음(git 밖). sha256은 `1765b5bf2c9085154a466ea36f2c090ba2d7d5a8001b2eff389d778f68e28cda`임. 그 안에 입력 묶음, 토큰 ID, 원 출력, 세션 기록이 모두 있음.
