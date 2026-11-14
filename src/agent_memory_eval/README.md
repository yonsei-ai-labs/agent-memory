# 공통 패키지 배치

파일 코어·가짜 실행·원본 재계산이 동작한다. [실행과 검증](../../docs/development/offline-core.md)을 따른다. [OpenRouter·τ² 무기억 연결](../../docs/development/tau2-connection.md)도 확인했으며 τ² 기억 통합과 통계 실증은 후속이다. 저장소 checkout의 스키마를 사용하며 과거 연구 코드를 import하지 않는다.

| 패키지 | 현재 구현과 후속 책임 |
| --- | --- |
| `contracts` | `RunContext`·`MemoryPolicy` 타입과 JSON/사건/출처 검사. 실제 계획의 의미/준비 검사는 후속 |
| `runner` | `OfflineRunner`·`ToolRouter`의 사전 배정·수집/독립 probe·혼합 호출. 누적·교차·개입 실행은 후속 |
| `memory` | `MemoryService`의 저장·불변 버전·은행·snapshot/fork/compose·멱등성 |
| `policies` | 공통 허용/한도 규칙은 현재 `contracts.core.MemoryPolicy`. 실제 작성·조회·가공 지침 운용은 후속 |
| `adapters` | `FakeBenchmark`와 pinned τ² 무기억 binding. τ² 기억 통합·AppWorld·BFCL은 후속 |
| `models` | `FakeWorker`와 OpenRouter 전송·사용량/비용·호출/시간/금액 guard |
| `recording` | `Journal`·`SourceRegistry`·Recording/OfflineGateway의 파일 원본·출처·자기 작성/반환/입력 포함 |
| `analysis` | 합성 사건/결과 짝·노출·결측 재계산. 효과·근거 주석·통계·실제 비용 분석은 후속 |

[상세 설계](../../docs/design/architecture.md)와 [개발 계획](../../docs/development/roadmap.md)의 전체 연구 범위는 유지한다. 가짜 환경의 성공을 실제 모델/벤치의 성능으로 표시하지 않는다.
