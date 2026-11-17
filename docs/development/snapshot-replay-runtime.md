# 고정 기억의 한 응답 실행과 원본 감사

2026-11-18 · 실행 경로의 기능 검증 · 새 유료 모델 실험 없음

[작성 기억 비교 준비](../research/written-memory-replay-v1.md)의 다음 구성요소로 `run_snapshot_replay`를 추가했다. 보존한 결정 문맥과 불변 snapshot을 받아 새 세션·새 은행 복제본에서 한 응답을 기록한다. 실제 전송에는 기존 `RecordingGateway`와 OpenRouter client를 사용한다. 준비 입력과 최종 전송 바이트가 다르면 전송 전에 거절한다.

현재 연결은 **한 응답 실행·출처·비용 기록·재검증**까지다. 네 조건의 반복/배정표, 독립 의미 판정, 새 실행 한도와 동결 프로토콜을 자동으로 정하지 않는다. v1/v2 초안은 그대로 draft이며 새 `--live` 진입점은 만들지 않았다. 이번 행동 검증은 합성 응답과 모의 HTTP이고, 실제 Luna 기억 효과나 에피소드 성공의 관측이 아니다.

## 기록하는 것

- 공개 결정 문맥의 원래 도구·모델 파라미터·대화를 유지하고, 같은 renderer로 snapshot 본문을 제공한다. 원본과 다른 요청 hash, 조회/쓰기 허용 정책, 다른 모델 경로·입력 한도, 중복 run ID는 은행 생성 전에 거절한다.
- 각 실행은 독립 세션과 읽기 전용 은행 복제본을 사용한다. 종료 시 같은 항목·버전·순서의 snapshot을 남기고 복제본을 닫는다. 원은행은 수정하지 않는다. 빈 snapshot도 그대로 실행한다.
- `memory.preloaded`를 출처로 등록하고 `memory.input_included`에 제목·본문의 실제 입력 구간을 연결한다. 본문을 일반 template로 위장해 같은 바이트를 보내도 감사에서 거절한다.
- 원응답·사용량·보고 비용·제공자·전송 상태를 보존한다. 모델/제공자 불일치, 거절·잘린 응답·잘못된 종료 형식은 정상 응답으로 승인하지 않는다. 공개 응답 사본에는 비공개 추론을 넣지 않는다.
- 요청된 업무/기억 도구는 실행하지 않는다. `outcome.scored`와 결과의 공식 성공은 항상 미측정이다. 확인 질문과 새 조회 제안도 완료한 업무로 세지 않는다.

입력 한도 초과도 요청 구성과 전송 거절을 기록한다. 이때 준비된 기억 구간은 있으나 `delivery_status=not_submitted`이고 수신 응답과 연결된 구간은 0이다. `received_memory_spans`는 **전송 계층에서 응답을 받은 요청에 연결된 구간 수**이며 모델의 실제 이해·활용을 뜻하지 않는다. HTTP 오류에도 전송 응답은 있을 수 있으므로 응답의 상태·오류 코드와 함께 읽는다.

정상 응답의 비용이 없으면 공개 응답을 보존하면서 `unknown_cost=true`, `stop_required=true`를 반환한다. 같은 `SpendGuard`는 다음 전송을 차단한다. 오류에도 `stop_required=true`를 반환하며 자동 재시도나 성공할 때까지 대체 실행하지 않는다. 호출·시간·금액의 전체 한도와 중단/생략 배정 기록은 후속 배치 실행기의 책임이다. 이 저수준 API만으로 새 실험이 승인되거나 동결된 것은 아니다.

## 호출 경계

```python
result = run_snapshot_replay(
    journal, decision_ref, snapshot_id,
    policy=read_only_preload_policy,
    instruction=replay_instruction,
    condition_id=condition_id,
    client=verified_client_with_shared_spend_guard,
    execution_limits=ExecutionLimits(
        worker_requests=1, writer_requests=0, user_simulator_requests=0,
        max_input_bytes=explicit_input_limit,
    ),
    expected_request_sha256=prepared["request_ref"]["sha256"],
)
```

작업자 한 요청은 이 진단의 의미이며 실험 반복 수가 아니다. 입력 한도·전체 비용·timeout·조건 순서는 실행 프로토콜에서 명시한다. 준비 manifest의 160개 행이나 48개 고유 입력을 그대로 유료 배정 수로 간주하지 않는다. 모델 작성 조건과 빈 조건은 기존 네 조건 renderer의 바이트를 재사용할 수 있음을 행동 테스트로 확인했다. 일반/연구자 메모의 출처를 보존한 은행 가져오기와 전체 조건 배정은 아직 이 API가 수행하지 않는다.

기억 내용이 원래 작성자의 산출물인지는 별도의 작성/가져오기 감사로 확인한다. 이 실행기의 감사는 전달된 로컬 snapshot과 실제 입력의 일치를 확인하며, 임의로 만든 은행을 자율 작성 기억으로 인증하지 않는다.

## 재검증과 좁은 응답 판정

```bash
uv run --frozen python scripts/review_snapshot_replays.py --output PATH_TO_RECORDED_JOURNAL
uv run --frozen python scripts/review_snapshot_replays.py \
  --output PATH_TO_RECORDED_JOURNAL \
  --preparation PATH_TO_V2_PREPARATION \
  --rubrics artifacts/analyses/20261117-written-memory-public-rubrics.json
```

감사는 원본 폴더나 API 연결 없이 보존된 capsule·snapshot·요청 manifest·원응답으로 재구성한다. 입력/출처, 요청 수, 비용, 공개 응답 사본, 종료 순서, 결과와 은행 봉인을 대조한다. 준비만 있는 journal은 실행 완료로 통과시키지 않는다. 결과 행은 journal의 실제 시작 순서로 반환하며 무작위 run ID의 정렬 순서를 쓰지 않는다.

`request_audit`는 journal 전체의 요청을 검사한다. 상위 `model_requests`·비용과 `replays`는 이번 재전송 run만 집계하므로 같은 journal의 작성 요청 비용과 혼동하지 않는다. 작성·수행의 전체 비용 비교는 배치 분석에서 별도로 연결해야 한다.

두 번째 명령은 [공개 근거 검사](written-comparison-validity.md)에 새 응답을 연결한다. 각 원요청 hash뿐 아니라 공개 메시지 전체를 실제 capsule과 대조한다. 질문/조회, 수량·증서 위반, 정상 검사 통과, 미확정을 구분하며 오류/부분 응답에는 판정값을 만들지 않는다. 비용 미확인은 응답을 버리지 않고 별도 상태로 남긴다. 잠정 rubric과 단일 검토자의 한계는 유지하고, 결과를 합쳐 정책 효과나 공식 성공률을 계산하지 않는다. 준비물의 원본 무결성은 기존 `--audit-only`로 별도 확인한다.

## 검증 범위와 다음 연결

[검증 기록](../../artifacts/analyses/20261118-snapshot-replay-runtime-validation.json)에 개발 코드의 파일 hash, 테스트 실행, 기존 48개 준비 입력의 감사, 합성 예시와 읽기 전용 재검증을 연결한다. 코드가 미커밋된 작업 트리의 개발 검증이며 유료 프로토콜 동결은 아니다.

최종 **전체 294개 테스트가 469.205초에 통과**했고 기본 검사 3종도 통과했다. 원본 경로를 옮긴 합성 예시에서 CLI 감사와 공개 판정을 재현했다. 네트워크·프로세스 실행·파일 쓰기/변경을 막은 감사 중 금지된 시도는 0이었고, 대상 110개 파일의 목록·크기·수정 시각이 같았다. 이 예시의 두 정상 응답과 두 오류는 모두 합성이며 실제 모델의 득실 결과가 아니다.

[행동 테스트](../../tests/test_snapshot_replay.py)는 실제 파일 기록자·기억 서비스·게이트웨이·모의 HTTP를 사용한다. 요청 동일성, 빈 기억, 세션/은행 격리, 원본 삭제 후 감사, 입력/금액 한도, 비용 미확인 뒤 차단, HTTP 503, 부분/거절/잘못된 모델 응답, 공개 응답/비용/점수/설정/은행 변조와 판정 연결을 확인한다. 계약 [schema](../../schemas/snapshot-replay.schema.json)와 [합성 예시](../../tests/fixtures/snapshot-replay-v1.json)는 별도로 기본 검사에 포함한다. 일반 journal v1.1과 snapshot journal v1.2는 변경하지 않았다.

후속 [네 조건 배치](written-replay-batch.md)는 빈 작성을 포함한 전체 작성·통제 메모의 출처와 동일 입력의 의존성, 실패·생략·비용 감사를 연결한다. 사례·통제 메모 검토, 유료 반복·예산 확정과 실제 새 응답 관측이 남는다. 원경험/단순 요약 대조, 자연 실행, 자율 조회, 전체 에피소드, 전이·누적·득실과 전체 비용의 연구 범위를 유지한다.
