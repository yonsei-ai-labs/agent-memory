# 실험 계획

[현재 τ² 두 회차 초안 v1.1](drafts/tau2-two-pass-v1.1.json)은 같은 Luna 작업자에서 기억 없음·종료 후 작성·수행 중 작성·병행을 비교하는 설정 예시다. 자기 경험 은행과 여러 과제의 혼합 은행을 지원하는 방향으로 구성했다. [v1.0 초안](drafts/tau2-two-pass.json)은 호환 확인용으로 보존하며 동시에 현재 실행 계획으로 사용하지 않는다.

상태는 `draft`다. 모델 파라미터·벤치 commit·도메인·과제·반복·페이지·중단·주석·통계·비용 배분의 미정 값은 null 또는 미확정 표시로 남긴다. v1.1은 단계별 지침과 사용자 시뮬레이터·native 한도·응답 캐시·adapter 설정을 추가했으며 실제 값은 미정이다. 이 파일은 실제 실행 결과나 실행 준비가 끝난 계획이 아니다.

실제 실행 시 새 계획 revision과 ID로 구체적인 조건·배정·seed·출처·비용·중단 규칙을 고정한다. 초안을 임의 숫자로 채워 사용하지 않는다. 내용은 [실험 프로토콜](../docs/research/experiment-protocol.md), 형식은 [설정 스키마 v1.1](../schemas/v1.1/run-plan.schema.json), 준비 검사 의미는 [코어 계약](../docs/design/core-contracts.md#4-v11과-실제-실행-준비)을 따른다.

현재 실행기 CLI는 구현 전이다. 계약 검증은 저장소 루트에서 실행한다.

```bash
uv run python scripts/validate_design.py
```
