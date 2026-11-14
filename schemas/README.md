# 설정과 사건 계약

새 구현 기준은 **v1.1**이다. 기존 v1.0의 `$id`·내용·fixture·draft는 호환 확인용으로 그대로 보존한다. v1.1은 반환 참조·공개 경험·지침 필드의 의미도 바꾸므로 버전 없이 교환하지 않는다.

자동 본문 제공은 기존 v1.1 사건을 유지하면서 **별도 v1.2 확장**을 쓴다. [event](v1.2/event.schema.json)는 `memory.preloaded`와 snapshot 출처의 입력 포함, [manifest](v1.2/input-manifest.schema.json)는 snapshot binding, [journal](v1.2/journal-state.schema.json)은 기존/확장 사건을 함께 담는다. 이전 로그를 새 버전으로 덮어쓰지 않는다. [실행 프로필](delivery-comparison.schema.json)의 버전 1.0은 이 기록 버전과 별개다. [동작과 한계](../docs/development/tau2-delivery.md)를 따른다.

| 파일 | 역할 |
| --- | --- |
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
