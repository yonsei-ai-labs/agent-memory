# 설정과 사건 계약

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
