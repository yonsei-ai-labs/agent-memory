# 숨은 함정 기억 자료: 파일럿 패킷 구현 계획

**후속 상태 안내(2026-11-12):** 아래는 제작 당시 구현 계획을 보존한 것임. 현재 납품본·검토 상태·수정 대기 항목과 누적 세션의 다음 구현 범위는 [최신 통합안](ai-memory-data-cleanup-and-context-stream-plan-2026-11-12.md)을 먼저 확인함. 이 문서의 예전 체크박스를 현재 미구현 목록으로 해석하지 않음.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 작업자를 실행하기 전에 검토할 수 있는 숨은 함정 기억 자료의 파일럿 패킷(4세션, 영어)을 만든다.

**Architecture:**
- 기존 배달 경로 생성기(`experiments/delivery_path`)를 이어 쓰는 새 패키지 `experiments/hidden_traps`를 만든다.
- 대본(사건)에서 정답을 계산한다. 같은 대본을 드러낸·숨긴 겉말과 S·M·L 길이로 렌더링한다.
- 과거는 대본이 정한 고정 과거(정답 응답)로 둔다. 그래서 모든 조건의 실제 모델 입력을 실행 전에 확정할 수 있다.

**Tech Stack:** Python 3.11 표준 라이브러리, `unittest`, 토큰 수만 `tokenizers`(main의 `.venv`), 기존 `experiments.delivery_path` 모듈

**Spec:** `docs/ai-memory-hidden-memory-traps-plan-2026-11-12.md` (설계), `docs/ai-memory-data-augmentation-handoff-2026-11-12.md` (Codex 인계 요구)

## Global Constraints

- **언어:** 겉말은 영어로만 쓴다.
- **실행 금지:** 이 패킷 단계에서는 작업자·GPU·유료 API를 실행하지 않는다. 효용(utility) 라벨은 모두 `null`이다.
- **출처:**
  - 파일럿 4세션은 new-a 가족 중 여행 v2 세션에 쓰지 않은 24가족에서 고른다.
  - 본 자료(20세션)는 파일럿 검토 뒤 같은 24가족의 나머지로 만든다.
  - new-b 40가족은 읽지도 쓰지도 않는다.
  - main 첫 자료 24가족(그중 11가족은 Codex 증강 학습)은 쓰지 않는다.
- **판단 요청:** 판단 요청(질문) 문장은 두 겉말에서 같다. 겉말이 달라지는 것은 사건 턴뿐이다.
- **숨긴 겉말:** 숨긴 사건 턴에는 그 함정의 금지어가 없다.
- **드러난 단서 목록:** 드러난 겉말 틀에서만 가져와 미리 고정한다: `cancel`, `cancelled`, `canceled`, `replaces`, `replaced`, `no longer applies`, `has changed plans`.
- **기록 목록:**
  - 기록 본문은 예약 확인과 사용자 발화 원문으로만 쓴다. 예약 확인은 고정 과거 응답에서 만든다.
  - 숨긴 겉말의 기록에는 대본에만 있는 사실(무효 표시 등)을 넣지 않는다.
- **표면 규칙:** "최신 기록만"과 "드러난 표시" 규칙은 기록 본문의 사람 이름(단어 단위 일치)·`day N`·단서 목록만 쓴다.
- **채택 기준:** 모델이나 정책의 성적으로 문항을 고르거나 고치지 않는다.
- **답 형식:** `{"dinner":"ID","accommodation":"ID"}`이다. 값으로 `UNKNOWN`(알 수 없음)과 `NONE`(행동 없음)을 허용한다.
- **기억 예산:** 2,048·8,192 토큰이다. 작업자 토크나이저 `runs/delivery-path/validity-main/9b/conditions-runs/part-01/artifacts/worker-tokenizer/tokenizer.json` 기준이다.
- **길이 목표 (세션 마지막 판단 시점의 전체 이력 입력):** S ≤ 9천, M 2.5만–4만, L 8만–13만 토큰이다.
- **커밋:** 작업 트리 브랜치 `hidden-traps-data`에만 한다. push와 main 병합은 사용자 확인 뒤에 한다.

## Review Focus

1. **숨긴 문장의 모호성:** 꼼꼼한 사람이 읽어도 대본의 사건으로 읽히지 않을 수 있다. 숨긴 틀마다 "뜻하는 사건" 주석을 달고 모두 검토표에 싣는다. Task 6 `test_every_hidden_template_in_review_packet`이 확인한다.
2. **닮은 이름의 표면 키 오인:** "Ana"가 "Anna" 기록에 걸리면 안 된다. Task 4 `test_surface_key_whole_word`가 확인한다.
3. **조건 함정의 정답 집합:** 정답 집합이 비거나 메뉴 전체가 되면 함정이 아니다. Task 1 `test_constraints_bite`, Task 3 `test_acceptable_nonempty_and_not_all`이 확인한다.
4. **고정 과거의 자유 선택이 이후 참조됨:** 자유 선택 예약(`open_book`)이 나중에 참조되면 정답이 고정 과거의 임의 선택에 묶인다. Task 1 `test_open_book_never_referenced`가 확인한다.
5. **잡음 턴의 충돌 사실:** 길이를 늘리는 잡음이 예약 사실과 부딪히면 안 된다. Task 5 `test_noise_has_no_conflicting_facts`가 확인한다.

---

### Task 0: 작업 트리와 출처 목록

**Files:**
- Create: `experiments/hidden_traps/__init__.py`, `experiments/hidden_traps/inventory.py`, `experiments/hidden_traps/tests/__init__.py`, `experiments/hidden_traps/tests/test_inventory.py`

**Interfaces:**
- Produces: `inventory.build_inventory() -> list[dict]`. 원천 120가족마다 한 행이다. 키는 `family_id, source_split, city, group('original'|'new-a'|'new-b'), prior_use: list[str], role('pilot'|'main'|'sealed'|'not_used')`이다.
- Produces: `inventory.families(role: str) -> list[dict]`. 해당 역할 가족의 원천 행을 돌려준다. `delivery_path.families.load_source()`의 형식을 따른다.

- [ ] **Step 1: 작업 트리 만들기**
  - `git -C /home/depa/ai-memory worktree add .claude/worktrees/hidden-traps -b hidden-traps-data worktree-delivery-path-experiment`를 실행한다.
  - 그다음 EnterWorktree(`path`)로 들어간다.
  - 토크나이저 파일을 `runs/hidden-traps/worker-tokenizer/`로 복사한다. 이 폴더는 git 무시 대상이다.
- [ ] **Step 2: 실패하는 테스트 작성** (`test_inventory.py`)
  - `test_roles_counts`: pilot 4, main 20, sealed 40
  - `test_no_overlap_with_used_sources`: pilot·main ∩ (Codex 증강 `family_id` 11개 ∪ 여행 v2 28세션 가족 ∪ main 첫 자료 24) = ∅
  - `test_prior_use_recorded`: pilot·main 모두 `'delivery-path-stage1'`을 포함
- [ ] **Step 3:** `python3 -m unittest discover -s experiments/hidden_traps/tests -t . -v` → 실패(모듈 없음)
- [ ] **Step 4: `inventory.py` 구현**
  - **원천 읽기:** source-specifications.zip, `datasets/memory_need_sources/20261111-v1/families.json`, `datasets/memory_state_augmentation/20261112-v1/cases.jsonl`, 배달 경로 `travel/sessions.jsonl`이다. 앞의 둘은 `delivery_path.families`의 경로를 쓴다.
  - **역할 배정:** new-a 중 여행 v2에 없는 24가족을 `sha256(VERSION|family_id)` 순으로 정렬한다. 앞 4개를 pilot, 나머지를 main으로 둔다.
- [ ] **Step 5:** 같은 명령 → 통과
- [ ] **Step 6:** 커밋 `Add the source inventory for the hidden-trap data`

### Task 1: 대본 생성 (`script.py`)

**Files:** Create `experiments/hidden_traps/script.py`, `tests/test_script.py`

**Interfaces:**
- Produces: `script.build_script(family: dict, catalog: dict) -> dict`
  - 키: `session_id, version, family_id, city, menu, people, lookalike_pair: [str, str], preferences: {person: rule}, turns: list[dict]`
  - 턴 키: `index, kind, sitting(1–4), person, day`. 종류에 따라 `places`, `target`, `fallback`, `first_void`, `rule`, `trap`이 붙는다.
- Produces: `script.rule_allows(rule: dict, entry: dict) -> bool`
- 턴 종류:

  | 분류 | 종류 |
  |---|---|
  | 사건 | `book`, `change`, `void`, `complement`, `preference`, `exception_book` |
  | 판단 요청 | `ref`, `ref_changed`, `ref_conditional`, `ref_void`, `open_book`, `history`, `ref_lookalike`, `absent` |

- 상수:
  - `VERSION = 'hidden-traps-20261112-v1'`
  - `TRAPS = ('void','complement','exception','constraint','history','lookalike','absent')`
  - 기본 구성:
    - 예약자 5명(닮은 이름 한 쌍 포함) × 4일 = 예약 20
    - 사건: change 3, void 3, complement 2, preference 2
    - 판단 요청: ref_conditional 2(둘 다 first_void), ref_void 1, ref 4(near 2, far 2), ref_changed 2, open_book 4(예외 뒤 2, 조건 뒤 2), history 2, ref_lookalike 2, absent 2
  - 거리 규칙은 `validity_specs`를 따른다. 더하는 규칙: preference 턴은 첫 10턴 안에 오고, 그 사람의 open_book은 15턴 이상 뒤에 온다. complement 뒤의 참조는 2턴 이상 뒤에 온다.
- 예약 장소는 `validity_specs._places`와 `_usable`로 배정한다.

- [ ] **Step 1: 실패하는 테스트**
  - `test_every_trap_at_least_twice`
  - `test_deterministic`: 같은 가족이면 같은 대본
  - `test_order_rules`
  - `test_lookalike_pair_present`: 두 이름 모두 판단 요청 전에 예약이 있음
  - `test_constraints_bite`: 각 preference 규칙에 대해 해당 종류의 메뉴에 허용·불허가 각각 1개 이상
  - `test_open_book_never_referenced`
- [ ] **Step 2:** 실행 → 실패
- [ ] **Step 3: 구현**
  - **조건 규칙 후보:** 식당 cuisine 제외(예: Seafood), 숙소 `No pets` 회피, `No children under 10` 회피, 1박 가격 상한(메뉴 숙소 가격 중앙값)
  - **규칙 선택:** 메뉴에서 허용·불허가 모두 생기는 첫 후보를 digest 순으로 고른다.
  - **예외 예약:** exception_book은 그 사람의 규칙을 어기는 장소로 예약한다.
- [ ] **Step 4:** 실행 → 통과
- [ ] **Step 5:** 커밋 `Generate hidden-trap session scripts`

### Task 2: 겉말 (`surface.py`)

**Files:** Create `experiments/hidden_traps/surface.py`, `tests/test_surface.py`

**Interfaces:**
- Produces:
  - `surface.CUES: tuple[str, ...]` (Global Constraints의 7개)
  - `surface.FORBIDDEN: dict[str, tuple[str, ...]]`
  - `surface.turn_text(turn: dict, script: dict, catalog: dict, surface: str) -> str` (`surface` ∈ `'explicit'|'hidden'`)
  - `surface.forbidden_hits(text: str, kind: str) -> list[str]`
  - `surface.HIDDEN_TEMPLATES: dict[str, list[dict]]`. 각 틀은 `{'id', 'text', 'implies'}`이고 `implies`는 뜻하는 사건을 사람이 읽을 문장으로 적는다.
- **금지어:**
  - `void`: CUES + `change`, `changed`, `drop`, `dropped`, `no longer`, `off`, `void`
  - `complement`: CUES + `change`, `changed`, `instead of`
- **숨긴 틀:** void, complement, preference, exception_book마다 3개 이상 쓴다.
- **판단 요청 문장:** 두 겉말에서 같다. 조건부 요청은 "if X is no longer using that booking"처럼 중립적으로 쓴다.
- **드러낸 겉말:** void는 `validity_specs.request_text`의 cancel 문장 형식이다.

- [ ] **Step 1: 실패하는 테스트**
  - `test_hidden_has_no_forbidden_words`: 파일럿 4세션의 모든 숨긴 사건 턴
  - `test_explicit_void_has_cue`
  - `test_requests_identical_across_surfaces`
  - `test_no_catalog_ids_in_user_text`
  - `test_templates_have_implies`
- [ ] **Step 2:** 실행 → 실패
- [ ] **Step 3: 구현.** 틀은 digest로 고른다. 장소는 이름으로만 말하고 ID는 쓰지 않는다.
- [ ] **Step 4:** 실행 → 통과
- [ ] **Step 5:** 커밋 `Render explicit and hidden surfaces`

### Task 3: 상태 재생과 라벨 (`labels.py`)

**Files:** Create `experiments/hidden_traps/labels.py`, `tests/test_labels.py`

**Interfaces:**
- Consumes: Task 1의 대본
- Produces:
  - `labels.memory_id(script: dict, index: int) -> str` (`f'{session_id}-t{index:02d}'`)
  - `labels.scripted_answer(script: dict, catalog: dict, index: int) -> dict`
    - book·change: 이름 붙은 ID
    - 드러낸 void(cancel): 취소된 ID
    - 숨긴 void, complement, preference: `NONE`
    - 판단 요청: 정답 집합의 카탈로그 순 첫 값
  - `labels.label(script: dict, catalog: dict, index: int) -> dict`
    - 키: `trap, needs_memory, acceptable: {slot: [ids]}` (UNKNOWN이면 `['UNKNOWN']`), `required_memory_ids, invalid_memory_ids, distractor_memory_ids, evidence_memory_ids`
    - `candidate_relations: {memory_id: {'relation': 'needed'|'invalid'|'distractor'|'irrelevant', 'judge_with': [memory_id]}}`
    - `ambiguous: bool, utility: None`
- `judge_with`는 그 후보의 상태를 판정하려면 함께 봐야 하는 다른 기록이다. 예: 무효가 된 예약의 판정에는 void 턴이 필요하다. Codex의 "보이지 않는 기록" 요구에 답하는 필드다.

- [ ] **Step 1: 실패하는 테스트** (함정마다 손으로 만든 작은 대본)
  - `test_void_conditional_uses_fallback`
  - `test_ref_void_is_unknown`
  - `test_complement_keeps_booking`
  - `test_exception_does_not_lift_preference`
  - `test_constraint_filters_acceptable`
  - `test_acceptable_nonempty_and_not_all`
  - `test_history_returns_original`
  - `test_lookalike_counts_other_person_as_distractor`
  - `test_absent_is_unknown`
  - `test_judge_with_lists_invalidating_record`
  - `test_label_uses_only_earlier_turns`
- [ ] **Step 2:** 실행 → 실패
- [ ] **Step 3: 구현.** 턴을 차례로 재생하며 `(person, day)`별 유효 예약·이력·무효 집합·선호 규칙을 유지한다.
- [ ] **Step 4:** 실행 → 통과
- [ ] **Step 5:** 커밋 `Label hidden-trap decisions from the script`

### Task 4: 조건별 입력 (`inputs.py`)

**Files:** Create `experiments/hidden_traps/inputs.py`, `tests/test_inputs.py`

**Interfaces:**
- Consumes: Task 2 `turn_text`, Task 3 `scripted_answer`·`label`
- Produces:
  - `inputs.records(script, catalog, index, surface) -> list[dict]` (`{'memory_id', 'index', 'text'}`)
  - `inputs.surface_key(text: str, people: list[str]) -> tuple[str, int] | None`
  - `inputs.latest_surface(records, people) -> list[dict]`
  - `inputs.marked_surface(records, people) -> list[dict]`
  - `inputs.bm25_budget(records, query: str, budget: int, count_tokens) -> list[dict]`
  - `inputs.messages(script, catalog, index, condition, surface, tier='S') -> list[dict]`
  - `inputs.CONDITIONS = ('none','full_history','sitting_plus_records','record_list','latest_surface','marked_surface','bm25_2k','bm25_8k','needed_only','invalid_removed')`
- **기록 본문:**
  - 예약 확인: "Recorded booking from request k: P, day d. Dinner: NAME [ID X]. Accommodation: NAME [ID Y]."
  - 드러낸 cancel: 지금 형식
  - 그 밖의 사건 턴: "Note from request k: <사용자 원문>"
- **시스템·과제 문장:** `validity.start`의 카탈로그 서문을 쓰고, 과제 문장에 `NONE` 규칙을 더한다.
- **`sitting_plus_records`:** 현재 sitting의 대화와 이전 sitting의 기록 목록이다.
- **`invalid_removed`:** 진단용이다. 상태마다 `latest_surface`와 같은지 계산해 protocol에 기록한다. 같으면 실행 대상에서 뺀다.

- [ ] **Step 1: 실패하는 테스트**
  - `test_record_list_has_no_script_only_facts`: 숨긴 겉말 기록에 CUES·`void` 없음
  - `test_surface_key_whole_word`: Ana/Anna
  - `test_marked_uses_cue_list_only`
  - `test_bm25_respects_budget`
  - `test_no_future_records`
  - `test_inputs_deterministic`
- [ ] **Step 2:** 실행 → 실패
- [ ] **Step 3: 구현.** BM25는 표준 식(k1=1.2, b=0.75)으로 직접 짠다. 토큰 수는 주입된 `count_tokens`로 센다. 테스트에서는 공백 분할 대용을 쓴다.
- [ ] **Step 4:** 실행 → 통과
- [ ] **Step 5:** 커밋 `Build condition inputs over a fixed scripted past`

### Task 5: 길이 단계 (`tiers.py`)

**Files:** Create `experiments/hidden_traps/tiers.py`, `tests/test_tiers.py`

**Interfaces:**
- Produces:
  - `tiers.render_turns(script, catalog, tier: str) -> list[dict]`
    - S는 대본 턴 그대로다.
    - M·L은 예약·자유 선택 턴 앞에 검색 결과 블록(카탈로그에서 결정적으로 만듦)을 넣고, 잡음 턴(`kind='noise'`, 응답 `NONE`)을 끼운다.
  - `tiers.NOISE_BANK: list[str]`
  - `inputs.messages`는 tier에 따라 이 턴 목록을 쓴다.
- **단계별 내용:**
  - M: 메뉴 요약 검색 결과와 sitting마다 잡음 3턴
  - L: 메뉴 전체 속성·가격 검색 결과와 sitting마다 잡음 10턴. 일부 잡음에는 닮은 이름을 넣는다.

- [ ] **Step 1: 실패하는 테스트**
  - `test_tiers_share_labels`: 세 단계의 판단 라벨이 같음
  - `test_noise_has_no_conflicting_facts`
  - `test_tier_lengths_in_range`: main `.venv`의 tokenizers가 없으면 skip
- [ ] **Step 2:** 실행 → 실패
- [ ] **Step 3: 구현.** 길이 목표를 맞추려고 검색 결과·잡음 양을 조정하는 것은 허용한다. 이것은 모델 결과가 아니다.
- [ ] **Step 4:** `/home/depa/ai-memory/.venv/bin/python -m unittest discover -s experiments/hidden_traps/tests -t . -v` → 통과
- [ ] **Step 5:** 커밋 `Render S, M and L context lengths`

### Task 6: 패킷 내보내기와 검증 (`packet.py`, `build.py`)

**Files:** Create `experiments/hidden_traps/packet.py`, `experiments/hidden_traps/build.py`, `tests/test_packet.py`

**Interfaces:**
- Produces:
  - `packet.build_packet(role: str, out_dir: Path) -> dict`
  - `packet.validate_packet(out_dir: Path) -> dict` (`{'status': 'passed'|'failed', ...}`)
  - CLI: `python -m experiments.hidden_traps.build --role pilot --out experiments/hidden_traps/results/pilot-20261112-v1`
- **파일:**

  | 파일 | 행 단위와 내용 |
  |---|---|
  | `source-inventory.jsonl` | 원천 가족 한 행씩 |
  | `events.jsonl` | 대본 턴. 모델 입력 아님 |
  | `cases.jsonl` | 판단 시점 × 겉말 × 단계. 조건별 messages, 입력 해시, 토큰 수, 후보 은행 |
  | `relations.jsonl` | 위 사례별 라벨. utility null |
  | `contrasts.jsonl` | 드러낸↔숨긴, S↔M↔L, 같은 기록의 무효 전↔후 |
  | `review-packet.jsonl` | 숨긴 사건 문장과 질문 "What does this imply about P's day d booking?", 판단 요청 일부의 기록·요청. 정답 없음 |
  | `review-key.jsonl` | 정답 |
  | `protocol.json` | 버전, 조건, 예산, CUES, 금지어, 토크나이저 해시, `invalid_removed`=`latest_surface` 동일 건수 |
  | `manifest.json` | 해시·크기 |

- **사람 검토 묶음:**
  - 숨긴 틀은 모두 싣는다.
  - 판단 사례는 함정마다 이득·중립이 섞이도록 고르게 뽑는다. 정답은 가린다.

- [ ] **Step 1: 실패하는 테스트** (1세션을 임시 폴더에 만들어 확인)
  - `test_packet_files_and_schema`
  - `test_no_conflicting_labels_for_same_input_hash`
  - `test_review_packet_hides_answers`
  - `test_every_hidden_template_in_review_packet`
  - `test_utility_is_null`
- [ ] **Step 2:** 실행 → 실패
- [ ] **Step 3:** 구현
- [ ] **Step 4:** 실행 → 통과. 이어서 파일럿 4세션 빌드를 실행하고 `validate_packet` 결과가 `passed`인지 확인한다.
- [ ] **Step 5:** 커밋 `Export and validate the pilot packet`

### Task 7: 인계

**Files:** Create `experiments/hidden_traps/README.md`

- [ ] **Step 1: README 작성.** 다음을 적는다.
  - 패킷 구성, 실행 명령, 세션·사례·조건·단계별 건수
  - 숨긴 틀 목록
  - Codex가 확인할 항목: 인계 노트 5절
- [ ] **Step 2: 숫자 확인.** 전체 테스트와 `validate_packet`을 다시 실행한다. README의 숫자가 출력과 같은지 확인한다.
- [ ] **Step 3:** 커밋 `Describe the pilot packet for review`
- [ ] **Step 4: 사용자 확인 후 인계 브랜치.** main 기준 브랜치에 `datasets/hidden_memory_traps/pilot-20261112-v1/`만 담은 인계 브랜치를 만든다. 병합 여부는 사용자가 정한다.
