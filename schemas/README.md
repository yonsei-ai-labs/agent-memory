# 설정과 사건 계약

새 실험은 계획 파일 하나로 선언한다. 벤치·과제·1회차·작성 방식·조회 방식·조합·참조 기록·반복·예산·재시도를 담고, 작성·조회 방식의 이름과 내용은 자유롭게 정한다. [계획 형식](study-plan-v1.schema.json) · [사용 방법](../docs/development/study-plans.md)

등록된 재방문 첫 판단을 이어가는 계획은 기존 배정·체크포인트 영수증·모델을 해시로 고정하고, 같은 안내의 실제·빈 메모와 고객별 상담 지표를 선언한다. v2는 주 비교의 직접 가림 판독과 판독 불가·네이티브 한도 종료의 처리를 고정한다. v3는 한도에 걸린 요청의 역할까지 대조해 작업자의 요청·입력 한도를 주 지표 실패로 센다. v1·v2 기록도 오프라인에서 다시 읽을 수 있다. [후속 계획 형식](retail-followup-plan-v1.schema.json) · [전체 상담의 배정과 판독](../docs/development/study-plans.md#리테일-재방문의-최종-상담)

두 벤치의 공통 정책 라운드는 공개 근거와 개발 과제, 형식별 작성·조회 정책, 확인 전 기준을 등록한다. 정책 근거 결과는 행과 작성 기록을 생략한 요약에도 각 집합의 해시를 남긴다. [라운드 선언](policy-round-v1.schema.json) · [정책 근거 결과](policy-evidence-v1.schema.json) · [실제 사용 방법](../docs/reports/20261126-evidence-foundation-plan.md)

원기록 결과는 1회차 공개 단서와 첫 업무 입력, 기억 노출, 함수 선택, 공식 성패 및 관측한 중단 시점의 진척을 연결한다. 개별 행을 생략한 요약에도 전체 행의 해시가 들어간다. [궤적 결과 v1](trajectory-outcomes-v1.schema.json) · [은행·BFCL 재현 명령](../docs/development/trajectory-outcomes.md)

개별 판단에서 같은 문자열이 원천·기억·다른 업무 입력·응답에 각각 있었는지 저장 버전과 요청 위치로 대조한다. [판단 단서 v1](decision-cue-v1.schema.json) · [세 벤치의 실제 사례](../docs/development/trajectory-outcomes.md#한-판단에서-단서의-출처를-검산하기)

원천 근거를 사전에 등록한 연구는 작성 전에 전체 연구의 진행·중단 판정과 원천 해시를 보관한다. [원천 판정 v1](source-opportunity-v1.schema.json)

과제별 표는 등록한 결과 파일에서 단계별 공식 성패를 집계한다. 검산표는 확인된 고객과 신원 미확정 과제, 전체 미조회 짝과 결과를 본 뒤 고른 부분집합을 구분한다. 지정된 총 짝 수의 검정력은 실제로 성패가 갈릴 짝 수의 변동을 포함해 계산한다. [과제별 원표](task-room-v1.schema.json) · [검산표](task-room-review-v1.schema.json) · [은행 적용 결과](../docs/reports/20261127-banking-task-room.md)

은행 개설 기억을 판독할 때는 상품·계좌 유형·소유자, 주장한 결과와 공개 반환의 호출 ID를 함께 기록한다. 각 작성 배정의 모든 항목을 판독하면 고객별 비교에 한 건으로 들어간다. [판독 제출 형식 v2](opening-outcome-ratings-v2.schema.json) · [대상과 인용 검증 사례](https://github.com/yonsei-ai-labs/agent-memory/blob/40f881aa1f70f5772f52484a4f1a19d0de01162d/tests/test_opening_review_v2.py)

공통 [짝비교 결과 v1](paired-outcomes-v1.schema.json)은 지표의 정의·버전·시점·단위와 집계 분모, 비교 배정, 원본 파일 해시, 조건별 관측의 JSON Pointer를 담는다. BFCL 스크립트 대조 예시로 지표 구조와 동작을 검증한다. [결과 스키마의 예시](../tests/fixtures/paired-outcomes-v1.json) · [보관 결과를 읽는 명령](../docs/development/framework-scoring-plan.md#보관-결과-짝비교-실행)

BFCL `rec_sum`에서 고유 도구를 쓰는 실행은 기록 계약 v1.3을 선택한다. `memory_append`의 자기 작성 본문, `memory_retrieve`가 반환한 본문의 항목·버전과 실제 입력 구간을 검사한다. 공식 반환에 내부 식별자를 추가하면 검증이 실패한다. 공통 파일 기록의 기본 버전은 v1.1, snapshot 자동 제공은 v1.2다. [v1.3 사건](v1.3/event.schema.json) · [원장](v1.3/journal-state.schema.json) · [반환 예시](../tests/fixtures/contracts-v1.3/native-return.json) · [BFCL 연결 검증](../docs/reports/20261125-common-policy-measurements.md)

다형식 작성의 `result_only`는 공식 1회차 성패와 그 이벤트를 묶어 보존한다. 별도 판독 계약은 가려진 자료에 대한 사람·모델 판독을 요청 해시와 원문 인용으로 연결한다. [성패 수신](form-result-feedback-v2.schema.json) · [근거 판독 제출](semantic-memory-ratings-v1.schema.json) · [동작과 검증](../docs/development/memory-evidence-review.md)

필수 형식 비교는 [필수 저장 정책](required-form-writing-v1.schema.json)을 사용한다. `writer-required-store/v1`은 저장 도구 호출이 실제 항목을 남겨야 완결된다. 선택형 저장의 기존 `writer-finish/v1` 기록은 원래 계약으로 감사한다. [성공·실패 fixture](../tests/fixtures/writer-required-store-v1.json)

[prompt-comparison-v2.schema.json](prompt-comparison-v2.schema.json)은 보관된 1회차와 두 메모 정책을 비교하는 v2 설정이다. 공개 개발 원문에 근거한 작성 지침 제안도 검증한다. [초안 예시](../protocols/drafts/tau2-airline-payment-memory-v2.json) · [제안·실행·판정](../docs/development/memory-policy-improvement.md)

v3는 등록한 각 메모리 형식에서 공통 후보 문구 하나의 효과를 기존 작성물과 짝지어 비교한다. [v3 계약](prompt-comparison-v3.schema.json) · [형식별 배정 예시](../protocols/drafts/tau2-airline-writer-pairs-v3.json)

[written-replay-batch.schema.json](written-replay-batch.schema.json)은 네 조건의 한 응답 배치 프로필이다. v2 준비물의 해시, 반복 단위·배정 시드·한도, 판정 및 비용 승인을 명시한다. 미정값은 초안에 보존한다. [실행과 감사](../docs/development/written-replay-batch.md)

[snapshot-replay.schema.json](snapshot-replay.schema.json)은 고정 문맥·snapshot으로 한 응답을 실행하는 설정/결과의 별도 v1 계약이다. 한 worker 요청과 명시적 입력 한도, 공식 성공 미측정, 오류/비용 미확인 시 중단 표시를 검사한다. [합성 예시](../tests/fixtures/snapshot-replay-v1.json)는 형식 자료이며, [실행기와 원본 감사](../docs/development/snapshot-replay-runtime.md)의 행동 검증과 구분한다. 기존 journal/event/manifest v1.1/v1.2 의미는 바꾸지 않는다.

[written-memory-comparison-v2.schema.json](written-memory-comparison-v2.schema.json)은 작성 기억의 오프라인 비교 전용 초안이다. 선정 도구·순번·전체 수와 연구 범위를 명시하며 ready/live 승격을 허용하지 않는다. [written-comparison-rubrics.schema.json](written-comparison-rubrics.schema.json)은 공개 메시지 근거에 연결한 좁은 인수 검사의 별도 v1 계약이다. [합성 fixture](../tests/fixtures/written-comparison-validity-v1.json)는 기본 검사에 포함하며, 의미 판정의 정확성·독립성을 자동 입증하지 않는다. [실제 적용과 한계](../docs/development/written-comparison-validity.md)를 따른다. 기존 일반 v1.1·snapshot v1.2와 과거 준비 v1은 유지한다.

[execution-limits.schema.json](execution-limits.schema.json)은 새 API가 명시적으로 선택하는 `per-role-limits/v1` 계약이다. 작업자·작성자·사용자 시뮬레이터 요청 수와 입력 bytes를 각각 필수로 받는다. [합성 fixture](../tests/fixtures/execution-limits-v1.json)와 음성 사례를 기본 설계 검사에서 확인한다. 기존 프로필의 합산 한도·journal v1.1/v1.2를 바꾸지 않으며 [적용 범위](../docs/development/runtime-hardening.md)를 따른다.

새 구현 기준은 **v1.1**이다. 기존 v1.0의 `$id`·내용·fixture·draft는 호환 확인용으로 그대로 보존한다. v1.1은 반환 참조·공개 경험·지침 필드의 의미도 바꾸므로 버전 없이 교환하지 않는다.

자동 본문 제공은 기존 v1.1 사건을 유지하면서 **별도 v1.2 확장**을 쓴다. [event](v1.2/event.schema.json)는 `memory.preloaded`와 snapshot 출처의 입력 포함, [manifest](v1.2/input-manifest.schema.json)는 snapshot binding, [journal](v1.2/journal-state.schema.json)은 기존/확장 사건을 함께 담는다. 이전 로그를 새 버전으로 덮어쓰지 않는다. [실행 프로필](delivery-comparison.schema.json)의 버전 1.0은 이 기록 버전과 별개다. [동작과 한계](../docs/development/tau2-delivery.md)를 따른다.

| 파일 | 역할 |
| --- | --- |
| [first-customer-query.schema.json](first-customer-query.schema.json) | 실제 무기억 작업자의 첫 요청에서 고객의 첫 발화만 교체하는 진단 입력 v1. 원본 요청과 실험자가 작성한 발화의 출처를 함께 보존한다. [실험 설정](../protocols/pilots/tau2-return-date-calibration-v1.json) |
| [checkpoint-customer-query.schema.json](checkpoint-customer-query.schema.json) | 도구 결과를 이미 본 무기억 상담의 마지막 고객 발화를 교체하는 v1 진단 입력. 앞선 발화와 도구 결과 전체의 해시 및 원본 요청을 함께 검증한다. [입력 예시](../tests/fixtures/checkpoint-customer-query-v1.json) · [결제·취소 업무 적용](../protocols/pilots/tau2-multi-workflow-v1.json) |
| [public-profile-read.schema.json](public-profile-read.schema.json) | 같은 상담에서 기록된 단일 고객 프로필 조회와 반환을 공개 첫 발화 뒤에 붙이는 v1 진단 입력. 발화의 고객, 조회 인수와 반환의 고객, 호출 ID가 일치해야 한다. [입력 예시](../tests/fixtures/public-profile-read-v1.json) · [적응형 후속 비교](../protocols/pilots/tau2-multi-workflow-profile-step-v1.json) |
| [baseline-screening.schema.json](baseline-screening.schema.json) | 무기억 train 선택·반복별 seed·native 한도·run/전체 guard의 별도 배치 계약. 기존 기록 v1.1과 연결 파일은 유지 |
| [v1.1/run-plan.schema.json](v1.1/run-plan.schema.json) | 단계별 지침, 사용자 시뮬레이터·native 한도·응답 캐시 확인·adapter 설정 해시 |
| [v1.1/event.schema.json](v1.1/event.schema.json) | 요청 manifest·과제 입력 참조, 공개 경험 접두부, 반환/자기 작성 출처 |
| [v1.1/input-manifest.schema.json](v1.1/input-manifest.schema.json) | 실제 요청의 출처 binding과 렌더링된 기억 구간 |
| [task-start-v1.schema.json](task-start-v1.schema.json) | 동일 과제 첫 판단의 벤치·버전·과제·초기 조건 해시와 메모 제공 전 공개 입력. 공개 입력의 구조는 벤치 어댑터가 검증한다. |
| [v1.1/journal.schema.json](v1.1/journal.schema.json) | 모든 사건을 확정하는 journal envelope v1.0. 사건 버전과 별개 |
| [v1.1/journal-state.schema.json](v1.1/journal-state.schema.json) | 첫 코어의 journal envelope v1.1. 불변 내부 상태 참조와 상태 전용 commit |
| [기존 run-plan](run-plan.schema.json), [기존 event](event.schema.json) | v1.0 호환 자료, 새 필드로 덮어쓰지 않음 |
| [connection-check-v1.1.schema.json](connection-check-v1.1.schema.json) | Luna/DeepSeek 1017의 역할별 제공 경로·native seed 전달을 고정한 현재 연결 확인 프로필 |
| [connection-check.schema.json](connection-check.schema.json) | 이전 GPT-4.1 연결 확인의 v1.0 호환 자료 |
| [memory-connection-check.schema.json](memory-connection-check.schema.json) | 기존 모델/지침의 해시 참조·두 회차 정책·전체 한도를 고정하는 별도 guided 기억 연결 진단 |
| [autonomy-pilot-v1.1.schema.json](autonomy-pilot-v1.1.schema.json) | 기존 관측 snapshot의 해시/출처, 세 조건·블록/seed·선택적 조회·전체 한도를 고정한 파일럿 |
| [autonomy-pilot.schema.json](autonomy-pilot.schema.json) | 원천 경험과 선택적 사후 작성을 새로 실행한 v1.0 파일럿의 보존 계약 |

설정 예시는 [현재 v1.1 초안](../protocols/drafts/tau2-two-pass-v1.1.json), 사건과 요청·반환 예시는 [v1.1 fixture](../tests/fixtures/contracts-v1.1/README.md)에 있다. [v1.0 초안](../protocols/drafts/tau2-two-pass.json)과 [기존 fixture](../tests/fixtures/contracts/README.md)도 계속 검증한다. v1.0에서 없는 provenance를 임의 생성해 v1.1의 검증을 통과시켰다고 보고하지 않는다.

JSON Schema는 Draft 2020-12다. draft에는 모델 파라미터·과제 manifest·반복·페이지·중단 설정의 null이 있다. ready는 주요 필드와 유한 호출 또는 시간 조건을 요구하지만, 형식 통과만으로 실행 준비가 완료되지는 않는다.

조건 ID·출처 분할·배정·프롬프트 해시·실제 모델 지원·은행 권한·어댑터 capability 등은 구현할 계획 검증기의 의미 검사 대상이다. [실행 준비 계약](../docs/design/architecture.md#10-설정과-실행-준비-판정)을 따른다.

현재 오프라인 확인:

```bash
uv run python scripts/validate_design.py
```

검증기는 두 버전의 스키마·합성 예시를 검사한다. v1.1은 journal 연결·승인 출처·요청 전체의 binding·반환/자기 작성 구간·노출 사건 완전성을 예시 범위에서 확인한다. 음성 사례는 `uv run python -m unittest discover -s tests`로 실행한다. [코어 계약의 한계](../docs/design/core-contracts.md#5-합성-검증의-범위와-남은-구현)를 따른다. 모델 API·벤치·내구성 있는 기록자나 라우터를 실행하지 않는다.

일반 실험 계획 v3는 서로 다른 원천·목표 과제 쌍과 목표별 행동 판독 규칙을 선언한다. 기존 v1/v2 기록과 리테일 재방문 전용 계획은 각자 원래 형식을 따른다. [계획 스키마](study-plan-v1.schema.json) · [행동 전이 배정](../docs/development/study-plans.md#서로-다른-과제-사이의-행동-전이)
