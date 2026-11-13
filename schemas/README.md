# 설정과 사건 계약

기존 설계의 v1 계약을 이관했다. 의미를 바꾸지 않은 스키마의 `$id`와 `schema_version`을 유지해 기존 합성 자료와의 호환성을 보존한다.

| 파일 | 역할 |
| --- | --- |
| [run-plan.schema.json](run-plan.schema.json) | 설정 타입·기억 조건·주요 실행 준비 제약 |
| [event.schema.json](event.schema.json) | 공통 사건 envelope와 핵심 종류별 payload |

설정 예시는 [τ² 두 회차 초안](../protocols/drafts/tau2-two-pass.json), 사건과 요청·반환 예시는 [계약 fixture](../tests/fixtures/contracts/README.md)에 있다.

JSON Schema는 Draft 2020-12다. draft에는 모델 파라미터·과제 manifest·반복·페이지·중단 설정의 null이 있다. ready는 주요 필드와 유한 호출 또는 시간 조건을 요구하지만, 형식 통과만으로 실행 준비가 완료되지는 않는다.

조건 ID·출처 분할·배정·프롬프트 해시·실제 모델 지원·은행 권한·어댑터 capability 등은 구현할 계획 검증기의 의미 검사 대상이다. [실행 준비 계약](../docs/design/architecture.md#10-설정과-실행-준비-판정)을 따른다.

현재 오프라인 확인:

```bash
uv run python scripts/validate_design.py
```

검증기는 스키마·합성 사건·원본 참조·해시·입력 부분 문자열과 잘못된 형식의 거절을 확인한다. 모델 API와 벤치를 실행하지 않는다.
