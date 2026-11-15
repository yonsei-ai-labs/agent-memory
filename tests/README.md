# 검증 자료와 테스트 배치

[무기억 탐색](test_tau2_baseline.py)의 새 12개를 포함해 2026-11-15에 전체 128개가 통과했다. 준비되지 않은 비용·비유한 숫자·변경된 소스·시험 분할·기존 출력 폴더를 거절하고, native 모의 HTTP에서 새 세션/DB·반복별 seed와 사용자 wire seed 생략·개별/전체 요청 중단·미확인 비용·native 단계 한도·출처 보존·원본 재계산·중단 기록·요약 변조를 검증한다. 별도 guard 검사는 두 범위의 요청/금액/시간 거절에서 HTTP 횟수가 부풀지 않는지 확인한다. 기본 검사 3개와 새 스키마 검사도 통과했다. 실제 Luna 실험이나 기억 효용 결과가 아니다. 아래 80·88·109·116개는 이전 단계의 검증 이력이다.

[기억 도구 노출 진단](test_memory_visibility.py) 7개를 추가했다. 텍스트에 적힌 이름과 실제 schema 제공, `none`으로 막힌 선택, 원응답 호출과 텍스트 언급, 다른 run/ID/연산의 반환·누락/중복 반환, 기억 schema/auto/raw call의 HTTP 전송 보존, 저장 JSON의 구조 재현을 확인한다. 이 7개와 기존 native 라우팅·기억 오류·전송 관련 3개를 실행해 통과했다. 직전 전체 109개에 더한 현재 116개 전체는 이번에 다시 실행하지 않았다. 기존 기록 재검사는 [진단 보고서](../docs/reports/20261114-memory-retrieval-diagnosis.md)를 따른다.

현재는 파일 코어·입력 구성·혼합 도구·가짜 두 회차의 [실제 오프라인 행동 테스트](test_offline_core.py), [v1.1 합성 계약 fixture](fixtures/contracts-v1.1/README.md)의 음성 사례, 보관본 손상 탐지를 제공한다. [기존 v1.0 fixture](fixtures/contracts/README.md)도 보존한다. 실제 모델·벤치 능력이나 정책 효과의 실증은 아니다.

```bash
uv run python scripts/validate_design.py
uv run python scripts/check_repository.py
uv run python scripts/verify_legacy_data.py
uv run python -m unittest discover -s tests
```

오프라인 코어 26개·계약 26개·보관본 8개·OpenRouter 전송/프로필 mock 10개를 실행한다. τ² extra와 고정 checkout이 있으면 native 무기억 통합 2개와 [기억 통합](test_tau2_memory.py) 8개를 더해 총 80개다. 기억 통합은 같은 native 환경/채점에서 내부 왕복·혼합 순서·종료 batch·사후 작성 실패·은행 분리·요청 한도·출처/audit를 확인한다. 새 기능/벤치 연결에는 관련 행동 테스트를 추가한다. 현재 지원 범위는 [기억 연결 안내](../docs/development/tau2-memory.md)와 [오프라인 코어 안내](../docs/development/offline-core.md), 전체 목표는 [개발 계획](../docs/development/roadmap.md)을 따른다.

[자율 조회 파일럿](test_tau2_autonomy.py)의 순수 준비 검사 3개와 native mock 5개를 더해 현재 전체는 88개다. 블록/위치 배정·해시 변경 거절·무기억/빈은행/기억은행의 wire 대조·미확인 비용 중단·빈 source의 생략·동일 버전 가져오기·예전 대화 차단·원본 폴더 없이 audit·요약 변조 거절을 확인한다. 모든 unittest는 모의 HTTP만 사용한다.

[행동 보조 분석](test_action_comparison.py) 5개를 더한 93개에, [snapshot 제공](test_preload.py) 8개와 [과제/조건/반복 실행](test_tau2_delivery.py) 8개를 추가했다. extra와 고정 checkout이 있는 전체는 109개다. 한글·따옴표의 구간, v1.1 보존·v1.2 출처, 전송 전 한도·역할/은행 차단, 새 native 세션·DB 격리, 조회 도구 없는 제공, 두 과제의 공식 결과 차이·비용 미확인 생략·보관된 원본 재계산을 검사한다. [v1.2 닫힌 합성 fixture](fixtures/contracts-v1.2/README.md)는 기본 설계 검사에서도 확인한다. 실제 모델 효과 결과는 아니다.

[v1.1 테스트](test_contract_v11.py) 26개는 journal tail/손상/누락, 재전송 중복, 세션 ID·은행/고정 보기, 요청 출처/역할/시점/완전성, 자기 작성 인수, 공개 경험 접두부, 목록 메타데이터, 합성 혼합 호출 순서와 ready 설정 형식을 확인한다. 매 테스트는 작은 임시 fixture를 만든다. 실제 기록자·라우터·provider 요청을 생성하거나 실행하지 않는다.

[보관본 테스트](test_legacy_data_archive.py)는 작은 임시 파일에서 bytes 변경·누락·미등록 추가·경로 이탈·symlink·현재 출처 변경을 탐지한다. 큰 과거 데이터를 테스트 fixture로 복제하거나 모델을 호출하지 않는다. 첫 런타임 작업의 검증은 [구현 명세](../docs/development/first-implementation.md)를 따른다.

실제 네트워크 연결 확인은 [별도 명령](../docs/development/tau2-connection.md)에서 `--live`로 실행한다. `uv run --extra tau2 python -m unittest discover -s tests`는 native 도구·채점을 포함해 모의 HTTP만 사용한다.

[실제 48회 전달 결과](../docs/reports/20261114-tau2-delivery-development-results.md)는 위 unittest와 별도다. 고정 구현·τ² checkout·실행 원본이 있는 환경에서 `uv run --frozen --extra tau2 python scripts/review_tau2_delivery_development.py`로 공식 audit·259개 공개 근거·주석 연결과 receipt를 새 모델 호출 없이 재검증한다. 단일 검토자의 의미 판정 자체를 자동 입증하는 검사는 아니다.
