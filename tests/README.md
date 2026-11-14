# 검증 자료와 테스트 배치

현재 제공하는 것은 [합성 계약 fixture](fixtures/contracts/README.md)와 읽기 전용 검증기, 데이터 보관본 검증기의 손상 탐지 테스트다. 실제 에이전트·벤치 행동 테스트가 구현됐다는 뜻은 아니다.

```bash
uv run python scripts/validate_design.py
uv run python scripts/check_repository.py
uv run python scripts/verify_legacy_data.py
uv run python -m unittest discover -s tests -p 'test_legacy_data_archive.py'
```

기능 구현 이후에는 기억 버전·멱등성·은행 복제·세션 분리·목록/본문 노출·도구 라우팅·공식 채점·비용·중단/재개의 행동 테스트를 추가한다. 세부 완료 기준은 [개발 계획](../docs/development/roadmap.md)과 [검증 시나리오](../docs/design/architecture.md#13-재개와-검증-시나리오)를 따른다.

[보관본 테스트](test_legacy_data_archive.py)는 작은 임시 파일에서 bytes 변경·누락·미등록 추가·경로 이탈·symlink·현재 출처 변경을 탐지한다. 큰 과거 데이터를 테스트 fixture로 복제하거나 모델을 호출하지 않는다. 첫 런타임 작업의 검증은 [구현 명세](../docs/development/first-implementation.md)를 따른다.
