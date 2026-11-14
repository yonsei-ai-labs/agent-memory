# 공통 패키지 배치

파일 코어·가짜 실행·원본 재계산이 동작한다. [실행과 검증](../../docs/development/offline-core.md)을 따른다. [무기억 연결](../../docs/development/tau2-connection.md)과 [τ² 기억/두 회차 연결](../../docs/development/tau2-memory.md)이 있으며 일반 연구 실행과 통계 실증은 후속이다. 저장소 checkout의 스키마를 사용하며 과거 연구 코드를 import하지 않는다.

| 패키지 | 현재 구현과 후속 책임 |
| --- | --- |
| `contracts` | `RunContext`·`MemoryPolicy` 타입과 JSON/사건/출처 검사. 실제 계획의 의미/준비 검사는 후속 |
| `runner` | `OfflineRunner`·`ToolRouter`, native `Tau2MemoryRunner`·세 조건 파일럿, [고정 은행 전달 비교](../../docs/development/tau2-delivery.md)의 과제·조건·반복 배정과 준비 검사. 일반 계획·누적·교차·개입 실행은 후속 |
| `memory` | `MemoryService`의 저장·불변 버전·은행·snapshot/fork/compose·멱등성, 관측 source의 원본/버전을 보존한 snapshot 가져오기 |
| `policies` | `contracts.core.MemoryPolicy`와 guided 단계별 작성/조회 지침. 연구용 표현·조회/가공 조합 운용은 후속 |
| `adapters` | `FakeBenchmark`, pinned τ² 무기억/기억 binding·업무 projection·사후 작성. AppWorld·BFCL은 후속 |
| `models` | `FakeWorker`와 OpenRouter 전송·사용량/비용·호출/시간/금액 guard |
| `recording` | `Journal`·`SourceRegistry`·Recording/OfflineGateway의 파일 원본·출처·자기 작성/반환/입력 포함, v1.2 snapshot 자동 제공과 구간 검증 |
| `analysis` | 합성 재계산, native 연결/파일럿의 출처·노출·비용·채점 audit와 조건별 기술 통계, 원본과 버전을 분리한 이관 요약 제외 행동 비교. 효과·근거 주석·통계 추론은 후속 |

[상세 설계](../../docs/design/architecture.md)와 [개발 계획](../../docs/development/roadmap.md)의 전체 연구 범위는 유지한다. 가짜 환경의 성공을 실제 모델/벤치의 성능으로 표시하지 않는다.
