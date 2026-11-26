# 프레임워크 개선안: 11월 25일 이후 τ³·BFCL 작업 기준

2026-11-26 오전에 11월 25일 00시 이후의 커밋·실행 기록·프로토콜·보고서를 원기록과 코드로 다시 확인하고, 그 결과로 개선 순서를 제안한다. 이전 실험은 사용자가 결론 근거에서 제외했으므로 진단에 쓰지 않았다. 확인에는 모델 호출을 쓰지 않았다. 이 문서는 커밋·push·모델 호출·예산을 승인하지 않는다.

판단 기준은 두 가지다.

1. 1회차와 2회차는 기억과 기억 도구만 다르다.
2. 실행·채점 코드는 실험 결과만큼 중요한 산출물이다. 새 기억 정책은 새 실행 스크립트 없이 사양과 플러그인으로 붙고, 같은 점수표로 기존 정책과 비교돼야 한다. [보완 계획의 목표 상태](../development/framework-scoring-plan.md)

| 확인 대상 | 11월 26일 오전 기준 |
| --- | --- |
| 커밋 | 11월 25일 이후 37개, 브랜치 7개. 모두 로컬에만 있다. |
| 작업 트리 | `main`(`6025700`) 위에 미커밋 변경 222건 |
| 실행 디렉터리 | τ³ 9개, BFCL 13개 |
| 프로토콜 | 14개(τ³ 6, BFCL 7, τ² 항공 초안 1) |
| 기준 보고서 | [교차 벤치 점검](20261126-cross-benchmark-platform-check.md), [은행 세 층 리뷰 재검토](20261125-banking-framework-review.md) |

## 요약

- **지켜지는 것.** 1회차와 2회차는 기억과 기억 도구만 다르다. τ³·BFCL 2회차 262회와 τ³ 고정 판단 요청 416건에서 확인했다. 보고서 결과 18개도 바이트 단위로 재현된다.
- **안 되는 것 1. 선언과 실행이 연구마다 갈라진다.** 11월 25일 이후 새 실행기가 8개, 최상위 키 구성이 모두 다른 프로토콜이 14개 생겼다. 새 코드의 약 86%가 특정 연구 전용이다.
- **안 되는 것 2. 정책을 바꿔 끼울 자리가 거의 없다.** 무엇을 줄지 고르는 단계가 없고, 자율 조회는 모든 연구에서 0회다. 지금의 on_demand는 '제공했지만 읽지 않은' 조건이다.
- **위험. 원본이 이 컴퓨터에만 있다.** origin에는 11월 24일의 `main`만 있다.

## 확인한 것

| 항목 | 결과 |
| --- | --- |
| 회차 격리 | 2회차 첫 요청을 1회차 원요청과 원기록에서 대조했다. τ³ 110회(result-feedback 40, policy-check 40, multitarget 30)와 고정 판단 요청 416건, BFCL 152회(relevance 80, 통제 56, 도구 설명 16)에서 차이는 기억 부분뿐이었다. 시스템 끝의 기억 안내, 1번 위치의 메모 메시지, 기억 도구 두 개, 그리고 BFCL 강제 조건 32회의 `tool_choice`(의도한 개입)다. 모델·공급자·추론 설정과 τ³ 고객 시뮬레이터의 시스템 프롬프트·파라미터·도구도 같았다. |
| 재현 | 보고서 결과 18개(τ³ 10, BFCL 8)를 현재 작업 트리에서 다시 만들어 공개 파일과 비교했고 모두 바이트 단위로 같았다(부록). 커밋만 내보낸 사본 `4e7def5`·`02e7d61`·`8162116`에서도 해당 결과가 같았다. |
| 같은 시작 | `runner/task_start.open_task_start`와 `TaskStartMismatch`를 τ² 어댑터와 BFCL 어댑터가 함께 쓴다. |
| 공통 짝비교 | `analysis/paired_trials.py`와 `schemas/paired-outcomes-v1.schema.json`을 BFCL relevance, τ³ multitarget, policy-cycle 분석이 함께 쓴다. |
| 검증 | 기본 검증 3개가 통과했다. 전체 단위 테스트 663개는 09:32에 통과했고, 그 뒤 바뀐 BFCL·기억 도구 관련 세 모듈의 테스트 26개도 다시 통과했다. |

## 정해 둔 순서와 그 뒤의 진척

실험 에이전트는 11월 25일 23:54에 [은행 세 층 리뷰 재검토](20261125-banking-framework-review.md)에서 네 단계의 순서를 정했다. 이 순서에는 동의한다. 다만 그 뒤의 실행은 모두 BFCL이었고(실행 디렉터리 12개, 대부분 조회 경로 진단), 네 단계는 거의 그대로다.

| 순서 | 지금 | 근거 |
| --- | --- | --- |
| 1. 복원 가능한 원본 | 그대로 | origin에는 `main`(`6025700`)만 있다. 11월 25일 이후 커밋과 `runs/`·`data/`·`.local/`은 이 컴퓨터에만 있다. |
| 2. 작은 공통 사양 | 그대로 | 새 프로토콜 14개의 최상위 키 구성이 14가지다. 반복은 여전히 별도 단계로 만든다. |
| 3. 공통 분석기에 행동 연결 | 일부 | `paired_trials` 사용처 3곳과 τ³ 첫 시도 증거(`adapters/tau2_assessment.py`)가 생겼다. 고객 도구 호출과 단계별 DB 상태는 아직 원장에 없다. |
| 4. 복구 정책 | 그대로 | `models/openrouter.py`는 `6025700` 이후 바뀌지 않았다. τ² 어댑터는 `num_retries=0`으로 보낸다. |

## 개선안

아래 순서는 위 네 단계를 따르되, 확인에서 드러난 빈 곳(실행기, 결과 기록, 정책 층)을 끼워 넣은 것이다. 완료 기준은 모두 모델 호출 없이 확인할 수 있게 잡았다.

### 1. 원본을 이 컴퓨터 밖에서도 복원할 수 있게 한다

지금 상태:

- origin에는 `main`(`6025700`, 11월 24일)만 있다. 11월 25일 이후 커밋 37개는 브랜치 7개에 흩어져 로컬에만 있다.
- 가장 많이 합쳐진 코드는 커밋이 아니라 작업 트리에 있다. 예를 들어 `memory/router.py`의 native 기억 라우팅은 어느 커밋에도 없다. 브랜치마다 서로 다른 부분만 갖고 있다.
- 재현에 필요한 `runs/`, `data/`(τ² 체크아웃), `.local/`(BFCL 체크아웃 `bfcl-6ea5797`)은 Git에서 제외돼 있고 보관처가 문서에 없다.

제안:

- 작업 트리를 주제별 커밋으로 정리해 한 브랜치에 합친다. 다른 세션과 같은 트리를 쓰므로 정리 시점을 맞춘다.
- push와 보관처는 사용자가 정한다.
- 11월 25일 이후 실행 22개, 이들이 읽는 이전 원천(예: `tau-banking-memory-01`, `tau-banking-required-01`), 두 벤치 체크아웃을 해시 목록과 함께 보관처로 옮긴다. 해시 목록은 보관처와 따로 둔다.

완료 기준: 빈 작업 공간에서 push된 커밋과 보관본만으로 부록의 18개 결과가 바이트 단위로 같게 나온다.

### 2. 회귀 기준을 지금 연구로 옮긴다

지금 상태:

- [보완 계획](../development/framework-scoring-plan.md)의 0·1·3단계 완료 기준은 소매 두 번 방문, 주문 선택, 항공 실험이다. 모두 사용자가 폐기로 정한 이전 실험이다.
- 고정 재현 목록(`protocols/reproductions/`)은 11월 18일 이후 바뀌지 않았다.
- 보고서 명령이 원 작업 공간을 암묵적으로 가정한다. `analyze_banking_memory --form-repeats`는 `--workspace`가 없으면 현재 디렉터리의 `data/tau2-bench`를 Git 체크아웃으로 읽는다. 그래서 커밋만 내보낸 사본에서는 `--workspace`로 원 작업 공간을 줘야 돌았다.

제안:

- 부록의 18개를 고정 재현 목록에 등록하고, 공용 코드를 바꿀 때마다 돌린다. 옛 항목은 고치지 않고 그대로 둔다.
- 등록 항목마다 작업 공간, 벤치 체크아웃, 입력 실행의 경로와 해시를 적는다.
- 새 연구는 보고서를 낼 때 자기 재현 명령을 이 목록에 더한다.

완료 기준: 깨끗한 체크아웃에서 18개가 통과한다. 입력이 없으면 무엇이 없는지 알려 주고 멈춘다.

### 3. 실험 사양을 하나로 만든다

지금 상태:

- 11월 25일 이후 프로토콜 14개의 최상위 키 구성이 모두 다르다. τ³ 6개는 `format` 이름도 6가지이고, BFCL 7개 중 6개에는 `format` 키가 없다.
- 반복을 선언할 수 없다. 그래서 multitarget은 `solve-repeat-2` 단계를 따로 만들었다.
- 대조 조건도 선언할 수 없다. `experiments/matched_trial.py`의 `verify_trial`은 원천이 있는데 기억이 꺼진 시행을 거부한다(`policy.enabled != (first is not None)`). 빈 메모는 작성 은행이 있어야 만들 수 있다.
- 과제 적격 규칙이 실행 스크립트 안에 있다. `scripts/run_banking_memory.py`의 `stratum()`은 초기 상태가 있는 과제를 뺀다. 11월 25일 확인에서는 97개 중 22개가 남았다.

제안: 이름과 버전이 하나인 사양 형식에 다음을 적는다.

| 항목 | 내용 |
| --- | --- |
| 벤치와 과제 | 어댑터 이름·버전, 과제 목록, 적격 규칙과 그 버전 |
| 역할별 모델 | 작업자·작성자·고객 시뮬레이터의 경로, 파라미터, seed 유무 |
| 단계 | `source`·`write`·`solve`와 추가 단계의 순서 |
| 조건 | 기억 정책 필드의 조합(7번)과 은행 변형(실제·빈) |
| 반복 | 반복 수, 반복 번호를 배정 ID에 넣는 규칙 |
| 대조 | 선언하면 도는 대조 조건(8번). 기본 배정에 넣을지는 사용자가 정한다. |
| 허용 입력 차이 | 시스템 끝 안내, 삽입 메시지 위치, 추가 도구, `tool_choice` |
| 지표 | 등록된 지표의 이름·버전·분모·군집 단위 |
| 한도와 장애 | 비용 한도, 재시도 규칙(5번), 결측 규칙 |

완료 기준: τ³ multitarget(반복 포함)과 BFCL relevance two-pass를 새 사양으로 다시 쓴다. 해석기가 만든 배정 목록(과제·조건·반복·입력 해시)이 기존 등록과 같다. 그 뒤의 새 프로토콜은 이 형식만 쓴다.

### 4. 단계 실행기를 하나로 만들고 격리를 송신 전에 검사한다

지금 상태:

- 11월 25일 이후 새 실행기가 8개다. τ³ 5개는 `scripts/run_banking_*.py`(policy_cycle, scoped_writer, scoped_decisions, replacement, form_repeats), BFCL 3개는 `runner/bfcl_relevance.py`, `runner/bfcl_read_check.py`, `runner/bfcl_memory_check.py`다.
- τ³는 `run_banking_*` → `run_banking_memory` → `run_fresh_matched` → `scripts/run_full_retail.py`의 `run_study`로 이어진다. 단계 루프가 소매 이름의 스크립트 안에 있다. BFCL은 자기 루프를 갖는다.
- 스크립트 150개 중 88개가 다른 스크립트를 가져온다. `analyze_banking_policy_cycle`과 `analyze_banking_replacement`는 서로를 가져온다.
- 패키지에 명령 진입점이 없고(`pyproject.toml`), src 파일 27개가 저장소 경로(`ROOT`)를 참조한다.
- 격리는 두 곳에서 확인한다. 같은 시작은 송신 전에 막고(`open_task_start`), 첫 요청 전체가 기억 부분만 다른지는 실행 뒤 감사(`analysis/requests.audit_requests`와 연구별 감사)로 본다.

제안:

- `run_study`의 단계 루프(배정 등록, 단계별 영수증, 누적 승인, 중복 전송 방지, 재개)를 `src/agent_memory_eval/runner/`로 옮긴다. 벤치마다 다른 부분은 어댑터가 맡는다. 과제 시작 열기, 한 회 실행, 공식 채점, 기억이 들어가는 자리다.
- BFCL 루프를 같은 어댑터 인터페이스의 두 번째 구현으로 옮긴다.
- 사양의 허용 입력 차이로 송신 직전에 검사한다. 2회차 첫 요청에서 허용한 기억 부분을 뺀 나머지가 1회차 원요청과 다르면 보내지 않고 이유를 원장에 남긴다. 지금 사후 감사가 매번 확인하는 것을 구조로 보장하는 것이다.
- 명령 진입점(상세 설계 14절의 `plan validate`·`run`·`resume`·`analyze`)을 두고 작업 공간을 인자로 받는다.

완료 기준: 기록된 응답을 차례로 돌려주는 가짜 모델로 τ³ 연구 하나와 BFCL 연구 하나를 새 실행기로 다시 돌렸을 때 요청 해시가 원기록과 같다. 기억 외 차이를 넣은 요청은 송신 전에 거부된다. 부록 18개는 그대로 통과한다.

### 5. 일시 장애 복구를 실행기 기능으로 넣는다

지금 상태:

- OpenRouter 클라이언트는 Retry-After를 기록하지만 다시 보내지 않는다. τ² 어댑터는 `num_retries=0`으로 보낸다.
- 11월 25일 replacement 연구(036)는 429 한 번으로 21칸 중 20칸을 잃었다.
- BFCL은 자체 복구를 따로 만들었다(`reuse_generations`, `-recovery`·`-decoded` 실행 디렉터리).

제안: 실험 에이전트의 4단계를 실행기 기능으로 만든다.

- 사양에 재시도 대상(완료 응답이 없는 429·503·연결 끊김), 대기(Retry-After 우선, 없으면 등록한 간격), 시도 상한을 적는다.
- 시도마다 영수증과 비용을 같은 배정에 잇는다. 완료 응답을 받은 요청은 다시 보내지 않는다. 전송 여부를 모르는 요청과 업무 도구의 중복 실행은 따로 표시한다.
- 상한을 넘으면 결측으로 남기고 분모에서 빼지 않는다.

완료 기준: 가짜 게이트웨이로 429, 503, 전송 여부 불명, 입력 크기 초과를 넣은 장애 묶음이 통과한다. 429 한 번은 그 요청의 재시도로 끝나고 다른 칸에는 영향이 없다.

### 6. 결과 기록에 반복 번호와 기억 경로를 넣는다

지금 상태:

- `paired_trials`의 키가 (과제, 칸)이라 같은 칸을 반복하면 "Duplicate policy allocation"으로 거부한다.
- 결과 기록에 기억이 실제로 어디까지 갔는지가 없다. `memory.returned` 이벤트는 저장소의 반환을 세므로 τ³에서는 작성 수(20·40·20)와 같았다. 읽기 횟수로 쓰면 안 된다. BFCL의 '본문이 다음 판단 입력에 들어갔는가'는 `runner/bfcl_read_check.py` 안에서만 계산한다.
- `analysis/outcome_report.py`의 `READERS`가 지표 5개를 코드에 고정한다. 새 지표마다 코드를 고쳐야 한다.
- τ³ 공식 ACTION 채점은 맞는 호출이 한 번이라도 있으면 성공으로 본다. 첫 시도 증거는 생겼지만 공통 보고 지표 5개에는 없다. 11월 25일 확인에서 과제 008의 2회차 성공 24/29 중 16건은 잘못된 첫 이관 사유 뒤의 성공이었다.

제안:

- 결과 기록에 반복 번호(기본 0)를 넣고 키를 (과제, 칸, 반복)으로 바꾼다. 반복을 짝짓는 방식(같은 번호끼리, 반복 평균)은 지표 정의에 적는다.
- 기억 경로 필드를 둔다. 제공 방식, 목록 노출과 항목 수, 본문 요청·반환 ID, 판단 직전 요청에 들어간 기억 ID, 첫 조회 시점이다. [AGENTS.md](../../AGENTS.md)의 '목록 제목도 기억 노출이며 반환과 입력 포함을 구분한다'를 필드로 옮기는 것이다.
- 지표는 사양에서 이름으로 등록하고 보고기는 등록된 지표를 읽는다. τ³ 첫 시도 성공을 공식 점수 옆의 기본 지표로 등록한다.

완료 기준: 지금의 세 사용처가 반복 0으로 같은 결과를 낸다. multitarget 반복이 별도 단계 없이 같은 표에 들어간다. BFCL 본문 포함과 τ³ 노출이 같은 필드로 집계된다.

### 7. 기억 정책을 바꿔 끼울 자리를 만든다

지금 상태:

- `contracts/core.py`의 `MemoryPolicy`는 전달 방식만 담는다. 사용 여부, 작성 시점, 같은 회차 읽기, 페이지 크기, 읽기 방식, 항목·바이트 한도다.
- 무엇을 줄지 고르는 단계가 없다. `memory/preparation.py`의 `snapshot_items`는 저장된 순서대로 전부 돌려준다.
- preload는 τ³와 BFCL 모두 1번 위치의 사용자 역할 메시지에 `{"items": ...}` JSON으로 들어간다. 표현을 바꿀 자리가 없다.
- `policies/`에는 11월 26일 10시 48분 커밋에서 `writer_pairs.py`가 처음 생겼다. 작성 지시에 문구 하나를 더한 조건 쌍을 만드는, 벤치와 무관한 함수다. 지금은 BFCL 실행기만 쓴다.
- 그래서 지금 비교할 수 있는 것은 사실상 '메모 형식 × 제공 방식'이다. 무엇을 남기고, 무엇을 주고, 언제 찾아볼지는 연구마다 스크립트와 안내 문구로 바뀐다.
- 자율 조회가 한 번도 일어나지 않았다.

| 연구 | 조건 | 기억 도구를 스스로 부른 시행 |
| --- | --- | ---: |
| τ³ result-feedback | on_demand | 0/20 |
| BFCL relevance two-pass | on_demand | 0/40 |
| BFCL 읽기 인터페이스 v2 | 자유 선택 | 0/8 |
| BFCL 조회 개시 | 이 질문의 메모가 있다고 명시 | 0/8 |
| BFCL 도구 설명만 | 기존 설명과 구체화한 설명 | 0/16 |

첫 호출을 목록 도구로 지정하면 16회 모두 본문까지 읽었다. 하지만 업무 함수 호출 여부 정답은 7/8에서 4/8과 5/8로 내려갔다. 8문항이라 일반화할 수는 없다. 지금의 on_demand는 '제공했지만 읽지 않은' 조건이다. 조회 정책을 비교하려면 먼저 조회가 일어나는 조건이 있어야 한다.

제안:

- 정책 인터페이스를 세 개 둔다.
  - 저장: 작성 시점, 형식, 작성 지시, 필수 여부. `writer_pairs`가 첫 구현 후보다.
  - 선택: 무엇을 줄지. 첫 구현은 지금 동작인 '전체, 저장 순서', 두 번째는 '없음'(빈 메모 대조)이다.
  - 조회 개시와 표현: preload, on_demand, 첫 목록 지정, 존재 안내, 메모의 역할·위치·형식.
- 정책마다 이름과 버전을 `run.started`와 결과 기록에 남긴다.
- 선택 정책의 구체안(BM25, 2k 배분 등)은 기본값으로 넣지 않고 실험 계획에서 선언한다.
- 조회 개시율을 1차 보고 지표로 둔다. 조회가 0인 조건의 성과 차이는 기억 내용의 효과로 해석하지 않는다는 규칙을 보고 형식에 넣는다.

완료 기준: 지금의 τ³·BFCL 연구가 '전체, 저장 순서' 선택 정책으로 같은 요청 해시를 낸다. '없음' 선택 정책이 사양만 바꿔서 돈다.

### 8. 대조 조건과 고객 재생을 선언할 수 있게 한다

지금 상태:

- 11월 25일 이후 2회차 무기억 실행은 0회다. 2회차 무기억을 기본 배정에서 빼기로 한 것은 사용자의 11월 23일 결정이고, 실험 에이전트의 순서도 이를 따른다.
- τ³에서는 작업자와 고객 시뮬레이터가 모두 표본을 뽑고, 고객 시뮬레이터에는 seed가 없다. 같은 시작은 고객의 첫 발화까지만 1회차를 재생한다. 그 뒤에는 작업자 응답이 1회차와 같아도 고객이 응답을 새로 뽑는다.
- 그래서 짝 차이 가운데 기억과 무관한 몫을 연구 안에서 잴 기준이 없다. 지금 쓸 수 있는 기준은 '조회하지 않은 on_demand' 짝이다. 첫 요청이 같았고, 11월 25일 result-feedback에서 20쌍 중 5쌍이 뒤집혔다. 과제가 4개라 일반 잡음 수준으로 쓸 수는 없다.
- BFCL은 greedy 로컬 모델이라 같은 입력이 같은 출력을 낸다. 그래서 1회차가 정확한 A/A 기준이 된다.

제안:

- 다음 대조를 선언만 하면 돌릴 수 있게 한다. 무엇을 실제로 돌릴지와 비용은 사용자가 정한다.
  - 2회차 무기억
  - 안내와 도구는 같고 메모만 빈 조건(7번의 '없음' 선택 정책)
  - 같은 메모 재실행(반복)
- 고객 재생 옵션을 둔다. 고객 입력이 1회차와 바이트 단위로 같은 동안에는 1회차의 고객 응답을 재생한다. 같은 시작을 대화 중간까지 늘려, 고객 쪽 표본 차이를 기억 차이와 떼어 낸다.
- 처음 갈라진 행위자와 위치를 원장에 남기고, 보고서마다 쓴 잡음 기준과 그 분모를 적는다.

완료 기준: 세 대조가 사양 한 줄로 배정에 들어간다. 가짜 고객으로 돌렸을 때 재생 구간과 첫 분기 지점이 원장에 남는다.

### 9. 증거 공백을 메운다

11월 25일 확인에서 남은 항목이다. 첫 항목은 오늘 코드에서 다시 확인했고, 나머지는 다시 확인하지 않았다.

- 고객 도구 호출이 원장에 없다. `adapters/tau2_memory.py`의 `execute_tool_calls`는 기억 묶음이 아니면 원래 실행으로 바로 넘긴다. 11월 25일 확인에서 상담 167건 중 27건의 호출 44회가 보고서 blob에만 있었다. 이 파일은 `6025700` 이후 바뀌지 않았다.
- DB 해시가 시작과 끝에만 있다. 처음 올바른 호출과 중간의 잘못된 변경을 원장만으로 증명할 수 없다.
- 은행 검색 응답의 순위·문서 ID·점수를 사후에 다시 만든다.
- 고객 ID를 기록하지 않고 겹침을 과제 ID로만 검사한다. 11월 25일에 개발 015, 검증 014, 사례 036이 같은 고객(`890389b165`)으로 확인됐다.

제안: 업무 도구 호출을 행위자와 함께 모두 원장에 남기고, 상태를 바꾸는 호출마다 전후 DB 해시를 남긴다. 검색 응답의 원래 순위·ID·점수를 요청에 잇는다. 배정에 고객 ID를 넣고 겹침을 고객 단위로 검사한다.

완료 기준: 실험 에이전트 3단계의 첫 검증대로, 과제 008의 24 대 8과 다중 거래의 DB를 원장만으로 다시 계산한다.

### 10. 공용화가 되돌아가지 않게 한다

- 새 연구는 실행기를 새로 만들지 않는다. 사양과 어댑터로 표현할 수 없으면 그 부족분을 공통 실행기에 먼저 넣는다. 탐색 진단은 스크립트로 둘 수 있지만 보고서에 탐색이라고 적는다.
- 두 번째 연구가 쓰는 함수는 `src/`로 옮긴다(보완 계획 5단계의 규칙).
- `scripts/check_repository.py`에 상한 검사를 둔다. 다른 스크립트를 가져오는 스크립트 수와 `format` 없는 프로토콜 수가 늘면 실패하게 한다.
- 아래 값을 보고서마다 다시 센다.

| 지표 | 11월 26일 값 |
| --- | ---: |
| 다른 스크립트를 가져오는 스크립트 | 88 / 150 |
| 11월 25일 이후 새 실행기 | 8 |
| 11월 25일 이후 프로토콜의 서로 다른 최상위 키 구성 | 14 / 14 |
| 11월 25일 이후 새 비시험 코드 중 연구 전용 | 약 86% |
| 고정 재현 목록의 마지막 변경 | 11월 18일 |

첫 행은 모든 가져오기 형태를 센 값이다. 11월 23일에 같은 기준으로 센 값은 62 / 119였다. 연구 전용 비율은 파일 이름과 위치로 나눴고, 시험 코드를 뺀 6,554줄 중 5,650줄이다.

## 사용자가 정할 것

1. 커밋 정리와 push, `runs/`·`data/`·`.local/` 보관처(1번)
2. 공통 사양과 실행기(3·4번)가 생길 때까지 새 연구를 멈출지, 10번 규칙을 지키는 조건으로 계속할지
3. BFCL 작업자. AGENTS.md는 Luna 작업자를 유지하라고 한다. 11월 26일 BFCL 연구는 greedy 로컬 Qwen3.5-4B를 썼다. 개발용 계측기(정확한 A/A)로만 둘지, 본 비교에도 쓸지 정해야 한다.
4. 8번 대조 가운데 무엇을 실제로 돌릴지와 그 비용

## 하지 않는 것

- 원본 실행, 과거 실험, 과거 lock 해시, `historical-v1.json`을 고치지 않는다.
- BM25나 임의의 2k 배분을 기본값으로 넣지 않는다.
- 2회차 무기억을 기본 배정에 다시 넣지 않는다. 선언할 수 있게만 만든다.
- 공용화와 재현 통과를 기억 효과의 실증으로 보고하지 않는다.

## 부록: 회귀 명령 18개

저장소 루트에서 bash로 실행한다. 모델 호출은 없다. 11월 26일 작업 트리에서 18개 모두 `same`을 출력했다. `--form-repeats`와 `--dispute-grader-controls`는 현재 디렉터리를 작업 공간으로 쓴다.

```bash
set -u
out=$(mktemp -d)
A=artifacts/analyses
same() { if cmp -s "$1" "$2"; then echo "same  $2"; else echo "DIFF  $2"; fi; }

# τ³ banking_knowledge (10)
uv run --offline python -m scripts.analyze_banking_memory --workspace . --result-feedback > "$out/rf.json"
same "$out/rf.json" "$A/20261125-banking-result-feedback-results.json"
uv run --offline python -m scripts.review_banking_evidence --workspace . --writer-comparison \
  --note-ratings "$A/20261125-banking-feedback-note-ratings.json" --output "$out/rf-notes" > /dev/null
same "$out/rf-notes/note-review.json" "$A/20261125-banking-feedback-note-review.json"
for k in development validation input-audit; do
  uv run --offline python -m scripts.analyze_banking_policy_cycle "$k" --workspace . --output "$out/cycle-$k.json" > /dev/null
  same "$out/cycle-$k.json" "$A/20261125-banking-policy-cycle-$k.json"
done
uv run --offline python -m scripts.review_banking_scoped_writer evaluate --workspace . \
  --ratings "$A/20261125-scoped-writer-blind-ratings.json" --output "$out/scoped-writer.json" > /dev/null
same "$out/scoped-writer.json" "$A/20261125-scoped-writer-review.json"
uv run --offline python -m scripts.analyze_banking_scoped_decisions evaluate --workspace . \
  --ratings "$A/20261125-scoped-decision-blind-ratings.json" --output "$out/scoped-decisions.json" > /dev/null
same "$out/scoped-decisions.json" "$A/20261125-scoped-decision-results.json"
uv run --offline python -m scripts.analyze_banking_memory --form-repeats > "$out/form-repeats.json"
same "$out/form-repeats.json" "$A/20261125-banking-multitarget-forms.json"
uv run --offline python -m scripts.analyze_banking_memory --dispute-grader-controls > "$out/dispute.json"
same "$out/dispute.json" "$A/20261125-banking-dispute-grader-controls.json"
uv run --offline python -m scripts.analyze_banking_memory --action-attempts \
  runs/tau-banking-{memory,required,result-feedback}-01/task-task_008/solve runs/tau-banking-memory-01/task-task_008/source \
  runs/tau-banking-policy-check-01/task-task_014__{existing,candidate}/solve \
  runs/tau-banking-policy-check-sources-01/task-task_014/source > "$out/attempts.json"
same "$out/attempts.json" "$A/20261125-banking-first-action-attempts.json"

# BFCL (8)
profile=protocols/diagnostics/bfcl-relevance-two-pass-v1.json
recording=runs/bfcl-relevance-two-pass-01-decoded
checkout=.local/benchmark-portability/bfcl-6ea5797
uv run --offline python -m agent_memory_eval.runner.bfcl_relevance analyze \
  --profile "$profile" --checkout "$checkout" --recording "$recording" > "$out/decisions.json"
same "$out/decisions.json" "$A/20261126-bfcl-relevance-local.json"
uv run --offline python -m agent_memory_eval.analysis.bfcl_relevance --archive "$out/decisions.json" \
  --profile "$profile" --checkout "$checkout" --recording "$recording" > "$out/evidence.json"
same "$out/evidence.json" "$A/20261126-bfcl-relevance-evidence.json"
audit() {  # spec arm control expected
  uv run --offline python -m agent_memory_eval.runner.bfcl_read_check --audit --parent "$recording" \
    --parent-profile "$profile" --archive "$A/20261126-bfcl-relevance-local.json" \
    --spec "$1" --arm "$2" --control "$3" > "$out/audit-$2.json"
  same "$out/audit-$2.json" "$4"
}
for arm in availability list_only availability_list_only; do
  audit protocols/diagnostics/bfcl-read-initiation-v1.json "$arm" "runs/bfcl-read-initiation-$arm-01" \
    "$A/20261126-bfcl-read-path/initiation-$arm.json"
done
audit protocols/diagnostics/bfcl-read-interface-v2-check.json constrained runs/bfcl-read-interface-v2-constrained-01 \
  "$A/20261126-bfcl-read-path/v2-constrained.json"
for arm in original contextual; do
  audit protocols/diagnostics/bfcl-tool-only-retrieval-v1.json "$arm" "runs/bfcl-tool-only-$arm-01" \
    "$A/20261126-bfcl-read-path/tool-only-$arm.json"
done
```
