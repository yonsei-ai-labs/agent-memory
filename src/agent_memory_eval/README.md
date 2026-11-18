# 공통 패키지 배치

파일 코어·가짜 실행·원본 재계산이 동작한다. [실행과 검증](../../docs/development/offline-core.md)을 따른다. [무기억 연결](../../docs/development/tau2-connection.md)과 [τ² 기억/두 회차 연결](../../docs/development/tau2-memory.md)이 있으며 일반 연구 실행과 통계 실증은 후속이다. 저장소 checkout의 스키마를 사용하며 과거 연구 코드를 import하지 않는다.

| 패키지 | 현재 구현과 후속 책임 |
| --- | --- |
| `contracts` | `RunContext`·`MemoryPolicy` 타입과 JSON/사건/출처 검사. 실제 계획의 의미/준비 검사는 후속 |
| `contracts.execution` | opt-in `ExecutionLimits`: 역할별 요청 수·입력 bytes, 기존 프로필의 합산 한도는 유지 |
| `experiments` | 기존 API의 작성 단독 조합: 고정 source·실행 전 검사·clean 동결·전체 guard·배정·snapshot 크기 검사 |
| `runner` | `OfflineRunner`, native `Tau2MemoryRunner`·세 조건 파일럿, [고정 은행 전달 비교](../../docs/development/tau2-delivery.md)의 과제·조건·반복 배정과 준비 검사. `runner.router`는 호환 import. 일반 계획·누적·교차·개입 실행은 후속 |
| `memory` | `MemoryService`의 저장·불변 버전·은행·snapshot/fork/compose·멱등성, snapshot 가져오기, `ToolRouter`, 공개 경험의 피드백별 선택적 작성과 공통 과거 메모 renderer |
| `policies` | `contracts.core.MemoryPolicy`와 guided 단계별 작성/조회 지침. 연구용 표현·조회/가공 조합 운용은 후속 |
| `adapters` | `FakeBenchmark`, pinned τ² 무기억/기억 binding·업무 projection·사후 작성. AppWorld·BFCL은 후속 |
| `models` | `FakeWorker`와 OpenRouter 전송·사용량/비용·호출/시간/금액 guard |
| `recording` | `Journal`·`SourceRegistry`·Recording/OfflineGateway의 파일 원본·출처·자기 작성/반환/입력 포함, v1.2 snapshot 자동 제공과 구간 검증, writer-local blob 검증 캐시 |
| `analysis` | 합성 재계산, native 연결/파일럿의 출처·노출·비용·채점 audit와 조건별 기술 통계, 공통 요청/노출 감사·작성 피드백 제한 감사·native ID 과정 trace. 효과·독립 근거 주석·통계 추론은 후속 |

[상세 설계](../../docs/design/architecture.md)와 [개발 계획](../../docs/development/roadmap.md)의 전체 연구 범위는 유지한다. 가짜 환경의 성공을 실제 모델/벤치의 성능으로 표시하지 않는다.

[무기억 탐색 실행](../../docs/development/tau2-baseline.md)은 `runner/tau2_baseline.py`·`analysis/tau2_baseline.py`·별도 CLI로 준비 검사·배정·회차 격리·run/전체 중단·역할별 비용·원본 재검증을 구현했다. 실제 배정 60개 중 55회 채점·오류 1회·미시작 4회의 [기존 결과](../../docs/reports/20261115-tau2-baseline-screening-results.md)를 보존한다. 비용이 미정인 draft는 live를 거부한다.

[작업자 작성 기억](../../docs/development/worker-written-memory.md)은 공통 API·native mock 통합과 보존된 무기억 경험의 요청 준비를 지원한다. `experiments.writing`은 기존 API를 조합한 작성 전용 실행이며, 미정 초안은 유료 호출을 차단한다. 실제 작업자 작성 기억의 효과 측정은 준비 전이다.

[작성 기억 비교의 타당성 보완](../../docs/development/written-comparison-validity.md)은 `experiments.writing_comparison`의 v2 선정·전체 receipt·원본 재구성 감사와 `analysis.written_comparison`의 공개 근거 packet·rubric·수하물/증서 인수 검사를 제공한다. 준비에는 모델 호출이 없고, 좁은 검사 통과는 승인 적합성·공식 성공·독립 의미 판정이 아니다. 기존 v1 준비 초안과 과거 결과는 보존한다.

[고정 기억의 한 응답 실행](../../docs/development/snapshot-replay-runtime.md)은 `runner.snapshot_replay`가 준비 hash와 같은 입력을 v1.2 게이트웨이로 기록하고, `analysis.snapshot_replay`가 실제 기억 구간·원응답·비용·봉인된 은행을 재검증한다. 새 응답을 공개 근거의 좁은 검사에 연결할 수 있다. 네 조건의 전체 배치·새 유료 프로토콜·공식 성공/정책 효과 측정은 이 API의 구현 범위에 포함하지 않는다.

[추가 실행 보완](../../docs/development/runtime-hardening.md)은 공개 writer 응답의 필드별 출처, 전체 대화/비용 감사, 역할별/입력 한도, 공식 성공과 구분한 도달 상태 진단과 clean 구현 동결 검사를 제공한다. 새 작성 전용 진입점은 이를 사용하며 기존 batch의 legacy 한도는 유지한다.

네 조건 전체는 `experiments.written_replay_batch`와 `analysis.written_replay_batch`가 연결한다. 모든 작성·통제 은행의 출처, 공유 전송과 작성 배정의 대응, 미확인 비용·중단 이후의 생략을 보존한다. [배치 실행과 검증](../../docs/development/written-replay-batch.md)
