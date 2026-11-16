# 공통 패키지 배치

파일 코어·가짜 실행·원본 재계산이 동작한다. [실행과 검증](../../docs/development/offline-core.md)을 따른다. [무기억 연결](../../docs/development/tau2-connection.md)과 [τ² 기억/두 회차 연결](../../docs/development/tau2-memory.md)이 있으며 일반 연구 실행과 통계 실증은 후속이다. 저장소 checkout의 스키마를 사용하며 과거 연구 코드를 import하지 않는다.

| 패키지 | 현재 구현과 후속 책임 |
| --- | --- |
| `contracts` | `RunContext`·`MemoryPolicy` 타입과 JSON/사건/출처 검사. 실제 계획의 의미/준비 검사는 후속 |
| `contracts.execution` | opt-in `ExecutionLimits`: 역할별 요청 수·입력 bytes, 기존 프로필의 합산 한도는 유지 |
| `runner` | `OfflineRunner`, native `Tau2MemoryRunner`·세 조건 파일럿, [고정 은행 전달 비교](../../docs/development/tau2-delivery.md)의 과제·조건·반복 배정과 준비 검사. `runner.router`는 호환 import. 일반 계획·누적·교차·개입 실행은 후속 |
| `memory` | `MemoryService`의 저장·불변 버전·은행·snapshot/fork/compose·멱등성, snapshot 가져오기, `ToolRouter`, 공개 경험의 피드백별 선택적 작성과 공통 과거 메모 renderer |
| `policies` | `contracts.core.MemoryPolicy`와 guided 단계별 작성/조회 지침. 연구용 표현·조회/가공 조합 운용은 후속 |
| `adapters` | `FakeBenchmark`, pinned τ² 무기억/기억 binding·업무 projection·사후 작성. AppWorld·BFCL은 후속 |
| `models` | `FakeWorker`와 OpenRouter 전송·사용량/비용·호출/시간/금액 guard |
| `recording` | `Journal`·`SourceRegistry`·Recording/OfflineGateway의 파일 원본·출처·자기 작성/반환/입력 포함, v1.2 snapshot 자동 제공과 구간 검증, writer-local blob 검증 캐시 |
| `analysis` | 합성 재계산, native 연결/파일럿의 출처·노출·비용·채점 audit와 조건별 기술 통계, 공통 요청/노출 감사·작성 피드백 제한 감사·native ID 과정 trace. 효과·독립 근거 주석·통계 추론은 후속 |

[상세 설계](../../docs/design/architecture.md)와 [개발 계획](../../docs/development/roadmap.md)의 전체 연구 범위는 유지한다. 가짜 환경의 성공을 실제 모델/벤치의 성능으로 표시하지 않는다.

[무기억 탐색 실행](../../docs/development/tau2-baseline.md)은 `runner/tau2_baseline.py`·`analysis/tau2_baseline.py`·별도 CLI로 준비 검사·배정·회차 격리·run/전체 중단·역할별 비용·원본 재검증을 구현했다. 실제 배정 60개 중 55회 채점·오류 1회·미시작 4회의 [기존 결과](../../docs/reports/20261115-tau2-baseline-screening-results.md)를 보존한다. 비용이 미정인 draft는 live를 거부한다.

[작업자 작성 기억](../../docs/development/worker-written-memory.md)은 공통 API와 native mock 통합까지 검증했다. 새 유료 실행 프로필·실제 효과 측정·과거 baseline transcript 자동 import는 준비 전이다.

[추가 실행 보완](../../docs/development/runtime-hardening.md)은 공개 writer 응답의 필드별 출처, 전체 대화/비용 감사, 역할별/입력 한도, 공식 성공과 구분한 도달 상태 진단과 clean 구현 동결 검사를 제공한다. 기존 batch 실행기는 legacy 한도를 사용하며 새 유료 진입점의 연결은 후속이다.
