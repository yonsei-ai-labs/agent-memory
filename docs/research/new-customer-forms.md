# 새 고객의 예약 상태를 실제로 바꾸는 두 회차 실험

2026-11-18 · 사전 지정 · τ² airline 공식 test 분할 · 같은 과제 재수행

## 모집과 배정

공식 train의 30개 시나리오에서 고객을 식별하고 같은 고객이 등장하는 test 과제를 제외한다. 나머지에서 참조 업무 호출이 초기 DB를 실제로 바꾸고 필수 안내 문구가 없으며 공식 점수가 DB·COMMUNICATE인 모든 과제를 선택한다. 대상은 19·24·25·29·30·32번으로 서로 다른 고객 6명이다. 이는 이 저장소가 항공 train에서 평가한 고객을 기준으로 한 새 표본이다. 공개 벤치의 test 과제 중 이 여섯 개는 이 실험에서 사용한 것으로 기록한다. [원본 해시·선정 규칙·분석 기준](../../protocols/pilots/tau2-new-customer-forms-v1.json)

| 과제 | 분석자가 대조할 업무 |
| --- | --- |
| 19 | 적용 가능한 예약을 식별하고 취소 |
| 24·25 | 새 예약의 항공편·날짜, 승객, 수하물, 결제 수단과 금액 |
| 29 | 원래 예약의 취소와 새 예약 생성, 항공편·결제 |
| 30 | 기존 예약 항공편·날짜, 객실과 결제 수단 변경 |
| 32 | 같은 예약에 대한 순차 변경, 각 단계의 항공편·날짜와 결제 수단 |

1회차는 새 고객 세션과 초기 DB에서 기억 없이 한 번씩 푼다. 성공 여부를 보고 대상을 교체하거나 균형을 맞추지 않는다. 각 과제의 같은 공개 경험을 네 작성자에게 주고 성공·실패 결과와 채점 기준은 분석에만 쓴다. 작성 지침과 공통 문구는 이전 네 형식 실험의 버전을 사용한다. 각 형식과 반복에 독립된 작성 세션·은행을 배정한다. 먼저 작성 48건을 끝내고 새 2회차 72건을 실행한다. 각 반복의 여섯 조건은 단순 정보·일기·교훈·절차·빈 메모·무기억이다. 네 형식은 각각 작성한 본문 전체를 시작부터 제공한다. 빈 메모는 같은 안내와 빈 스냅샷으로 시작한다. 무기억은 은행·기억 안내가 없는 native 수행이다.

1회차 작업자 시드는 0, 두 번의 작성 시드는 0·1, 2회차 작업자와 native 시드는 2·3이다. 고객 시뮬레이터에는 시드를 보내지 않는다. 각 과제·반복 내 조건 순서는 고정 해시로 정한다. 빈 작성은 원래 조건에 남긴다. 1회차 미채점은 예정된 2회차를 미시작으로 보존하고 다른 고객으로 교체하지 않는다. 2회차 오류도 공식 실패와 별도로 기록한다.

## 판정과 비용

각 형식과 무기억·빈 메모를 같은 과제·반복에서 짝지어 개선, 악화, 양쪽 성공, 양쪽 실패, 결측을 센다. 1회차 실패에서 회복한 비율과 성공에서 다시 실패한 비율을 따로 표시한다. 형식끼리의 여섯 비교와 빈 메모 대 무기억은 보조 비교다. 표본 단위는 고객 6명이고 네 형식이 같은 대조 실행을 공유한다.

참조 변경 행동마다 같은 도구와 예약 ID 또는 예약 생성 고객 ID의 정상 실행을 순서대로 하나씩 대응한다. 비슷한 중간 호출, 실제 업무 상태와 점수 속성은 따로 보존한다. 행동이 누락되면 그 행동에 필요한 날짜·항공편·수량·금액 필드도 충족되지 않은 것으로 센다. 적용 대상이 없는 항목은 분모에서 제외한다. 32번에는 같은 예약의 같은 도구가 두 번 필요하므로 한 번의 관측을 두 단계에 쓰지 않는다. 주 판단은 native 공식 결과이며 확정된 업무를 재생해 최종 상태를 재검산한다.

OpenRouter를 사용하는 새 실행의 예약 포함 상한은 1회차 $0.30, 작성과 2회차 합계 $1.70이다. 원문과 요청 출처를 확인할 수 있는 HTTP 429·502·503·504는 단계별 최대 두 번까지 해당 요청의 보수적 최대액을 예약해 남은 한도에서 차감한다. 한번 제출한 작업은 다시 보내지 않는다. 그 밖의 미확인 청구는 해당 단계를 중단한다. 성적과 실패 사유, 실제 사용료와 예약액은 별도로 보관한다.

## 원본에서 다시 확인하기

다음 명령은 저장소 루트에서 실행한다. 첫 명령은 고객 선정과 원본 벤치 해시를 검사한다. 두 번째는 봉인된 1회차에서 공개 경험과 2회차 배정을 재구성한다. 뒤의 두 명령은 새 출력 폴더에 짝별 원문과 실제 입력, 업무 도구의 인수, 공식 재채점 및 비용 집계를 만든다. 네 명령 모두 오프라인에서 실행할 수 있다.

```bash
uv run python scripts/run_fresh_forms.py source --preflight
uv run python scripts/run_fresh_forms.py comparison --preflight
uv run python scripts/report_prompt_comparison.py runs/tau2-new-customer-forms-01 outputs/new-customer-report-copy
uv run python scripts/analyze_fresh_forms.py outputs/new-customer-report-copy outputs/new-customer-analysis-copy
```

원본에서 세 번째 HTTP 503으로 전송이 멈춘 뒤, 79∼120번 미시작 배정만 이어가는 별도 계획을 고정했다. 앞선 78개 작업의 원본 journal과 결과를 대조하고, 청구액 미확인 세 요청의 전송별 최대액을 기존 한도에서 예약한다. 새로 받은 대상 오류에는 제공자가 지정한 대기 시간과 연속 오류 횟수에 따른 대기를 적용한다. 합산본에는 원본 작성·수행이 이미 포함된다. [보충 계획](../../protocols/pilots/tau2-new-customer-forms-continuation-v1.json)

```bash
uv run python scripts/continue_fresh_forms.py --preflight
uv run python scripts/report_prompt_comparison.py runs/tau2-new-customer-forms-continuation-01 outputs/new-customer-combined-report-copy
uv run python scripts/analyze_fresh_forms.py outputs/new-customer-combined-report-copy outputs/new-customer-combined-analysis-copy --continuation
uv run python scripts/replay_fresh_booking_fields.py outputs/new-customer-booking-replay-copy.json --continuation
```
