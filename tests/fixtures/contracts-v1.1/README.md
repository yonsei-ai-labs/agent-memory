# v1.1 합성 계약 예시

모든 사건은 `record_kind=illustrative`다. 합성 수집 run과 새 probe run의 연결을 보여주며 실행된 모델·업무나 성능 자료가 아니다. 사용량·비용·공식 성공값은 null이다.

- `log.jsonl`: 28 commit, 29 사건. run 시작/종료·요청/응답·업무·채점 결측·기억 연산을 한 순서에 둔다.
- `experience.jsonl`: 생산 사건과 연결한 공개 경험의 접두부. 이후 관측 추가는 이전 접두부를 변경하지 않는다.
- `sources.json`: 승인 템플릿, 과제/업무 입력, 자기 작성 응답, 기억 반환의 출처 목록. 검증 예시의 신뢰 기준이며 모델 입력에 목록 전체를 보내지 않는다.
- `examples/`: 요청/응답·manifest·도구 결과·기억·snapshot. 읽기 쉬운 경로를 쓰는 합성 묶음이며 실제 blob 저장소 구현이 아니다.
- `tool-batches.json`: 원래 실행/결과 순서를 유지한 혼합 호출과 종료 도구 batch의 사전 거절을 나타낸 합성 trace. 도구를 실행하지 않는다.

입력 구간은 6개다. 저장 인수의 제목/본문은 자기 작성 2개, list 제목은 후속 두 요청에서 각각 1개, get 제목/본문은 2개로 기록한다. 자기 작성에는 가짜 반환/항목 ID가 없고 공개 list에는 은행 revision이나 본문이 없다. 한글·개행·따옴표를 포함해 원문과 JSON 렌더링 해시를 구분한다.

저장소 루트에서 확인한다.

```bash
uv run python scripts/validate_design.py
uv run python -m unittest discover -s tests -p 'test_contract_v11.py'
```

예시는 [builder](../../contract_fixture_v11.py)로 재생성할 수 있고 테스트는 임시 디렉터리에서 생성한다. 실제 기록자의 동기화·장애 복구·bank fork/compose나 실제 세션 분리를 입증하는 자료는 아니다. [계약과 검증 범위](../../../docs/design/core-contracts.md)를 따른다.
