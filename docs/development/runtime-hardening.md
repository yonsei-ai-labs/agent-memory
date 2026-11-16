# 기억 작성 입력·감사·실행 한도의 추가 보완

2026-11-16 착수 · 모델 호출 없는 개발 검증

작업자 작성 기억의 첫 연결 뒤 추가 검토에서 입력·감사·부분 쓰기의 문제 네 개를 재현했다. 이전 `1e59371`의 실제 worktree에서도 새 회귀 검사 네 개가 모두 실패했고, 수정 후에는 통과했다. [검증 자료](../../artifacts/analyses/20261116-runtime-hardening-validation.json)에 재현 방법·검사·원본 호환 결과를 연결한다.

| 확인한 문제 | 수정 |
| --- | --- |
| 원천 경험을 걸러도 writer의 `reasoning`·provider metadata가 다음 요청에 재입력됨 | 새 writer는 공개 assistant 필드만 전달. 원응답을 보존하고 기존 manifest의 leaf binding으로 각 필드의 실제 출처를 유지 |
| 감사가 첫 세 메시지만 확인해 추가 문맥을 놓침 | 모든 요청의 전체 대화를 응답·도구 결과 순서로 재구성하고, 도구·모델 설정·종료 상태까지 대조 |
| 중복 run ID를 거절하기 전에 은행이 생성됨 | writer와 native episode의 중복 ID를 시작 전에 거절 |
| 비용·사용량이 원응답과 달라도 공통 감사가 통과함 | 원응답 usage·비용 가용성·보고 금액을 대조. 전송 안 된 요청·불확실한 전송·정상 응답의 기억 구간을 분리 집계 |

새 writer의 응답 투영은 `public-assistant/v1`로 기록한다. 기존 기록에서 이 필드가 없으면 원래 응답 형식을 읽는다. 원본·과거 120요청·v1.1/v1.2 manifest 형식은 유지하며 새 출처 종류나 가짜 template 생산자를 만들지 않는다.

## 실행 한도는 명시적으로 선택한다

`contracts.execution.ExecutionLimits`와 [별도 스키마](../../schemas/execution-limits.schema.json)의 `per-role-limits/v1`을 추가했다. 작업자·작성자·사용자 시뮬레이터 요청 수와 입력 bytes를 모두 명시한다. 0은 해당 역할의 요청을 허용하지 않는 값이다. [합성 fixture](../../tests/fixtures/execution-limits-v1.json)의 수치는 테스트용이며 실험 기본값이 아니다.

`Tau2MemoryRunner.episode(..., execution_limits=...)`와 `write_from_experience(..., max_requests=None, execution_limits=...)`에서 사용한다. 이 모드에서는 사용자 시뮬레이터 요청이 작업자 한도를 차감하지 않는다. 한도는 각 run의 `model.request_started`를 역할별로 센다. 동시에 기존 합산 한도를 주는 것은 거절한다. 기존 프로필과 과거 batch 실행기는 원래 합산 한도를 유지한다.

입력 초과는 기억을 자르지 않고 실제 요청·출처·크기를 남긴 뒤 `not_submitted`·비용 `not_applicable`로 종료한다. 입력 구간을 구성한 것과 모델의 정상 응답을 받은 것을 집계에서 구분한다. OpenRouter의 입력 상한·HTTP timeout도 생성자 인수로 받는다. 이전 호출의 기본 64,000 bytes·45초는 유지하며, 새 명시적 입력 상한은 실제 transport와 맞춰야 한다. 은행 크기/복수 기억의 전체 실행 사전 검사는 아직 별도 프로토콜 작업이다.

## 한도 종료의 도달 상태와 공식 성공을 구분한다

고정 τ²의 공식 평가 함수는 정상 종료가 아닌 실행을 성공으로 인정하지 않는다. 따라서 한도 종료를 정상 종료로 바꿔 점수를 만들지 않는다.

새 명시적 한도 모드에서 `assess_on_policy_limit=True`이면 worker의 요청/입력 한도로 중단됐을 때 별도의 `reached-state-diagnostic/v1`을 남긴다. 고정 native 평가기의 DB·substring COMMUNICATE 구성요소를 메모리 사본에서 계산하고, 재생한 상태가 실제 도달 DB와 같은지 확인한다. live DB·native 단계/오류는 바뀌지 않아야 한다. 모델을 쓰는 semantic scorer는 실행하지 않는다. 사용자 시뮬레이터 한도 종료는 이 정책 측 진단에서 제외한다.

mock 통합에서는 승객 수정이 끝난 뒤 다음 worker 요청이 한도에 걸리는 경우를 확인했다. DB 진단은 충족으로 나왔지만 `official_success`는 `null`, grading은 `incomplete`로 보존했다. 이는 중간 상태 진단이며 실제 기억 효과나 공식 성공률이 아니다. 1차 집계에서 한도 종료를 실패/결측으로 어떻게 다룰지는 새 프로토콜에서 정해야 한다.

## 응답 이상과 비용 제한도 기록 가능하게 한다

NaN·무한대 예산과 유효하지 않은 예약 가격은 HTTP 전에 거절한다. 응답 JSON이 null/list/string이거나 usage 형식이 잘못돼도 원문 JSON을 보존하고 완료 사건을 남긴다. 실제 전송 뒤 비용 미확인은 계속 미확인으로 남기며 다음 HTTP를 차단한다. 새 writer는 마지막 답변의 비용이 미확인이어도 완료/빈 기억으로 보고하지 않는다. 유효한 null `tool_calls`와 잘못된 호출 구조도 구분한다.

## 동결은 깨끗한 코드에서만

```bash
uv run --frozen python scripts/freeze_implementation.py --output outputs/NEW_IMPLEMENTATION_FREEZE.json
```

추적 파일 수정·staged 변경·미추적 코드/문서가 있으면 거절하고 출력 파일을 만들지 않는다. 무시된 실행 데이터는 개발 코드 변경으로 세지 않는다. 결과는 immutable 구현 commit·tree·lock 참조이며 실험 설정·예산 승인·분석 재현 commit을 대신하지 않는다. 새 유료 진입점이 이 함수를 호출하게 연결하는 작업은 남아 있다. 기존 실험의 동결 기록을 갱신하지 않는다.

증분 journal/인덱스, baseline/delivery 공통 루프, 과거 baseline 경험 자동 import, 실제 task 14의 작성·재전송 실행기와 은행/입력 사전 검사, 인프라 재개·독립 의미 판정은 후속이다. 이 보완은 새 유료 실험을 실행하거나 미정 표본·금액을 확정하지 않는다.
