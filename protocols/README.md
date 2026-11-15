# 실험 계획

[무기억 탐색 초안](drafts/tau2-baseline-screening-v1.json)은 별도 배치 실행기로 준비 검사·실행·감사를 지원한다. 공식 train 30개 × 2회와 반복별 seed·한도를 검토용으로 제안하며, 전체 비용 상한은 null이라 live가 차단된다. [구현과 검증](../docs/development/tau2-baseline.md)을 따르고 이전 48회의 승인·연결 스키마·원본은 유지한다.

[현재 τ² 두 회차 초안 v1.1](drafts/tau2-two-pass-v1.1.json)은 같은 Luna 작업자에서 기억 없음·종료 후 작성·수행 중 작성·병행을 비교하는 설정 예시다. 자기 경험 은행과 여러 과제의 혼합 은행을 지원하는 방향으로 구성했다. [v1.0 초안](drafts/tau2-two-pass.json)은 호환 확인용으로 보존하며 동시에 현재 실행 계획으로 사용하지 않는다.

상태는 `draft`다. 모델 파라미터·벤치 commit·도메인·과제·반복·페이지·중단·주석·통계·비용 배분의 미정 값은 null 또는 미확정 표시로 남긴다. v1.1은 단계별 지침과 사용자 시뮬레이터·native 한도·응답 캐시·adapter 설정을 추가했으며 실제 값은 미정이다. 이 파일은 실제 실행 결과나 실행 준비가 끝난 계획이 아니다.

실제 실행 시 새 계획 revision과 ID로 구체적인 조건·배정·seed·출처·비용·중단 규칙을 고정한다. 초안을 임의 숫자로 채워 사용하지 않는다. 내용은 [실험 프로토콜](../docs/research/experiment-protocol.md), 형식은 [설정 스키마 v1.1](../schemas/v1.1/run-plan.schema.json), 준비 검사 의미는 [코어 계약](../docs/design/core-contracts.md#4-v11과-실제-실행-준비)을 따른다.

연구용 ready 계획을 해석하는 범용 실행 CLI는 구현 전이다. 계약 검증은 저장소 루트에서 실행한다.

[고정 은행 전달 비교 초안](drafts/tau2-delivery-comparison-v1.json)은 별도 실행기가 읽는다. 무기억·빈 은행·자율 조회·본문 시작 제공, 과제별 출처 그룹·반복·배정을 설정한다. 실제 과제·은행·반복·페이지·guards와 preload의 조회 도구 유지 여부는 미정이다. [전달 비교 안내](../docs/development/tau2-delivery.md)의 로컬 확인·명시적 실행·원본 audit를 지원하며, 범용 연구 계획을 ready로 승격시키지 않는다.

별도 [개발 실행 프로필](pilots/tau2-delivery-development-v1.json)은 train 40·47·49 × 네 조건 × 4회, 같은 guided 은행, Luna low·DeepSeek 1017, 조회 도구 유지와 승인 비용 상한 $3을 고정했다. [실행 전 방법](../docs/research/delivery-development-pilot.md)·[동결 해시](../artifacts/pilots/20261114-tau2-delivery-development-protocol.json)·[실제 결과](../docs/reports/20261114-tau2-delivery-development-results.md)를 연결하며 기존 초안은 유지한다.

[실제 OpenRouter·τ² 연결 확인](checks/openrouter-tau2-smoke.json)은 별도 `connection_check` 프로필이다. [실행 안내](../docs/development/tau2-connection.md)에 따라 제한된 무기억 1과제에서 연결·계측을 확인한다. 이 프로필을 연구용 ready 계획이나 정책 효과 결과로 사용하지 않는다.

[기억 두 회차 연결 확인](checks/openrouter-tau2-memory-smoke.json)은 별도 `memory_connection_check`/`guided_check`다. 모델/사용자 연결 설정과 단계별 지침의 해시, 사후 작성/새 snapshot 복제본 조회 정책, 전체 실행 한도를 고정한다. [실행·검증 안내](../docs/development/tau2-memory.md)를 따르며 자율 기억 정책 효과나 일반 연구 계획을 대신하지 않는다.

[현재 자율 조회 파일럿 v1.1](pilots/tau2-autonomous-memory-v1.1.json)은 기존 관측 기억을 고정하고 무기억·빈은행·기억은행을 각 3회 비교한다. 같은 실행 경로·선택적 조회 지침·블록 순서·사용량/비용 한도를 고정하며 [안내](../docs/development/tau2-autonomy-pilot.md)에 해석 범위를 적었다. [v1.0](pilots/tau2-autonomous-memory-v1.json)은 새 사후 작성에서 저장이 없었던 첫 시도의 계약으로 보존한다. 연구 초안의 미정 값을 채우거나 범용 ready 실행기로 취급하지 않는다.

```bash
uv run python scripts/validate_design.py
```
