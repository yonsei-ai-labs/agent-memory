# 검증 자료와 테스트 배치

현재 제공하는 것은 [합성 계약 fixture](fixtures/contracts/README.md)와 읽기 전용 검증기다. 실제 에이전트·벤치 행동 테스트가 구현됐다는 뜻은 아니다.

```bash
uv run python scripts/validate_design.py
uv run python scripts/check_repository.py
```

기능 구현 이후에는 기억 버전·멱등성·은행 복제·세션 분리·목록/본문 노출·도구 라우팅·공식 채점·비용·중단/재개의 행동 테스트를 추가한다. 세부 완료 기준은 [개발 계획](../docs/development/roadmap.md)과 [검증 시나리오](../docs/design/architecture.md#13-재개와-검증-시나리오)를 따른다.
