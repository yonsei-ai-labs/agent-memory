# 검증 자료와 테스트 배치

현재는 파일 코어·입력 구성·혼합 도구·가짜 두 회차의 [실제 오프라인 행동 테스트](test_offline_core.py), [v1.1 합성 계약 fixture](fixtures/contracts-v1.1/README.md)의 음성 사례, 보관본 손상 탐지를 제공한다. [기존 v1.0 fixture](fixtures/contracts/README.md)도 보존한다. 실제 모델·벤치 능력이나 정책 효과의 실증은 아니다.

```bash
uv run python scripts/validate_design.py
uv run python scripts/check_repository.py
uv run python scripts/verify_legacy_data.py
uv run python -m unittest discover -s tests
```

오프라인 코어 26개·계약 26개·보관본 8개·OpenRouter 전송 mock 7개를 실행한다. τ² extra와 고정 checkout이 있으면 native mock 통합 1개도 실행한다. 새 기능/벤치 연결에는 관련 행동 테스트를 추가한다. 현재 지원 범위는 [오프라인 코어 안내](../docs/development/offline-core.md), 전체 목표는 [개발 계획](../docs/development/roadmap.md)과 [검증 시나리오](../docs/design/architecture.md#13-재개와-검증-시나리오)를 따른다.

[v1.1 테스트](test_contract_v11.py) 26개는 journal tail/손상/누락, 재전송 중복, 세션 ID·은행/고정 보기, 요청 출처/역할/시점/완전성, 자기 작성 인수, 공개 경험 접두부, 목록 메타데이터, 합성 혼합 호출 순서와 ready 설정 형식을 확인한다. 매 테스트는 작은 임시 fixture를 만든다. 실제 기록자·라우터·provider 요청을 생성하거나 실행하지 않는다.

[보관본 테스트](test_legacy_data_archive.py)는 작은 임시 파일에서 bytes 변경·누락·미등록 추가·경로 이탈·symlink·현재 출처 변경을 탐지한다. 큰 과거 데이터를 테스트 fixture로 복제하거나 모델을 호출하지 않는다. 첫 런타임 작업의 검증은 [구현 명세](../docs/development/first-implementation.md)를 따른다.

실제 네트워크 연결 확인은 [별도 명령](../docs/development/tau2-connection.md)에서 `--live`로 실행한다. `uv run --extra tau2 python -m unittest discover -s tests`는 native 도구·채점을 포함해 모의 HTTP만 사용한다.
