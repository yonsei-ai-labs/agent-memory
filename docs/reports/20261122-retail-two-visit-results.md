# 새 소매 고객 77명에서 지난 주문의 기억이 바꾼 행동

고객이 주문번호 없이 “지난번에 이야기한 주문”을 가리키면, 그때 쓴 메모를 시작할 때 받은 상담원은 154건 중 129건에서 첫 행동으로 목표 주문 하나를 바로 조회했다. 같은 화면에 빈 메모를 받은 상담원은 27건이었다. 고객별 평균 차이는 **+66.2%p**, 77명 중 70명 개선·7명 동률이었다. 끝까지 대화한 뒤에는 두 조건 모두 목표 주문을 취소했다. 고객이 어느 주문인지 다시 알려 줄 수 있는 과제에서 기억의 가장 큰 이득은 확인 질문과 잘못된 취소 제안을 줄인 것이었다. [사전 설계](../research/retail-two-visit-v1.md) · [개별 판단과 상담 결과](../../artifacts/analyses/20261122-retail-two-visit-results-with-recovery.json)

## 같은 고객의 지난 주문을 바꾼 비교

공식 소매 과제에 쓰인 고객을 제외한 뒤 취소 가능한 주문이 둘 이상 있는 고객 77명을 선정했다. 고객마다 첫 방문 이력을 두 개 만들었다. 한 이력에서는 A 주문, 다른 이력에서는 B 주문에 든 상품을 문의했다. 재방문 때 “지난번 주문”을 가리키는 발화와 본인확인을 마친 프로필 화면은 두 이력에서 같았다. 목표 주문이 어느 쪽인지는 첫 방문에서 결정된다. 현재 주문번호를 명시하는 두 변형도 함께 실행했다. 하나는 지난번과 같은 주문, 다른 하나는 다른 주문을 지정했다. [과제와 배정](../../artifacts/analyses/20261122-retail-order-pairs.json) · [등록한 프로토콜](../../protocols/pilots/tau2-retail-two-visit-v1.json)

한 이력에서 사실·일기·교훈·절차 메모를 독립적으로 작성하고 같은 이력의 작성물을 한데 모았다. 각 재방문 사례에는 다섯 조건을 모두 배정했다. 자동 제공은 상담 시작 때 메모 묶음의 본문을 보여 준다. 빈 자동 제공은 같은 안내와 빈 묶음을 보여 준다. 자율 조회와 빈 자율 조회에는 같은 기억 도구가 열리고, 상담원이 직접 요청하면 각각 실제 묶음과 빈 묶음에서 응답한다. 무노트는 기억 안내와 도구 없이 같은 공개 상태에서 시작한다. 주 비교의 상대는 빈 자동 제공이다. [공개 상태와 조건별 입력 검산](../../artifacts/analyses/20261122-retail-two-visit-final-audit.json)

## 지난 주문을 지칭한 상담

| 전달 조건 | 첫 행동으로 목표 주문만 조회 | 어느 주문인지 되물은 상담 | 틀린 주문 취소를 제안한 상담 | 대상·사유를 설명하고 승인받아 목표 주문 취소 | 최종 DB 성공 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 메모 자동 제공 | 129/154 | 24/153 | 5/153 | 153/153 | 153/153 |
| 빈 자동 제공 | 27/154 | 129/154 | 26/154 | 149/154 | 154/154 |
| 메모 자율 조회 | 33/154 | 136/154 | 23/154 | 150/154 | 154/154 |
| 빈 자율 조회 | 29/154 | 133/153 | 24/153 | 151/153 | 153/153 |
| 무노트 | 16/154 | 141/154 | 15/154 | 152/154 | 154/154 |

첫 판단은 이 유형의 770건 모두에서 관측했다. 전체 상담 열의 분모는 마지막까지 마친 768건이다. 대상 주문을 확인하는 질문과 취소해도 되는지 묻는 승인은 따로 셌다. 잘못된 주문을 제안한 횟수와 고객에게 고친 제안을 다시 보여 준 횟수는 다섯 조건 모두 같았다. 실제로 다른 주문을 취소한 사례는 없었다. [판독 기준과 계산](../../scripts/analyze_retail_two_visit.py) · [본 실행 판독](../../artifacts/analyses/20261122-retail-two-visit-original-reviews.json) · [복구 판독](../../artifacts/analyses/20261122-retail-two-visit-recovery-reviews.json)

같은 사례의 두 조건이 모두 끝난 **153쌍**에서, 자동 제공과 빈 자동 제공의 DB 성공은 각각 153건이었다. 어느 주문인지 되물은 상담은 **24건 대 128건**으로 고객별 평균 차이 −68.2%p, 틀린 주문을 취소하자고 제안한 상담은 **5건 대 26건**으로 −13.6%p였다. 대상·사유를 설명하고 고객의 승인을 받아 취소한 상담은 **153건 대 148건**으로 +3.2%p였다. 이 마지막 차이는 5명 개선·72명 동률이고 양측 정확 부호검정은 p=0.0625였다. 상담 과정의 개선에 비해 승인까지 갖춘 최종 성공의 개선 폭은 작고 불확실하다. [고객별 짝비교와 결측 범위](../../artifacts/analyses/20261122-retail-two-visit-results-with-recovery.json)

전체 2,305건 중 목표 DB에 도달한 상담은 2,304건, 정해 둔 승인 절차까지 충족한 상담은 2,291건이었다. 두 수치가 갈린 13건 중 9건은 복수 후보에서 대상을 골랐지만 취소할 특정 주문을 제안해 승인을 받는 단계가 빠졌다. 3건은 마지막 제안에 취소 사유가 빠졌고, 1건은 고객 답변이 주문 식별과 취소 승인 중 어느 쪽인지 불명확했다. 실제로 한 고객은 두 주문 중 하나의 번호만 알려 줬고, 상담원이 바로 취소하자 고객이 먼저 확인하고 싶었다고 지적했다. 적절한 대상과 승인 절차를 각각 판독한 이유다. [대화 근거와 판독 결과](../../artifacts/analyses/20261122-retail-two-visit-original-reviews.json)

## 주문의 위치와 현재 정보

| 지난 주문의 목록 위치 | 메모 자동 제공 | 빈 자동 제공 | 메모 자율 조회 | 빈 자율 조회 | 무노트 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 맨 앞 | 50/54 | 27/54 | 33/54 | 29/54 | 16/54 |
| 두 번째 이후 | 79/100 | 0/100 | 0/100 | 0/100 | 0/100 |

위치별 수치는 첫 행동으로 목표 주문 하나만 조회한 횟수다. 앞선 7명 파일럿에서 결과를 본 뒤 발견한 위치 차이를 이번 77명에서는 실행 전에 지정했다. 뒤쪽 주문에서의 이득이 더 컸고 맨 앞 주문에서도 이득이 있었다. [이전 파일럿의 위치별 검산](20261122-retail-order-choice-replay.md) · [77명의 사전 배정](../../artifacts/analyses/20261122-retail-two-visit-preflight.json)

고객마다 같은 비중을 준 자동 제공 효과는 첫 위치에서 +42.6%p, 뒤쪽에서 +79.9%p였다. 두 효과의 차이는 +37.3%p였다. 고객을 단위로 10,000번 재표집한 백분위 구간은 위치 차이에서 +20.0∼+54.9%p, 주 비교의 +66.2%p에서 +59.1∼+73.4%p였다. 주 비교의 양측 정확 부호검정은 p=1.69×10⁻²¹이다. 위치별 분모에서 한 고객이 기여하는 주문 수가 달라 단순 비율과 고객별 평균이 조금 다르다. [위치별 원자료와 짝비교](../../artifacts/analyses/20261122-retail-two-visit-results-with-recovery.json)

왜 첫 행동이 달라졌는지 보려고 사후에 메모 제목이나 본문에 목표 주문번호가 있는지 확인했다. 정확한 목표 주문번호가 들어 있는 122개 묶음에서는 바로 조회가 자동 제공 119건 대 빈 자동 제공 18건이었다. 내용은 있지만 그 번호가 없는 30개 묶음에서는 10건 대 9건, 빈 묶음 2개에서는 양쪽 모두 0건이었다. **전체 순증 102건 중 101건이 목표 번호를 담은 묶음에서 발생했다.** 번호의 유무는 작성자가 결정했으므로 다른 메모 내용도 함께 달라졌을 수 있다. [메모 내용별 집계](../../artifacts/analyses/20261122-retail-two-visit-results-with-recovery.json)

자율 조회 도구를 제공한 첫 판단 924건과 그중 마지막까지 끝난 923건에서 메모 목록·본문 요청은 모두 0건이었다. 지난 주문 유형의 자율 조회와 빈 자율 조회 사이 첫 행동 차이는 +2.6%p였고, 고객별로 13명 개선·55명 동률·9명 악화였다(양측 부호검정 p=0.523). 상담원이 실제로 꺼내 본 메모는 0개였다. [조건별 기억 요청과 실제 노출](../../artifacts/analyses/20261122-retail-two-visit-results-with-recovery.json)

고객이 **오늘 주문번호를 직접 말한** 1,540건에서는 다섯 조건 모두 그 주문 하나부터 조회했다. 지난번과 다른 주문을 지정한 770건 중 정상 종료 769건은 모두 현재 주문을 승인받아 취소했다. 지난번과 같은 주문을 지정한 유형에서는 768건이 정상 종료했고 767건이 목표 DB에 도달했다. 나머지 1건은 빈 자동 제공 조건에서 고객이 조회된 상품을 자신의 주문으로 인정하지 않아 상담원이 고객 요청대로 사람에게 이관했다. [현재 번호가 있는 두 대조 유형](../../artifacts/analyses/20261122-retail-two-visit-results-with-recovery.json)

## 원천 장애와 복구

| 단계 | 본 실행 | 별도 복구 | 합산 |
| --- | --- | --- | --- |
| 첫 방문 | 완료 143, 공급자 오류 11 | 11개 이력을 모두 완료 | 서로 다른 이력 154개, 모두 원천 과제 점수 1 |
| 메모 작성 | 저장 347, 자발적 빈 작성 225, 원천 결측 44 | 저장 24, 자발적 빈 작성 20 | 616개 중 저장 371, 자발적 빈 작성 245 |
| 재방문 첫 판단 | 응답 2,244, 미전송 66 | 미전송 배정 66개 모두 응답 | 2,310개 전부 관측 |
| 끝까지 이어간 재방문 | 정상 종료 2,239, 통신 중단 5 | 정상 종료 66 | 정상 종료 2,305, 통신 중단 5 |

첫 재방문 모델 요청 전에 503 오류 영수증 11개를 봉인했다. 본 실행이 끝난 뒤 별도 실행에서 이 원천과 작성물을 복구하고, 원래 전송 기록이 없는 66개 배정에만 처음으로 모델 요청을 보냈다. 같은 고객·과제에 이미 있는 대조 기록을 두 분석에서 공유한다. 본 실행 규칙대로 집계하면 자동 제공 119/143 대 빈 자동 제공 26/143, 고객별 차이 +64.3%p다. 복구 포함 154쌍에서 +66.2%p다. 원래 관측한 2,244개 배정의 첫 판단과 상담 행을 두 결과 파일에서 대조했다. [본 실행 전체 결과](../../artifacts/analyses/20261122-retail-two-visit-results.json) · [복구 전 첫 판단 보고](20261122-retail-two-visit-first-decisions.md) · [복구 대상과 승인 범위](../../protocols/pilots/tau2-retail-two-visit-recovery-v1.json)

상담 중단 5건은 모두 고객 시뮬레이터 요청에서 발생했다. 4건은 HTTP 503, 1건은 전송 결과 불명이었다. 요청 당시까지 실행된 업무 도구를 새 DB에 재생했다. 한 건은 상담원이 목표 주문과 사유를 설명하고 고객에게 명시적 승인을 받은 뒤 취소까지 마쳤다. 취소 응답과 목표 DB 상태가 모두 남아 있었다. 나머지 네 건 중 세 건은 주문 조회까지, 한 건은 본인확인 뒤 업무 도구를 추가로 실행하기 전에 끊겼다. 다섯 건 모두 정상 종료 점수는 결측으로 보존했다. [중단 시점의 도구 영수증과 DB 검산](../../artifacts/analyses/20261122-retail-two-visit-final-audit.json)

## 판정과 비용

상담원은 OpenRouter의 `openai/gpt-6-luna`(OpenAI), 고객 시뮬레이터는 `deepseek/deepseek-v4.1-flash-1017`(Fireworks)을 사용했다. 본 실행 구현은 `007d4c4`, 복구 실행은 `c886508`에 고정했다. 복구 실행은 같은 모델과 단가, 동일한 공개 대화와 전달 규칙을 사용했다. 원천 상담 154개에는 기억이 없었고 작성자에게 전달된 것은 그 상담의 공개 경험과 피드백 없음 안내였다. 재방문 2,310개는 서로 다른 새 세션이었다. 각 첫 요청의 원문을 재구성했고, 정상 종료 2,305건의 업무 도구 동작을 새 소매 DB에 적용해 기록된 점수를 모두 재현했다. [세션·요청·DB 검산](../../artifacts/analyses/20261122-retail-two-visit-final-audit.json)

상담 조건·과거 메모·채점 목표를 가린 뒤 공개 발화와 도구 기록으로 승인 여부를 판독했다. 에이전트가 343건의 공개 대화를 직접 읽고, 나머지 1,962건에서는 주문 제안·응답·실제 취소에 연결된 공개 문구를 대조했다. 별도 사람 판독은 0건이다. 미리 해시로 선택한 100건 전체를 직접 읽은 결과, 판독 보조 규칙이 제시한 92건 중 2건에서 주문 식별 질문을 추가했다. 이 표본에서 제안이나 승인 응답 판정이 달라진 건수는 0이었다. 본 실행 일부와 복구 상담은 가명으로 섞어 판독했다. [판독 규칙](../../scripts/code_retail_two_visit_turns.py) · [본 실행의 봉인된 판독](../../artifacts/analyses/20261122-retail-two-visit-original-reviews.json) · [복구 판독](../../artifacts/analyses/20261122-retail-two-visit-recovery-reviews.json)

모든 재방문 첫 원응답 2,310개에서 도구명·주문번호를 읽어 목표 주문 단독 조회 여부를 다시 셌다. 완료 상담 2,305건도 판독 근거와 주문 변경 기록을 시간순으로 따라가며 개별 결과를 다시 판정했다. 이 결과와 15개 요청 유형·전달 조건 조합의 135개 집계값은 모두 결과 파일과 일치했다. 본 실행과 복구 포함 분석은 각각 두 번 생성해 파일 바이트가 같은지 확인했다. 실험 고객 77명은 이 소매 DB에서 정한 조건을 충족하는 고객이며, 재방문 과제는 연구용으로 구성했다. 결론의 적용 범위는 이 상담원·시뮬레이터·주문 취소 과제다. [두 실행의 검산기](../../scripts/verify_retail_two_visit_results.py) · [첫 원응답과 전체 상담의 독립 대조](../../artifacts/analyses/20261122-retail-two-visit-outcome-check.json)

| 비용 | 본 실행 | 복구 | 합계 |
| --- | ---: | ---: | ---: |
| 모델에 전송한 요청 | 17,073 | 576 | 17,649 |
| 비용이 확인된 요청 | 17,057 | 576 | 17,633 |
| 확인 비용 | $2.758971972 | $0.065650620 | $2.824622592 |
| 미확정 청구 최대액 | $0.030112500 | $0 | $0.030112500 |

미확정 요청 16건에는 공급자 응답 ID와 사용량 영수증이 남지 않았다. 요청 원문과 등록 단가로 잡은 최대액을 포함해도 총액은 **$2.854735092**로 승인 상한 $13 이내다. [단계별 비용과 영수증 검산](../../artifacts/analyses/20261122-retail-two-visit-final-audit.json)

### 오프라인 재현

개별 배정 결과·판독·검산 요약은 Git에 있다. 원본 요청과 공급자 영수증에서 아래 검산까지 재현하려면 로컬 `runs/` 두 실행과 고정된 소매 벤치 설치가 필요하다. 공개한 판독 대응표를 사용하므로 비공개 가명 키나 새 유료 요청은 필요하지 않다.

```bash
visit_check=$(mktemp -d)
visit_art=artifacts/analyses/20261122-retail-two-visit
visit_run=runs/tau2-retail-two-visit-01
uv run --frozen --no-sync python -m scripts.prepare_retail_order_pairs \
  --output "$visit_check/cohort.json" --materialize "$visit_check/pairs"
uv run --frozen --no-sync python -m scripts.analyze_retail_two_visit \
  --run "$visit_run" --review-index "$visit_art-original-reviews.json" \
  --reviews "$visit_art-original-reviews.json" --output "$visit_check/original.json"
uv run --frozen --no-sync python -m scripts.analyze_retail_two_visit \
  --run "$visit_run" --review-index "$visit_art-original-reviews.json" \
  --reviews "$visit_art-original-reviews.json" --recovery "$visit_run-recovery" \
  --recovery-reviews "$visit_art-recovery-reviews.json" --output "$visit_check/augmented.json"
uv run --frozen --no-sync python -m scripts.verify_retail_two_visit_results \
  --run "$visit_run" --recovery "$visit_run-recovery" \
  --public "$visit_check/pairs/public-prefixes.json" --original-results "$visit_check/original.json" \
  --augmented-results "$visit_check/augmented.json" --output "$visit_check/audit.json"
uv run --frozen --no-sync python -m scripts.check_retail_two_visit_outcomes \
  --run "$visit_run" --recovery "$visit_run-recovery" --original-reviews "$visit_art-original-reviews.json" \
  --recovery-reviews "$visit_art-recovery-reviews.json" --results "$visit_check/augmented.json" \
  --output "$visit_check/recount.json"
cmp "$visit_check/recount.json" "$visit_art-outcome-check.json"
cmp "$visit_check/original.json" "$visit_art-results.json"
cmp "$visit_check/augmented.json" "$visit_art-results-with-recovery.json"
cmp "$visit_check/audit.json" "$visit_art-final-audit.json"
```
