# 새 개발 세션 인수인계

2026-11-17 · 대화 이력 없이 개발을 이어가기 위한 기준

작업 저장소는 `agent-memory`다. 기존 `ai-memory`는 원천 자료와 과거 실험의 저장소다. **프레임워크 구현은 여기에서 진행하며, 첫 오프라인 공통 코어와 가짜 환경은 동작하는 상태**이다. 연구 범위 전체를 유지한 채 구현 의존성에 따라 순서를 정한다.

## 현재 실제 상태

| 구분 | 인수인계 시점 상태 |
| --- | --- |
| 연구·설계 | 연구 질문·프로토콜·지표·관련 연구·상세 설계 문서 있음 |
| 계약 | v1.0/v1.1 예시 보존, journal envelope v1.1, snapshot 자동 제공의 별도 journal/event/manifest v1.2 확장 |
| 동작하는 코드 | 파일 코어, native 기억/두 회차·파일럿·전달 비교·무기억 배치·결정 재전송, commit 재현·schema/blob 캐시·원본 10개 감사, 공개 경험의 피드백별 기억 작성과 새 native probe의 mock 연결 |
| 패키지 | [구현 위치와 후속 범위](../../src/agent_memory_eval/README.md)를 구분 |
| 미구현 | 범용 연구 계획 의미/준비 검사·다른 벤치, 제목/선택기/가공 확장·누적·저장/조회 교차·전체 회차 복원/개입·자동 의미 주석·통계 추론 |
| 실험 | 48회 전달 비교·55회 무기억 채점에 이어 고정 결정 지점 120요청 완료. 모두 정상 응답·보고 비용 $0.043359345. 업무 도구·사용자 시뮬레이터를 실행하지 않은 한 응답 진단이며 일반 기억 효과는 미확정 |
| 분석 | 인수 검사 45개·조건명을 가린 의미 주석 75개. 14의 메모 수하물 0개 호출 5/5, 21 medium은 쓰기 없이 확인/조회 5/5, 17 메모에 새 날짜 오류 4/5. 기존 실패 8·1·2 분류와 향후 에피소드 7·39 제외 기준·원본 집계 유지 |
| 과거 자료 | [데이터 보관본](../data/legacy-datasets.md)에 원본 datasets·설계 자료·생성 코드·설명 문서 복사 |

첫 저장소 초기화는 `172859cd4b2073440448ea9084c552758f3bd387`이다. 이 문서 이후 기능이 추가됐을 수 있으므로 `git status`, 최근 커밋, 실제 함수·테스트를 확인한다. 다음 기능 작업이 끝나면 이 표와 실행 방법·미완료 항목도 갱신한다.

## 먼저 읽을 문서

1. 루트 [AGENTS.md](../../AGENTS.md)와 [README](../../README.md).
2. [연구 계획](../research/plan.md), [실험 프로토콜](../research/experiment-protocol.md), [측정](../research/metrics.md).
3. [상세 설계](../design/architecture.md), [코어 계약](../design/core-contracts.md), [기록 계약](../design/recording.md).
4. [OpenRouter·τ² 무기억 연결](../development/tau2-connection.md), [기억/두 회차 연결](../development/tau2-memory.md), [현재 오프라인 실행/검증](../development/offline-core.md), [첫 구현 명세](../development/first-implementation.md)와 [전체 개발 계획](../development/roadmap.md).
   현재 세 조건 비교는 [자율 조회 파일럿](../development/tau2-autonomy-pilot.md)과 [관측 receipt](../../artifacts/pilots/20261114-tau2-autonomous-memory.json)를 확인한다.
5. [관련 연구](../research/related-work.md), [보고서 네 장 구성](report-outline.md), [데이터 사용 안내](../data/legacy-datasets.md).
6. [추가 실험팀 문서 검토](experiment-handoff-review.md)와 [파일럿 제안](../research/condition-sensitive-pilot.md).
7. [방향 논의 노트](../design/direction-notes.md): 최근 구성 스케치·재검토와 아직 논의할 세부 사항.
8. 다른 에이전트가 정리한 [파일럿 이후 발전 방향 노트](../research/novelty-direction-review.md): 추가 문헌과 후속 주장/비교의 제안이며 기존 범위·draft를 자동 교체하지 않는다.

연구·프로토콜·설계가 현재 기준이다. `archive/`의 과거 문서와 `docs/references/literature/`의 이전 조사 해석을 현재 실행 지침으로 대체하지 않는다.

## 이미 합의한 연구와 사용자 선호

- 같은 작업자 Luna의 가중치를 고정해 저장·조회 정책의 차이를 비교한다. 시작은 τ²-bench, 확장은 AppWorld·BFCL이다.
- 사용자 시뮬레이터는 **DeepSeek V4.1 Flash 1017**(`deepseek/deepseek-v4.1-flash-1017`)로 사용자가 지정했다. 현재 연결 프로필에서 OpenRouter → Fireworks 경로로 고정하며, GPT-4.1으로 되돌리지 않는다. DeepSeek 제공자 직접 경로는 계정 데이터 정책에 맞는 endpoint가 없다는 HTTP 404로 중단했고, 계정 정책은 바꾸지 않았다. Luna 작업자의 reasoning low는 유지한다. 모델별 파라미터·seed 전달 차이는 [연결 안내](../development/tau2-connection.md)를 따른다.
- `memory_store`·`memory_list`·`memory_get`을 통해 LLM이 저장 내용·개수·조회 시점을 판단한다. 저장 시점은 없음·종료 후·수행 중·병행을 지원한다.
- 1회차 경험에서 기억을 만들고, 2회차에는 새 세션·새 업무 환경에서 기억으로만 과거를 공유한다. 자기 경험 은행과 N개 혼합 풀은 같은 원천에서 작성한 동일 버전을 사용한다.
- 평가마다 시작 snapshot을 복제한다. 허용된 2회차 쓰기는 해당 복제본에서만 일어나고 원은행·다른 평가에 반영하지 않는다.
- 재시도·새 과제 전이·누적 변화·저장/조회 교차·내용 개입·비용을 연구 범위에 둔다. 첫 구현의 작은 예시를 연구 범위 축소로 해석하지 않는다.
- BM25나 임의의 2k 예산을 기본값으로 추가하지 않는다. 저장 용량·읽은 양·실행 종료 한도는 서로 다른 설정이다.
- 원천 경험·필요 근거·저장 내용·선택·입력 포함·실제 수행 효과를 나눠 관찰한다. 제목도 노출이고, 반환·입력 포함만으로 실제 활용을 단정하지 않는다.
- 실패→성공과 성공→실패를 모두 보고한다. 무기억 반복·반복 성공 확률·배정/결측과 의존성을 보존한다. 단계 수 증가를 진척으로 대체하지 않는다.
- 과거 고정 은행의 관계 라벨을 새 자율 생성 기억의 정답으로 사용하지 않는다. 새 성능이나 효용은 측정 전이다.
- 사용자는 불필요한 재확인으로 구현이 멈추는 것을 원하지 않는다. 이미 정해진 일반 구현은 진행하고, 실제 연구 설정의 새로운 결정을 기존 합의처럼 확정하지 않는다.
- 문서는 현재 판단을 담은 초기안이다. 큰 연구 방향을 유지하면서 디테일은 더 적합한 방식으로 판단하고 사용자와 논의해 조정한다. 저장 기술·모듈 분할·검증 방법을 문서에 적혔다는 이유만으로 최종 합의나 필수 조건으로 굳히지 않는다.
- 선행연구와 일부 겹쳐도 우리 조건에서 재현하며 추가 발견을 찾는 방향으로 진행한다. 겹침 자체로 구현·실험을 멈추지 않으며 기존 결과와 새 관측을 구분해 기여 주장을 정한다. 참고 문헌·탐색 질문·파일럿 해석은 [방향 노트](../design/direction-notes.md#선행연구를-활용하는-탐색-방향)에 있다.
- 최신 저장 방향은 첫 구현을 SQLite 없이 JSON·JSONL 파일로 가볍게 시작하는 것이다. 사용자는 당장 DB 설치·도입을 원하지 않는다. 단일 프로세스·순차 쓰기와 작은 오프라인 흐름을 우선하고, 복잡한 동시 쓰기·전체 세션 자동 복구는 후속으로 둔다. 세션·은행 격리·원본 버전·불완전 실행 기록은 유지한다.
- Git은 `main`을 기준으로 한다. 사용자가 만든 변경을 보존하고, 검증 후 요청된 커밋·푸시는 `main`에 한다.

## 다음 세션에서 바로 할 일

[실험 계층 개선안](../development/experiment-layer-improvement-plan.md)에 코드 리뷰와 최근 실험을 함께 정리했다. [P0 실행기](../development/historical-reproduction.md)·[registry](../../protocols/reproductions/historical-v1.json)로 전달 비교 `65a757f`·무기억 `7243fef`·재전송 `c51165b`·원인 진단 `c31a9d6`의 **네 과거 명령을 실제 재현했다.** [검증 자료](../../artifacts/analyses/20261116-historical-reproduction-results.json)에 API 키 제거·금지된 I/O 0·원본/사본/코드 보존·사본 정리를 남겼다. 실제 작은 Git worktree 행동 테스트는 18개다. 이어 schema validator를 경로별 캐시해 동일한 120요청 감사가 49.80→5.87초로 줄었고 결과 해시는 같았다. [측정 자료](../../artifacts/analyses/20261116-schema-validation-performance.json)·[반영 보고서](../reports/20261116-reproduction-and-review-update.md)를 따른다. 캐시 테스트 3개를 포함해 전체 **172개가 373.041초에 통과**했고 기본 검사 3개도 통과했다. 모든 unittest는 mock HTTP이며 새 모델 호출은 없다.

이어 `319780a`에서 `+1` 시각·usage null·완료 stop 사유의 세 버그를 수정했다. 관련 50개 검사에 이어 **전체 182개가 488.026초에 통과**했고 기본 검사 3개도 통과했다. 원천 항공편 300개·다음 날 도착 36개의 합성 순서 검사, 과거 세 실험의 최신 코드 감사와 고정 commit 재전송 재현도 통과했다. [수정·검증 기록](../development/runtime-edge-fixes.md)을 따른다. 새 모델 호출 없이 과거 결과와 동결 기록을 보존했다.

이어 [writer blob 검증 캐시](../development/blob-verification-cache.md)와 [피드백별 기억 작성](../development/worker-written-memory.md)을 구현했다. **전체 197개가 417.496초에 통과**했다. 같은 공개 경험을 세 피드백으로 나누고, 새 writer 은행의 snapshot을 독립 native 세션·업무 DB·공식 채점으로 연결하는 mock 검사다. task 40의 프로그래밍한 응답 결과이며 실제 Luna의 기억 효과는 측정하지 않았다. 도구 라우터는 memory 계층에 두고 기존 import는 유지한다.

[원본 10개 전체 감사](../development/preserved-run-audits.md)는 수정 전 `7f4d0f3`의 기준값과 모두 같았다. 명령과 전체 JSON 해시 registry를 추적한다. [재현 receipt 공유 index](../../artifacts/reproductions/20261116/index.json)에 기존 7회 증거의 경로 정리 사본을 보존했다. 새 재전송 판정은 버전을 표시하며 과거 결과는 그대로 읽는다.

그 뒤 [추가 실행 보완](../development/runtime-hardening.md)에서 writer 추론 필드 재입력·전체 대화/비용 감사·중복 ID 부분 쓰기를 고쳤다. 이전 `1e59371` worktree에서 네 회귀를 재현했고 **전체 215개가 414.294초에 통과**했다. 새 API의 역할별 요청/입력 한도·worker 한도 뒤 도달 상태 진단·비정상 응답 보존·clean Git 동결 검사도 연결했다. 기존 합산 한도·원본·공식 결과는 보존한다.

다음은 [task 14 설계 초안](../../protocols/drafts/tau2-worker-written-memory-study-v1.json)의 1차 비교·지침·비평·표본·예산과 새 한도 API의 실제 값, 은행/전체 입력 사전 점검·한도 종료 집계다. 도달 상태 진단을 공식 성공으로 세지 않는다. 현재 API는 새 source 수집을 지원하며 과거 baseline의 transcript 자동 import·새 유료 실행기와 동결 검사 연결은 미구현이다. 독립 판정·과거에만 있는 정보의 자율 조회 과제도 준비한다. 증분 journal/인덱스·baseline/delivery 공통 기능·인프라 재개는 후속이며 v1.3은 확정하지 않았다. 새 유료 호출은 없다.

현재 `validation.py`가 동결 해시와 달라진 `fafccd0`에서 P0의 재전송 검토를 다시 실행해 통과했다. `post_change_reproduction` 근거는 위 재현 검증 자료에 있다. 예전 broad 해시를 현재 코드에 맞춰 고치거나 현재 src를 옛 버전으로 되돌리지 않는다.

**최신 결과는 [결정 지점 120요청 결과](../reports/20261116-decision-replay-results.md)다.** [새 프로필](../../protocols/pilots/tau2-decision-replay-120-v1.json)·[판정 방법](../research/decision-replay-pilot-v1.md)·[동결 기록](../../artifacts/pilots/20261116-decision-replay-protocol.json)을 실행 전에 고정했고 [receipt](../../artifacts/pilots/20261116-decision-replay-results.json)·[단일 분석 에이전트 주석](../../artifacts/analyses/20261116-decision-replay-review.json)을 보존했다. 120/120 정상 응답·$0.043359345·오류/생략/비용 미확인 0이다. `call_limit`은 120시도 완료를 뜻한다. 업무 도구는 실행하지 않았다. 수하물 규칙의 재사용을 첫 후보로 준비하되 17의 새 오류·21의 보류를 함께 추적한다. 현재 문맥만으로 충분한 대조·일반 메모·옳았던 상태를 준비하고 작업자 작성 기억과 연구자 메모를 구분한다. 42·1과 20은 한 응답 판정에 적합한지 다시 고른다. 새 유료 실행·480요청으로의 확대는 미승인이다.

원본이 있으면 `uv run --frozen python scripts/reproduce_experiment.py --experiment decision_replay --run --output outputs/reproductions/NEW_REPLAY_REVIEW`로 고정 코드의 동결 참조·로그·검사·주석을 재검증한다. 최신 코드의 원본 호환 감사는 `scripts/run_decision_replay.py --audit-only --output runs/tau2-decision-replay-luna-120-01`로 별도 실행한다. 실행 구현 128개와 별도 판정/출처 참조는 당시 코드에 보존한다. 아래의 과거 직접 review/diagnose 명령은 해당 고정 checkout·공유 코드 사본 기준이며 현재 main에서는 P0 실행기를 사용한다. 이하 무기억 측정과 구현 단계는 이전 이력이다.

새 raw와 기준 코드 `c51165b` 사본은 [팀 Drive](https://drive.google.com/drive/folders/1hr7bEjZeAGegNeJiBouaaJOyYC05xk-O)에 약 3.0MB 증분 묶음으로 공유했다. [registry](../../artifacts/data-releases/20261116-decision-replay.json)·[복원 안내](../data/team-run-sharing.md)를 따른다. 7개 파일 모두 재다운로드·바이트/해시/위치 검증을 마쳤고, 새 묶음만 복원한 환경에서 API 역할 키·이전 raw 없이 결과 검사를 재현했다. 이전 공유본·원본·권한은 유지했다.

[train 전체 무기억 측정](../reports/20261115-tau2-baseline-screening-results.md)은 별도 [ready 프로필](../../protocols/pilots/tau2-baseline-screening-v1.json)의 전체 $1 한도와 [85개 동결 참조](../../artifacts/pilots/20261115-tau2-baseline-screening-protocol.json)를 실행 전에 고정했다. 936 API 시도·확인된 보고 비용 $0.1055333152에 미확인 요청 1건이 있다. 과제 28의 두 번째 반복에서 사용자 시뮬레이터 HTTP 503이 발생했고, 뒤의 7·36·15·21 두 번째 반복을 생략했다. 전체 60회 완료·비용 전액 확인·오류 자동 재개로 해석하지 않는다.

원본은 `runs/tau2-baseline-luna-deepseek1017-train-01`이다. [결과 receipt](../../artifacts/pilots/20261115-tau2-baseline-screening-results.json)·[공개 근거/주석](../../artifacts/analyses/20261115-tau2-baseline-behavior-review.json)·[DB 채점 진단](../../artifacts/analyses/20261115-tau2-train-grading-diagnostic.json)을 연결했다. 공식 성공은 참조에 DB 변경이 있는 과제 12/23, 없는 과제 32/32다. 오류를 사후 제외해 기억 효과나 일반 난도를 확정하지 않는다. 저장 원본은 약 81.4MiB이며 이전 48회와 작업량이 달라 저장량 차이의 인과 효과를 주장하지 않는다.

새 원본과 재검증용 코드 사본은 [팀 Drive 증분 묶음](https://drive.google.com/drive/folders/1ku0IRmXZYz34K1BlCuLjDAxX7Ly-f73l)에 공유했다. [registry](../../artifacts/data-releases/20261115-baseline-screening.json)와 [복원 안내](../data/team-run-sharing.md)를 따르며 이전 15개 실행의 보관본과 혼동하지 않는다. 약 15.2MB의 압축본과 안내 등 7개 업로드 파일은 모두 재다운로드·바이트/해시 검증했다.

사용자의 원인 파악 요청으로 [실패 원인 진단](../reports/20261115-tau2-baseline-failure-diagnosis.md)과 [재계산 근거](../../artifacts/analyses/20261115-tau2-baseline-failure-diagnosis.json)를 추가했다. Luna 565개 실제 요청에 정책 전문이 있었고, 실패 11개의 판단 직전 요청에는 기존 도구 결과가 모두 포함됐다. 정상 응답 935개에서 length 종료는 0이었다. 11개 최종 DB·DB/COMMUNICATE 점수를 strict 재생했고 모델·네트워크 시도는 0이었다. 모델이 왜 규칙을 잘못 적용했는지의 내부 원인까지 확정한 것은 아니다.

7은 ‘다른 예약’ 합계 708과 초기 전체 네 예약 합계 1628의 **대상 범위**도 문제여서 질문 시점만 고쳐서는 해결되지 않는다. 39 첫 반복은 보장 밖 사유의 취소 하나를 추가하면 참조 DB와 일치했고, 14는 수하물 6→0 하나로 DB 차이가 사라졌다. 이들은 로컬 도구·복사한 텍스트의 진단이며 모델/기억의 효과가 아니다. 기존 공식 점수·원본·비용은 유지한다.

추가 검증 의견을 [분류·진행 범위 정정](../reports/20261115-baseline-review-update.md)에 반영했다. **14는 필요하지 않은 수하물 추가와 6개 승인 누락이라는 Luna 절차 오류, 11은 정상 과제에서 사용자 시뮬레이터가 환불 제약을 추가한 이탈**로 정리한다. 이후 에피소드 주 비교는 과제 정의 충돌이 있는 7·39를 제외하는 개발 범위 결정을 따른다. [준비 기록](../../artifacts/analyses/20261115-decision-replay-preparation.json)에 train 후보 28개와 이유를 남겼다. 과거 55회 집계를 바꾸거나 39·2의 실제 부적합 취소를 지우지 않는다. 11의 이탈은 반복과 함께 관찰하되 성공할 때까지 대체 실행하지 않는다.

이어 [한 응답 재전송](../development/decision-replay.md)을 구현했다. 같은 8개 원본 요청에 원본/medium/연구자 메모만 비교하고 업무 도구는 실행하지 않는다. 기존 [draft](../../protocols/drafts/tau2-decision-replay-v1.json)는 480요청 제안이며 배정 seed·요청/시간/비용 한도가 null이라 live를 차단한다. 이후 별도 120요청으로 실행한 결과는 위 최신 문단을 따른다. 메모는 결과를 본 연구자가 쓴 절차 노트이며 작업자 자율 작성 기억과 섞지 않는다. 호출 3개는 좁은 인수 검사, 설명/계획 5개는 공개 응답의 의미 판정으로 남긴다. 이번 판정은 사람 대신 단일 분석 에이전트가 수행했다. 추가 조회·확인 질문·보류를 자동 성공으로 세지 않는다.

새 16개를 포함한 전체 148개 테스트와 기본 검사 3개가 통과했다. 기존 85개·64개 동결 참조와 원본 로그 해시를 유지했다. 다른 세션의 [추가 검토 노트](../research/baseline-failure-review-note.md)는 응답 유형별 추론·같은 seed의 변동과 일반 메모/옳은 상태 대조 등의 후속 제안을 담는다. 현재 세 조건·8개 오류 지점 구현에 그 확장까지 완료됐다고 해석하지 않는다.

재전송의 판정 기준·120요청 프로토콜과 결과는 위 최신 문단을 따른다. 후속 준비에서는 기억 본문에 정답 금액·예약 ID·원경험에 없던 시각을 보충하지 않는다. 새 반복 원본을 기준선으로 삼고 역사적 오류를 기준 오류율로 쓰지 않는다. 더 어려운 과제가 곧 기억 필요 과제는 아니다. 17처럼 잘못된 안내를 사용자가 수용한 경험이 어떻게 기억되는지도 후보 질문으로 유지한다. 전체 회차 성공·자율 조회·전이 효과는 이 한 응답 진단과 별도다.

새 모델 호출 없이 `uv run --frozen --extra tau2 python scripts/review_tau2_baseline_screening.py`로 동결 참조·원본·receipt·주석 연결을 재검증한다. 전체 128개 테스트는 실제 호출 전 통과했고, 공개 분석 테스트 4개가 별도로 통과했다. 아래 과거 단계의 ‘추가 호출 없음’·미정 비용 설명은 해당 단계의 이력이다.

최근 사용자 요청에 따라 [자율 조회 원인 진단](../reports/20261114-memory-retrieval-diagnosis.md)을 추가했다. 네 원본의 요청 744개에서 스키마/선택/원응답·반환을 대조했고, 최신 on-demand 88/88 요청에 list/get·auto가 있었다. 원응답의 기억 호출은 0이었고, 같은 모델 ID·경로의 과거 guided list/get은 실제 성공했다. 전송 누락·라우팅 유실을 발견하지 못했으며 벤치 문맥·정보 필요·지침·모델의 인과 분리는 아직 미실행이다. 검사 코드와 근거 JSON은 보고서에 연결했다. 새 진단 테스트 7개와 기존 관련 테스트 3개가 통과했으며 새 유료 호출은 0이다. 기존 109개 전체 통과 이력과 구분한다.

사용자는 현재 과제가 쉽고 기억을 필요로 하지 않았다는 판단을 덧붙였다. [다음 과제 선정 노트](../design/direction-notes.md#다음-과제-선정-난도와-기억의-쓸모를-따로-확인)에 난도·기억의 추가 가치·적용 가능성을 나눠 기록했다. 후속 후보는 원천 경험의 재사용 내용, 현재 경로에서 재획득 가능한 정보, 행동·채점의 차이를 먼저 확인한다. 기존 과제는 현재 문맥으로 충분한 대조로 보존한다. 새 후보·변형은 아직 구현·실행하지 않았고, 동결한 과제 선정·프로토콜과 원본은 유지했다.

2026-11-15의 [다음 단계 초안](../research/tau2-baseline-screening-draft.md)은 전체 train 30개 × 2회 무기억 탐색을 제안했다. 7개 소스 해시를 기존 manifest와 대조하고 30개 과제의 행동 구조 후보표를 작성했다. 이어 [무기억 전용 배치](../development/tau2-baseline.md)를 구현했다. 새 12개를 포함한 전체 128개 테스트·기본 검사 3개·새 스키마 검사와 이전 동결 파일 64개 해시 대조가 통과했다. 이 구현 검증 당시에는 native fake HTTP만 사용했고, 실제 측정은 위 별도 ready 프로필로 진행했다.

기본 CLI `scripts/run_tau2_baseline.py --preflight`는 모델/catalog HTTP 없이 30과제·60배정과 미정 비용 필드를 보고한다. 원래 draft의 전체 비용 null은 보존했고, 실제 측정은 별도 $1 ready 프로필을 사용했다. 기존 $3 승인을 전용하지 않았다. 완료 run의 provenance는 과거 commit에 보존해 head 누적 복사를 줄이고, 새 audit는 요청 당시 state에서 출처를 검증한다. 기존 코어·실행 코드와 과거 원본은 수정하지 않았다.

먼저 현재 변경과 기존 검증을 확인한다.

```bash
git status --short
git log -5 --oneline
uv sync --frozen
uv run python scripts/validate_design.py
uv run python scripts/check_repository.py
uv run python scripts/verify_legacy_data.py
uv run python -m unittest discover -s tests
```

현재 명령은 모델 호출 없이 로컬 계약·링크·보관 파일과 오프라인 행동을 검증한다. 기존 93개에 snapshot 제공 8개·전달 실행 구성 8개를 더해 extra/checkout이 있는 전체 109개가 통과했다. native 테스트 명령 자체는 유료 호출을 하지 않는다. τ² extra가 설치된 환경은 `uv run --extra tau2`로 기존 의존성을 유지한다.

**이전 [48회 전달 비교](../reports/20261114-tau2-delivery-development-results.md)도 보존한다.** [프로토콜](../research/delivery-development-pilot.md)·[동결 기록](../../artifacts/pilots/20261114-tau2-delivery-development-protocol.json)과 [결과 receipt](../../artifacts/pilots/20261114-tau2-delivery-development-results.json)·[근거/주석](../../artifacts/analyses/20261114-tau2-delivery-behavior-review.json)을 유지한다. 당시 622요청·$0.062301097, 오류·생략·비용 미확인 0이었다.

앞으로도 설명·시나리오 판정의 조건 가림 재검토, 실제 정보 차이가 있는 개발 과제·채점 검증, 같은 경험의 기억 표현 비교를 이어간다. 과제 40의 빈 은행/본문 제공 실패는 기존 생년월일을 다시 요구한 경로로 남겼으며 기억 문장의 원인 효과를 주장하지 않는다. 이전 상태 사본 약 759MiB와 새 측정의 기록량은 작업량 차이와 함께 본다. 파일 방식과 원본을 유지한다. [갱신한 2주 계획](../reports/20261114-progress-and-two-week-plan.md)을 기준으로 진행하며 아래 이전 단계의 개발 이력을 새 실행 지시로 해석하지 않는다.

원본이 있는 고정 구현 checkout에서 `uv run --frozen --extra tau2 python scripts/review_tau2_delivery_development.py`로 공식 audit·공개 근거·수동 주석의 연결·receipt를 재검증한다. 주석은 의미 판정의 독립 검증을 대신하지 않는다. raw `runs/`는 Git 밖에 보존한다.

첫 오프라인 코어와 2원천·8 run 가짜 흐름은 구현했다. [실행 안내](../development/offline-core.md)에 따라 **새/빈 출력 폴더**에서 실행하고 `--analyze-only`로 원본을 바꾸지 않고 재계산할 수 있다. 예제의 페이지 4·run별 요청 12는 합성 builder 값이며 실제 draft를 채우지 않았다.

OpenRouter·Luna 작업자·DeepSeek V4.1 Flash 1017 사용자·고정 τ²의 [무기억 확인 기록](../../artifacts/connection-checks/20261114-openrouter-tau2-deepseek1017.json)에 이어 [기억 두 회차 확인](../development/tau2-memory.md)과 [원본 해시/비용 receipt](../../artifacts/connection-checks/20261114-openrouter-tau2-memory.json)를 남겼다. 1회차 사후 저장 1개, 새 2회차 list/get 각 1회, 20요청·$0.0026238194·두 업무 native 성공이다. 저장/조회를 지시한 guided_check로, 자율 조회율이나 기억 정책 효과로 해석하지 않는다. 이전 GPT-4.1 [검증 기록](../../artifacts/connection-checks/20261114-openrouter-tau2.json)과 404 실패도 이력으로 보존한다.

이후 [자율 조회 파일럿](../development/tau2-autonomy-pilot.md)을 실제로 실행했다. 첫 시도는 선택적 사후 작성에서 저장 0으로 9개 평가를 source_empty로 생략했다. 조회 비교를 위해 기존 guided source의 기억 1개를 그대로 고정한 v1.1에서 조건별 3회·총 9회를 완료했다. 제목/본문 입력 포함은 전부 0, native 점수는 전부 1.0이었다. 이 과제는 명시적 취소 거절의 NL assertion이 reward_basis에 없으므로 이 점수로 거절 품질을 확정하지 않는다. 두 시도 보고 비용은 $0.0125059432이며 미확인 비용은 없다.

다음은 미조회와 정보 가치의 구분, 업무 행동을 판별할 채점/주석, 일반 연구 계획 의미/준비 검사다. 현재 닫힌 파일럿의 준비 검사를 범용 PlanCompiler 완료로 보지 않는다. 과거에만 있는 정보가 있는 과제 등을 별도 프로필로 설계하고 1차 비교·표본/한도를 확정한다. 기존 진단끼리의 점수/단계 차이를 효과로 쓰지 않는다. 전체 세션 복구·다중 실행 동시성·선택/가공 확장·누적/교차/개입과 통계를 이미 구현한 것으로 취급하지 않는다.

사용자가 결과 해석과 다음 2주의 발전을 강조해 [진행 보고서 초안과 2주 계획](../reports/20261114-progress-and-two-week-plan.md)을 추가했다. 보고서용 [별도 행동 분석](../development/tau2-autonomy-pilot.md#보고서용-행동-보조-분석)은 이관 요약만 제외하면 호출 차이가 9/9에서 7/9쌍으로 줄어드는 것을 원본 audit 후 재계산한다. 원래 로그·요약·지표는 보존했다. 이 보완에서는 새 유료 실행을 하지 않았다. 다음 순서는 과제/채점 검토 → 본문 시작 제공·최소 설정 실행 구성 → 동결한 비교이며, 실제 과제·반복·비용 값은 새 프로토콜에서 확정한다.

이어 [9회 업무 사례 검토](../reports/20261114-tau2-case-review.md)와 [다음 과제 후보·채점 기준](../research/tau2-task-selection-and-rubric.md)을 작성했다. 사용자에게 명시적으로 거절한 실행은 5회, 바로 이관한 실행은 4회였다. 다른 담당자 요청이 공개된 3회와 이관 근거를 더 판단해야 할 6회를 나누고, 기본 이코노미의 항공편 변경 대안 제안 1회를 설명상 검토 대상으로 남겼다. 단일 에이전트·조건 공개·사후 주석이며 원본 점수와 별도다. 기억 노출 0이므로 기억 내용의 원인 효과로 해석하지 않는다.

공식 train 30개를 구조 검토하고 개발 후보 0·40·47·49·7·17을 남겼다. 40과 49는 동일 예약의 출처 그룹이다. 39는 보험 보장 사유가 시나리오에 확정돼 있지 않아 추가 판정 대상으로 남겼다. 고정 native 오프라인 참조 재생과 무행동 DB 검사에서 7·17·40은 필요한 변경을 하지 않으면 DB 보상 0.0이었다. 이는 채점 구조 확인이며 새 Luna 성능 결과가 아니다. 분석 전용 JSON 두 개에 근거·소스 해시를 남겼고 worker·writer 입력으로 사용하지 않는다. 다음 구현은 본문 시작 제공과 최소 과제/조건/반복 실행 구성이다. 실제 은행·확인용 과제·반복·비용·조회 도구 유지 여부를 새 프로토콜에서 고정한다.

그 다음 작업으로 [고정 은행 전달 비교](../development/tau2-delivery.md)를 구현했다. 같은 snapshot의 모든 제목·본문을 system 다음 별도 문맥으로 제공하고 v1.2 출처·구간을 기록한다. 프로필로 공식 과제/출처 그룹·조건·반복·배정과 조회 도구 유지 여부를 받으며 매 run의 native 사용자·DB·은행을 분리한다. draft 확인과 `--preflight`는 네트워크 없이 실행되고, `--live`는 ready 설정·새 폴더를 요구한다. 초안은 보존한 채 별도 프로토콜에서 48회와 사용자 승인 $3 한도를 고정해 실제 비교까지 완료했다. 연구용 전체 PlanCompiler·통계 추론·동일 상태 개입은 후속이다.

이 구현의 검증은 전체 109개 테스트, 기본 설계/문서/보관본 검사, 기존 자율 조회 파일럿 두 개의 원본 audit다. 기존 로그·요약 해시는 유지됐다. 조회 도구를 끈 시작 제공에서도 허용되지 않은 쓰기를 거부하고 은행을 유지하며, 거부된 호출은 분석에서 유효한 실패/거부 기록으로 보존한다.

최근 구성·선행연구 탐색 질문은 [방향 논의 노트](../design/direction-notes.md), 구현 결정을 모은 기준은 [코어 계약](../design/core-contracts.md)이다. 자연 실행의 전체 정책 효과와 동일 경험의 저장 효과를 구분하고 현재 과제가 정상 제공하는 초기 대화는 유지한다.

최근 실증 리뷰의 정보 가치/적용 가능성 구분, 미조회 해석, 필요한 행동 유지와 효과 없음의 해석은 [만들면서 확인할 질문](../design/direction-notes.md#만들면서-확인할-실증-질문)에 남겼다. 구현을 멈추는 새 필수 조건으로 만들지 않고 가짜 흐름·실제 파일럿 순서에 맞춰 점검한다.

## 이번 리뷰 반영과 남은 구현

- 기억·모델 요청·업무·채점·run 수명주기를 하나의 `log.jsonl`에 확정한다. 자기/연결 해시·연속 순서와 불변 blob을 사용하며 은행·snapshot·사건 보기는 파생물이다.
- 요청 구성 시 승인 출처 manifest를 만들고 최종 전송 요청과 대조한다. 자기 작성 인수는 `self_authored`, 조회는 `memory_return`으로 구분하고 공개 경험은 길이·해시를 고정한 접두부로 참조한다.
- 공개 반환에서 은행 `head_revision`을 뺀다. 내부 revision·CAS와 불투명 커서는 유지한다.
- 혼합 호출은 부작용/결과 순서를 보존하고 종료 도구가 섞인 batch는 실행 전에 모두 거절한다. native 기억 binding에서 이를 행동 검증했고, 내부 기억 왕복은 사용자·native 단계/오류를 진행시키지 않는다. 수행 중/병행 작성은 native mock에서 검증하며 공개 guided 프로필의 실제 확인은 사후 작성/새 세션 조회다.
- v1.1 계획은 사용자 시뮬레이터·native 한도·응답 캐시 확인·adapter 설정 해시와 단계별 지침을 받는다. 미정 실제 값은 draft에 남겼다.

기존 v1.0 스키마·fixture·draft는 바꾸지 않았다. 새 v1.1 검증은 세션 ID 재사용·출처 위반·미래/변조된 경험·노출 누락·다른 은행/고정 버전·로그 손상을 합성 자료에서 검사한다. 과거 v1.0 검사 공백과 이번 확인 범위는 [계약 검증 설명](../design/core-contracts.md#5-합성-검증의-범위와-남은-구현)에 있다.

**파일 기록자·기억 서비스·출처 구성기·라우터와 가짜 실행을 구현했다.** 은행 생성·봉인·배정도 `state_ref`로 보존하는 journal v1.1을 추가했으며 기존 journal v1.0과 사건 v1.1은 유지한다. [실제 코드와 행동 검증](../development/offline-core.md)을 기준으로 한다. DB 설치는 하지 않았다. 이후의 제한된 실제 관측은 위 전달 비교와 무기억 측정 결과를 따르며 일반적인 기억 정책의 성능·득실은 미확정이다. Linux 파일 연산/주입 예외의 통과를 전원 장애·모든 파일시스템 또는 실제 provider의 보장으로 확대하지 않는다.

## 추가 인계 문서에서 남긴 후속 제안

사용자가 다운로드 폴더의 `memory-evaluation-experiment-handoff.md`도 검토하도록 요청했다. [원문](../references/feedback/20261114-memory-evaluation-experiment-handoff.md)을 보존하고, 기존 설계와의 관계·공식 근거·미열람 연결 자료를 [검토 문서](experiment-handoff-review.md)에 기록했다.

주요 추가안은 같은 공개 기록에서 일반 요약과 적용 조건·절차 기억을 작성해 비교하는 [파일럿](../research/condition-sensitive-pilot.md)이다. 종료 후 작성·시작 시 제공을 고정한 저장 진단에서 시작해 이후 조회 교차·1개/N개 후보로 이어갈 수 있다. 현재 draft의 자율 조회·네 저장 시점을 교체하지 않는다.

항공 33·36번을 등급 하나의 대조군으로 직접 쓰지 않는다. 새 진단쌍의 상태·인수·채점 의미를 먼저 검증하고, 변형 결과와 공식 원본 점수를 구분한다. 적용 가능성 필터는 작업자와 판정기의 정보 노출을 분리하고 적합 후보의 차단·미확정·추가 비용도 남긴다. Jev·계층 시간 기억·그래프는 후속 후보이며 기본 구현 의존성이 아니다.

필터 전 후보·판정·worker 노출을 별도 기록할 때 필요한 역할/필드의 계약은 아직 구현하지 않았다. 이 제안의 실제 모델·과제쌍·반복/예산·실험 실행도 미확정이다. 오프라인 코어와 native 기억 연결 위에서 별도 파일럿 draft를 준비한다.

## 미확정 값과 진행 가능한 작업

[τ² 초안](../../protocols/drafts/tau2-two-pass-v1.1.json)은 `draft`다. 실제 벤치 commit·도메인·과제·native 한도·응답 캐시·adapter 설정·모델 파라미터·단계별 작성 지침·페이지 크기·반복/순서·중단 한도·주석/통계/비용 배분은 실제 실험 계획에서 고정해야 한다. 사용자 모델은 위 DeepSeek 1017 지정을 반영하되, 연결 확인의 한도를 연구용 기본값으로 전용하지 않는다. 기본 표본 수·예산을 임의로 넣어 `ready`로 만들지 않는다.

모든 `null`이 미확정은 아니다. 선택적 저장 한도 없음, on-demand에서 preload 선택기 없음, 원문 조회에서 재가공 없음은 합법적인 설정이다. `PlanCompiler`는 미정 값과 해당 없음·무제한을 구분해야 한다. 스키마 통과도 의미 검사나 실행 준비 완료를 뜻하지 않는다.

오프라인 테스트에는 별도의 작고 명시적인 합성 값을 쓸 수 있다. 이를 τ² 실제 과제나 Luna 결과로 표기하지 않는다. 현재 계획 스키마의 벤치 이름은 실제 세 벤치만 허용하므로 가짜 환경은 테스트 전용 구성으로 시작한다.

제한된 무기억 연결 확인은 별도 connection_check 프로필로 수행했다. 연구 draft를 ready로 승격시키지 않았으며 향후 실행도 준비된 계획과 사용량·비용·오류 기록을 갖춘다. 오래된 모델 별칭을 불변 snapshot이라고 가정하지 않는다.

## 자료와 출처 유지

사용자가 지정한 Google Drive 팀 폴더에 [실행 원본 묶음과 복원 안내](../data/team-run-sharing.md)를 공유했다. 최신 48회와 이전 오류·준비 검사·합성 예제의 원본 폴더 15개를 보존했으며 업로드된 7개 파일을 다시 내려받아 검증했다. 링크·기준 코드 commit·해시는 [공유 registry](../../artifacts/data-releases/20261114-team-runs.json)에 있다. 복원 시 기준 코드와 자료 종류를 확인한다. 실행 중 기록을 Drive 동기화 경로로 옮긴 것은 아니다.

[데이터 보관 안내](../data/legacy-datasets.md)에 무엇을 복사했는지, 미커밋 원본을 어떻게 식별했는지, 무엇이 원래 위치에 남았는지 정리했다. 보관 자료는 오프라인 코어의 설치·기본 실행 의존성이 아니다.

[최초 설계 이관 manifest](../../artifacts/legacy-source-manifest.json)는 당시의 이력이다. `check_repository.py`의 `files_changed_since_migration`은 이후 정상 수정도 보고한다. 현재 파일 해시에 맞추려고 당시 manifest를 덮어쓰지 않는다. `--check-import-hashes`는 최초 이관 상태 확인용이며 이후 개발의 기본 통과 조건이 아니다.

데이터 보관본은 별도 [manifest](../../artifacts/legacy-datasets-manifest.json)로 관리한다. 원본 작업 트리를 포함하므로 기존 HEAD만으로 재생성할 수 없고, 복사한 파일 자체와 해시가 보존 근거다.

## 새 세션에 전달할 요청 예시

> 이 저장소의 AGENTS.md와 docs/handoff/next-session.md, docs/development/first-implementation.md를 읽고 현재 코드·검증 상태를 확인해줘. 추가 실험팀 문서 검토와 적용 조건 기억 파일럿 제안도 후속 계획으로 참고해줘. 연구 범위를 유지하면서 구현된 오프라인 코어와 실제 행동 검증을 확인하고, 실제 연결 확인 기록을 읽고 남은 연구 계획 준비 검사·τ² 기억 통합을 단계적으로 진행해줘. 보관 데이터·과거 라벨은 현재 실험 기본값으로 편입하지 말고, 미확정 실제 실험 숫자는 draft로 남겨줘. 구현·검증·문서 상태를 함께 갱신하고 main을 기준으로 작업해줘.
