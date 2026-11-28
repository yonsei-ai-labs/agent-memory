# 고정 결정 지점의 한 응답 재전송

2026-11-15 · 구현·모의 HTTP 검증 · 새 유료 실행 전

**저장된 Luna 요청을 그대로 복사해 응답 하나만 받는 진단 실행기를 추가했다.** [리뷰 반영](../reports/20261115-baseline-review-update.md)에 따라 과제 정합성 정리와 기억 비교 준비를 병행한다. 업무 도구는 실행하지 않으며 세션이나 전체 에피소드를 복원한 것으로 보고하지 않는다.

## 비교 범위

기존 [원인 진단](../../artifacts/analyses/20261115-tau2-baseline-failure-diagnosis.json)이 고정한 8개 지점을 사용한다: 17·1, 23·2, 14·2, 42·1, 42·2, 39·2, 21·1, 20·1. 역사적 실패를 보고 고른 개발 스냅샷이다. 일부 지점에는 앞선 잘못된 제안이나 사용자 승인이 이미 포함돼 있다. 최초 오류 직전 상태·무작위 과제 표본으로 해석하지 않는다.

| 조건 | 실제 요청의 변경 | 고정하는 것 |
| --- | --- | --- |
| `original` | 없음. 저장된 요청과 canonical bytes 일치 | 모델·제공 경로·모든 메시지·도구·원래 seed·출력 한도 |
| `reasoning_medium` | `reasoning.effort`만 medium | 나머지 요청 전체 |
| `memo_researcher` | 첫 system 본문 끝에 구분된 메모 추가 | 기존 메시지·도구·low 추론·원래 seed·출력 한도 |

기존 요청의 seed를 바꾸지 않는다. 같은 요청의 반복은 제공자 변동을 볼 수 있지만 독립 샘플을 보장하지 않는다. 배정 seed는 조건 순서를 섞는 용도이고 모델에 새 seed로 전달하지 않는다. case×반복을 블록으로 묶어 세 조건의 순서를 섞는다.

[현재 메모](../../protocols/diagnostics/decision-replay-researcher-memos-v1.json)는 **결과를 본 연구자가 정리한 정책·절차 메모**다. 잘못된 사례를 고른 사실과 저자의 결과 접근을 숨기지 않는다. 예약 ID·정답 금액·관측하지 않은 항공편 시각을 본문에 넣지 않았다. 작업자가 실패 피드백을 받고 직접 작성한 기억은 아직 준비하지 않았으며 이 조건과 합치지 않는다. 이 진단은 자율 저장·조회 은행의 효과가 아니라 메모를 추가하는 prompt 개입이다.

## 판정 범위

현재 선택 지점 중 도구 호출은 14·2, 23·2, 21·1의 3개이고 나머지 5개는 설명·계획 발화다. 39·2도 선택된 위치는 취소 호출 전의 계획 설명이라 사람 판정 대상으로 둔다.

- **14:** `book_reservation.total_baggages`가 0보다 큰지 검사한다. 필요하지 않은 수하물 추가에 대한 좁은 검사다.
- **23:** 한 `book_reservation`에 증서가 2장 이상인지 검사한다. 그 밖의 결제·승객·승인까지 옳다는 보증은 아니다.
- **21:** 제출한 항공편 순서에서 앞선 도착보다 다음 출발이 이른지 검사한다. 시각은 분석 전용 DB로 판정하고 모델 입력에 보충하지 않는다.
- **나머지 5개:** 공개 응답과 원래 입력을 보고 정책 근거·목표·관측의 적용을 사람이 판단한다. 모델의 비공개 추론은 검토 본문에서 제외한다.

자동 판정은 `violation_detected`, `argument_check_passed`, `no_target_write`, `unparseable_or_unknown_arguments`를 구분한다. 텍스트 지점은 `manual_review_required`다. 추가 조회·확인 질문·보류·도구를 호출하지 않은 응답을 자동 성공으로 세지 않는다. `argument_check_passed`도 완전한 업무 성공이 아니다. 조건별 표에는 전체 배정·응답·오류·생략과 대상 변경 호출의 유무를 함께 남긴다.

수동 판정 기준은 실제 호출 전에 이 문서와 함께 동결한다. 응답의 정책상 허용 여부·필요한 확인·알려진 오류·미확정을 나누고 근거 문구를 남긴다.

| 지점 | 알려진 오류의 기준 | 확인·추가 관측의 처리 |
| --- | --- | --- |
| 17·1 | basic economy라는 이유로 기존 항공편을 유지한 등급 변경까지 금지 | 비행 여부나 변경 세부 확인은 별도 확인 응답 |
| 42·1 | 타인 승객 예약을 자기 예약으로 취급하거나 일부 날짜만 확인하고 전체 충돌이 없다고 단정 | 전체 목표·승객·취소 이유·명시적 승인 확인을 구분 |
| 42·2 | 이미 관측한 BOS 5월22일 출발을 없다고 단정 | 원래 결과 재조회·내용 확인은 추가 관측으로 분리 |
| 39·2 | 24시간을 넘은 예약을 이내라고 주장하거나 그 근거로 부적합 취소를 제안. 미래 business 예약을 과거로 오인 | 적격 대상 재확인·확인 요청과 오적용을 구분 |
| 20·1 | 제공된 May15 기준에서 May20을 이미 지난 날짜라고 주장하거나 여행 연도를 임의 변경 | 과거라고 단정하지 않는 연도 확인은 별도 확인 응답 |

판정 시 조건 이름을 가린 공개 응답과 동일한 원래 문맥을 사용하고, 실제 가림 여부·검토자·근거를 남긴다. 짧은 확인 응답을 직접 올바른 변경을 수행한 응답과 합쳐 개선율로 만들지 않는다.

## 실행·감사

[실행 초안](../../protocols/drafts/tau2-decision-replay-v1.json)은 8지점×20회×3조건=480요청을 **제안**한다. 배정 seed·전체 요청/시간/비용 한도는 null이고 status는 draft다. 이전 실험의 $1 승인을 전용하지 않는다. 아래 명령은 catalog·모델 호출 없이 동작하며 실행 폴더를 만들지 않는다.

```bash
uv run --frozen --extra tau2 python scripts/run_decision_replay.py
uv run --frozen --extra tau2 python scripts/run_decision_replay.py --preflight
```

실제 실행은 별도 ready 프로필·배정·구현을 고정한 뒤 새/빈 출력 폴더에서만 허용한다. 제안된 $0.1은 실제 비용 상한이나 보장이 아니다. medium·메모의 출력/추론·캐시 사용량과 비용을 원응답에서 남긴다.

```bash
uv run --frozen --extra tau2 python scripts/run_decision_replay.py --config protocols/pilots/NEW_READY_PROFILE.json --live --output runs/NEW_DECISION_REPLAY
uv run --frozen --extra tau2 python scripts/run_decision_replay.py --audit-only --output runs/NEW_DECISION_REPLAY
```

[새 실행기](../../src/agent_memory_eval/runner/decision_replay.py)는 원래 요청의 해시·연결된 진단·메모 출처·전체 배정과 실제 요청/응답을 v1.1 journal에 보존한다. 요청 출처 manifest를 구성하고, 감사 시 원본 조건의 동일성과 나머지 두 조건의 변경 범위를 다시 확인한다. 저장된 원본 요청과 분석용 DB를 실행 기록 안에 복사하므로 완료 기록의 감사에는 원래 실행 폴더·τ² checkout·API 키가 필요 없다.

한 run은 worker 요청 한 번이다. DB와 사용자 시뮬레이터는 실행하지 않는다. `outcome.scored`의 공식 결과는 `not_available`·null로 유지하고 별도 `business.transition`에 응답 검사 결과를 저장한다. 메모는 승인된 prompt template의 개입으로 기록하고 가짜 기억 조회·반환·은행 이력을 만들지 않는다. 실행 기록에는 이 조건의 메모와 실제 전송 요청이 함께 남는다.

전체 요청·시간·비용 guard를 적용한다. 서비스 오류나 비용 미확인이 생기면 남은 배정을 이유와 함께 생략하며 자동 재시도하지 않는다. 마지막 응답까지 받았더라도 비용이 미확인이면 `partial`로 보고한다. provider 오류를 고쳤거나 서비스 가용성을 보장한다고 표현하지 않는다.

## 검증과 해석

```bash
uv run --frozen --extra tau2 python -m unittest discover -s tests -p 'test_decision_replay.py'
```

[행동 테스트](https://github.com/yonsei-ai-labs/agent-memory/blob/40f881aa1f70f5772f52484a4f1a19d0de01162d/tests/test_decision_replay.py)는 실제 원본 입력과 mock HTTP를 사용한다. 요청 변경 범위·배정·사람/자동 판정 분리·감사 재계산·원본 보존·전체 호출/비용 제한·503 비용 미확인 중단을 검증한다. 모델 효과나 실제 비용 결과는 아직 없다.

새 16개를 포함한 전체 148개 테스트와 기본 설계·문서·보관본 검사가 통과했다. 기존 실행 전 동결 참조 85개와 이전 전달 비교 참조 64개도 유지됐다.

같은 스냅샷을 새로 반복한 `original` 조건이 비교 기준선이다. 선정에 사용한 역사적 오류 1개와 개입의 새 응답들을 직접 비교하지 않는다. 메모 효과가 보여도 새 출처 전이·자율 조회·전체 에피소드 성공으로 일반화하지 않는다. 전체 연구 범위와 이후 자연 실행 비교는 유지한다.
