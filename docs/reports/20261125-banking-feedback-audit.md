# 다음 은행 실험에 대한 독립 검토를 원기록으로 대조한 결과

2026-11-25 · 대상은 기존 은행·소매 실행과 현재 로컬 Git 이력이다. 새 실험 요청은 0건이다.

**다음 은행 비교는 기억이 실제로 쓰일 수 있는 판단을 먼저 고르고 공용 채점기로 검증하는 편이 타당하다.** 같은 첫 입력에서도 상담원의 첫 검색과 최종 성패가 달라졌다. 메모를 본 뒤 최종 성공했다는 관측 하나만으로는 어느 행동을 메모가 바꿨는지 설명하기 어렵다. [프레임워크 점검과 다음 작업](20261125-framework-integrity-review.md)

## 1. 같은 입력의 변동과 정확 부호검정

성패 제공 전후의 자율 조회를 과제·형식별로 짝지었다. 20/20쌍에서 첫 상담원 요청이 바이트 단위로 같았다. 40개 상담 전부에서 목록·본문 조회와 메모 노출이 0건이었다. 양쪽의 첫 행동은 모두 문서 검색이었고, JSON 인수를 해석한 뒤에도 20/20쌍에서 검색 인수가 달랐다. 최종 성패는 5/20쌍에서 갈렸다. 같은 업무 입력과 실질적인 미노출 조건에서 다른 시점에 관측한 A/A 비교다. 두 상담의 후속 대화는 독립적으로 생성됐다. [성패 제공 결과](20261125-banking-result-feedback.md)

| 짝비교 | 한쪽만 성공 / 상대만 성공 | 양측 정확 부호검정 |
| --- | ---: | ---: |
| 성패 제공 자율 조회 / 기존 자율 조회, 양쪽 미노출 | 2 / 3 | 1.000 |
| 후보 작성 / 기존 작성, 전체 상담 | 1 / 4 | 0.375 |
| 성패 제공 / 미제공, 자동 제공 전체 상담 | 1 / 3 | 0.625 |
| 배경 삭제 / 행동 문장 삭제, 고정 판단 | 9 / 1 | 0.0215 |

표의 검정은 동률을 제외한 **배정 쌍이 독립**이라는 단순 계산이다. 같은 원천의 다섯 형식이나 같은 판단의 반복이 함께 움직일 수 있다. 실제로 고정 판단의 순증가는 008번 +5, 015번 +3이고 두 원천의 방향만 세면 양측 p=0.50이다. 전체 상담에서 관측한 기존 7/20·후보 4/20은 보존하고, 두 작성법의 일반적인 우열은 미확정으로 둔다. 5/20은 네 과제에서 관측한 불일치 비율이다. 이 숫자로 다른 과제나 고정 판단의 효과가 잡음 안에 든다는 통계적 판정을 내릴 수 없다. 공통 결과표에 고유 원천 수, 결측과 불일치, 사용할 수 있는 같은 조건의 대조 범위, 검정의 단위를 적는다. [전체 상담 분모](../../artifacts/analyses/20261125-banking-policy-cycle-validation.json) · [고정 판단별 득실](../../artifacts/analyses/20261125-banking-policy-cycle-development.json)

## 2–4. 판단 선정, 036, 자율 조회

- **기억이 쓰일 여지.** 8개 판단의 보관된 무기억 첫 응답은 8/8이었고 그중 7곳은 양쪽 작성법이 각각 20/20이었다. 반대로 016·017은 과제별로 두 작성법이 각각 0/5였고, 016의 원천에서 필요한 상품 문서가 반환된 횟수는 0이었다. 예비 선별표에 판단 직전 대화·도구 결과에 있던 정보, 원천 공개 기록에 있던 정보, 메모가 보존한 정보와 인용 위치를 둔다. 메모의 새 단서가 있는 경우와 현재 정보만으로 풀리는 경우를 모두 남긴다. 공개 원천에서 필요한 사실을 골라 만든 연구자 메모는 별도 양성 대조로 쓸 수 있다. 과제 정답·숨은 DB는 작성 입력에서 제외한다. 양성 대조가 작동한 개발 지점을 그대로 독립 확인 표본으로 세면 효과를 과대평가할 수 있다. 같은 무기억 고정 판단의 추가 반복은 비용과 별도 승인이 필요한 선택지다. 네 번 모두 맞혀도 실제 성공 확률이 100%라고 단정할 수 없다. [무기억 원응답과 320개 판단](20261125-banking-scoped-writer-results.md) · [016 원천 진단](20261125-banking-policy-cycle-diagnosis.md)
- **036 재실행.** 기존 등록은 배정당 최대 한 번 전송을 규정한다. 원천은 고객 429에서 중단됐고 실제 궤적은 카드 주문 판단까지 도달하지 않았다. 원본을 결측으로 보존한다. 별도로 등록한다면 유효한 무기억 원천에서 주문 전 판단을 확보하고 필요한 카드·배송지·사유·방식의 공개 근거부터 확인한다. 단일 원천을 공유하는 열 상담은 실행·DB 부작용 점검에 쓸 수 있다. 429·503에 대한 대기 시간과 횟수, 전송 여부 미확정 시 보류액과 중복 방지, 지출 한도를 사전에 정한다. 120·600·1,800초는 과거 복구의 예시다. 원래 결정한 새 2회차 무기억 상담 0건을 유지한다. [036 원본 등록](../../protocols/pilots/tau-banking-replacement-v1.json) · [복원한 실제 중단 지점](../research/banking-replacement-case.md)
- **자율 조회.** 선택 작성 0/19, 필수 작성 0/20, 성패 제공 0/20이라는 은행 관측이 맞다. 자율 조회는 조회 기회를 준 정책의 결과다. 같은 안내와 첫 입력, 실제 조회·노출 0이 모두 확인되는 짝에서 미노출 A/A를 계산할 수 있다. 소매의 조건부 안내는 실제 메모의 첫 응답 조회를 0/154에서 136/154로 높였다. 은행에서도 같은 효과가 난다는 증거는 아직 없다. 같은 저장본·업무 과제에서 중립 안내와 조건부 안내를 비교할 수 있고, 상담원에게 줄 안내에는 업무 정답을 넣지 않는다. 조회율과 첫 업무 행동, 상담 성패를 각각 채점한다. [선택 작성](20261124-banking-storage-review.md) · [필수 작성](20261124-banking-required-results.md) · [소매 조건부 안내](20261123-retail-conditional-retrieval.md)

## 5. JSON으로 감싼 답변이 고객에게 도달했는가

해시가 일치하는 모델 응답과 **바로 다음 고객 요청**을 다시 읽었다. 분모는 도구 호출 없이 비어 있지 않은 텍스트를 낸 상담원 응답이다. 마지막 고객 입력 메시지의 역할과 문자열이 정확히 일치하고 고객 모델 응답도 확인돼야 전달로 센다. 다음 고객 응답의 내용이 포장 형식 때문에 달라졌는지는 이 수치로 알 수 없다. [공통 집계 코드](../../src/agent_memory_eval/analysis/response_format.py)

| 단계 | JSON 객체 / 텍스트 응답 | 바로 다음 고객 입력에서 일치 |
| --- | ---: | ---: |
| 1회차 001·008·010·015 | 6/21 (28.6%) | 6/6 |
| 1회차 002·014·016·017 | 2/16 (12.5%) | 2/2 |
| 필수 작성, 자동 제공 | 88/116 (75.9%) | 88/88 |
| 필수 작성, 자율 조회 | 70/136 (51.5%) | 70/70 |
| 성패 제공, 자동 제공 | 52/109 (47.7%) | 52/52 |
| 성패 제공, 자율 조회 | 54/122 (44.3%) | 54/54 |
| 정책 사이클, 자동 제공 | 40/141 (28.4%) | 40/40 |

661개 텍스트 응답 중 JSON 객체는 312개였고 모두 그대로 전달됐다. 311개가 `message` 한 필드였고 정책 사이클의 1개는 `answer`였다. 본문을 한 번도 열지 않은 자율 조회에서도 포장 비율이 높았다. 각 비율은 상담 길이의 영향을 받는 발화 단위다. 메모 내용, 도구·안내, 실행 시점 중 어느 요인이 형식을 바꿨는지는 미확정이다. 같은 길이 대조를 시험한다면 토큰 수·제목·표현 형식·전달 방식을 맞추고, 조건 무관 내용 자체의 영향과 비용도 등록한다. 기존 고객에게 전달된 문구는 그대로 보존한다.

## 6–8. 독립 판독, 공용화, Git 보존

- **가림.** 은행 메모 판독은 Codex 한 명이 수행했다. 기존 판단의 가린 입력과 해독 가능한 근거 파일은 같은 커밋 `9d49b05`에 있다. 은행 후보 메모 20/20개에는 `Consequential decision`이라는 문구가 있었고 기존에는 0/20개였다. 키 접근이 가능하다는 사실로 판독자가 실제로 키를 봤다는 결론은 낼 수 없다. 다음 독립 판독에는 키가 없는 묶음만 전달하고 판정·인용을 먼저 봉인한다. 조건 추측과 정답률, 사람·모델 간 일치도, 보류율을 함께 남긴다. [기존 판독자와 범위](20261125-banking-result-feedback.md) · [가림 입력](../../artifacts/analyses/20261124-decision-attribution/blinded.json) · [근거 파일](../../artifacts/analyses/20261124-decision-attribution/evidence.json)
- **공용화.** `6025700..ecc8ef0`에서 은행 스크립트 10개가 생겼다. 각 커밋의 추가 줄 합계가 `scripts/` 3,412줄과 `src/` 614줄이고, 두 끝점의 diff에서 추가된 줄은 각각 3,393줄과 611줄이다. 다른 은행 실행기를 import하는 경로와 공용 `runner.fixed_decision`을 쓰는 경로가 함께 있다. 공통 짝비교·가림 판독·업무 행동 측정은 기존 구현을 확장하고 77명 재방문 같은 과거 결과로 검증한다. 업무별 목표·비대상은 어댑터가 선언하며 공식 검색기와 채점기의 버전을 유지한다. 전체 상담은 작게는 연결 검증에, 독립 사례가 확보되면 최종 성패·효율의 주 평가에 쓸 수 있다. [공용화 계획](../development/framework-scoring-plan.md) · [기존 공통 짝비교](../../src/agent_memory_eval/analysis/paired_policy.py) · [은행 실행기 연결](../../scripts/run_banking_scoped_decisions.py)
- **Git 보존.** 이번 감사에서 만든 파일을 제외하면 점검 시작 시점의 미추적 파일 중 36개 경로가 로컬 모든 Git 참조의 이력에도 없었다. `main`과 로컬 `origin/main`은 `6025700`, 036의 최종 커밋 `ecc8ef0`이 속한 로컬 브랜치는 `experiment/banking-replacement-20261125` 하나였다. 앞선 성패 제공·정책 사이클 커밋은 해당 실험 브랜치에도 있다. 후속 점검에서도 파일 생성일이 11월 23일인 19개와 24일인 17개가 미추적이며 로컬 Git 참조에 이력이 없었다. 혼재한 작업물은 보관할 파일과 소속을 확인해 main 기준으로 나누어 커밋할 대상이다.

## 같은 판단을 반복하는 설계의 검산

필수 작성·성패 제공·정책 사이클의 은행 2회차 상담을 각각 40건씩 확인했다. 120건 모두 1회차의 첫 고객 발화, 과제·초기 DB·채점 설정, 고객 모델의 안내·도구·설정이 일치했다. 1회차의 첫 상담원 요청에 등록된 메모 안내와 배정된 본문 또는 조회 도구만 더하자 실제 전송 요청이 120/120건에서 일치했다. 첫 응답에 기록된 모델 버전과 라우터 지역도 같았다. 이는 상담 시작의 격리를 확인한다. 이후의 고객 입력에는 앞서 생성한 답변과 업무 도구 결과가 들어간다. 세 실험에서 사용한 고유 원천 상담은 총 8건이며, 필수 작성과 성패 제공은 같은 4건을 공유했다. [원천 식별자·실험별 검산](../../artifacts/analyses/20261125-banking-fixed-decision-audit.json)

120건 중 119건은 1회차와 첫 응답의 문구 또는 도구 인수가 달랐다. 첫 행동의 **종류**가 문서 검색에서 고객에게 답하기로 바뀌거나 그 반대로 바뀐 것은 12건이었다. 같은 검색을 선택해도 인수에 따라 도달하는 문서가 달라질 수 있다. 아래는 별도 고정 판단 실험에서 완전히 같은 요청을 네 번씩 보낸 결과다. 도구 호출 ID를 제외한 문구·함수·인수와, 각 판단에서 사전에 정한 목표 행동 달성 여부를 따로 세었다. [문장 삭제 결과](../../artifacts/analyses/20261125-banking-policy-cycle-development.json) · [적용 조건 작성 결과](../../artifacts/analyses/20261125-scoped-decision-results.json)

| 네 번 반복한 입력 | 문구·함수·인수가 한 번이라도 달라진 묶음 | 첫 행동 종류가 달라진 묶음 | 목표 행동 달성 여부가 달라진 묶음 |
| --- | ---: | ---: | ---: |
| 은행 메모 문장 삭제, 96응답 | 20/24 | 10/24 | 9/24 |
| 은행 메모 적용 조건 작성, 320응답 | 70/80 | 18/80 | 5/80 |

적용 조건 작성에서 응답 표현은 87.5%의 입력에서 달라졌고, 목표 행동 달성 여부가 갈린 비율은 6.25%였다. τ² 새 계열의 기존 84%·85% 집계에는 보충 폴더에 복사된 원천 원장 2개와 답변이 없는 공급자 오류 49건이 포함됐다. 복사본을 한 번만 세고 정상 응답만 취하면 응답이 갈린 입력은 상담원 90/105, 고객 111/126이다. 같은 응답이 나온 쌍은 각각 30/349와 1,437/5,215였다. 소매 조건부 조회의 97/462묶음과 동일 응답 2,364/2,769쌍도 확인했다. 입력의 반복 횟수가 다르면 묶음 비율과 쌍 비율은 가중치가 다르다. 성과의 변동은 같은 과제·채점 기준에서 목표 행동과 최종 성공을 각각 다시 세어야 한다. 과거 무기억 소매 상담에서 세 번 모두 채점된 57과제 중 20과제는 최종 성패가 섞였다. [집계와 응답 정의](../../artifacts/analyses/20261125-banking-fixed-decision-audit.json)

**소매 129 대 27의 범위.** 고객이 지난 주문을 지칭하는 조건에서 77명에게 과거 주문 두 건씩을 연결했다. 실제 메모를 시작에 제공한 경우 목표 주문부터 연 횟수는 129/154, 같은 방식의 빈 메모는 27/154였다. 각 사례·조건에는 첫 판단이 한 번씩 있었다. 각 조건에서 끝까지 채점된 상담의 취소 성공은 153/153과 154/154였다. 이 사례는 기억이 첫 조회를 바꿀 수 있다는 근거다. 메모를 자율 조회하도록 조건부로 안내한 별도 실험에서 같은 첫 업무 행동은 실제 메모 106/154, 빈 메모 34/154였다. [재방문 상담](20261122-retail-two-visit-results.md) · [조건부 조회](20261123-retail-conditional-retrieval.md)

**구현과 실측 범위.** 기존 실행 계획 스키마, 전달·자율 조회·고정 판단 실행기가 있다. 남은 일은 여러 벤치가 이 사양을 공통 명령 하나로 실행하게 연결하는 것이다. 공용 채점기로 소매 첫 행동 대조 12개의 짝 수·고객별 방향과 최종 DB 대조 6개의 완료·성공 수를 재현했다. 고정 커밋 재현 목록에는 이 소매 실험이 아직 없다. 로컬 관측 기록의 중복을 제외한 저장 이벤트 1,638건은 모두 과제 종료 후 작성이었다. 초기 항공의 원기록·요약 기준선에 보고된 21·29는 재개 원장의 배정 행 수이며, 미시작 행과 요약 작성도 포함한다. 최종 고유 상담 슬롯은 조건당 8개, 채점 완료는 **각 5건**이다. [공용 채점 재현 범위](20261125-common-policy-measurements.md) · [계획 스키마](../../schemas/v1.1/run-plan.schema.json) · [고정 재현 목록](../../protocols/reproductions/historical-v1.json) · [항공 최종 배정](../../artifacts/analyses/20261118-two-pass-forms-final-cells/second-pass.csv)

### 은행 열 개 실행 폴더로 넓힌 검산

선택 작성의 39건까지 더하면 완료된 2회차 상담은 159건이다. 첫 고객 발화와 등록된 메모 처치를 포함한 첫 업무 요청을 **159/159건** 재구성했다. 첫 응답의 문구·인수는 157건에서, 검색과 고객 답변 사이의 첫 행동 종류는 13건에서 바뀌었다. 별도로 등록한 판단 416건은 문장 삭제 4지점의 96회와 적용 조건 작성 8지점의 320회다. 416건 모두 실제 1회차의 해당 차례에 등록한 안내와 봉인한 메모를 넣어 전송 요청과 일치시켰다. [단계·조건·과제별 집계](../../artifacts/analyses/20261125-banking-fixed-decision-audit.json)

| 전체 상담 조건 | 왼쪽 공식 성공 | 오른쪽 공식 성공 |
| --- | ---: | ---: |
| 선택 작성, 자동 제공 / 자율 조회 | 5/20 | 6/19 |
| 필수 작성, 자동 제공 / 자율 조회 | 8/20 | 7/20 |
| 성패 제공, 자동 제공 / 자율 조회 | 6/20 | 6/20 |
| 작성 규칙, 기존 / 후보 | 7/20 | 4/20 |

**작성 139건의 단위는 모델 요청이다.** 작성 배정은 120개였고 119개에서 메모를 저장했다. 선택 작성 20배정은 39회 요청했고 1배정이 빈 메모로 끝났다. 이후 필수 작성 100배정은 각각 한 번에 저장했다. 작성자 요청 139개 모두 원천 상담원이 실제 본 마지막 공개 대화·도구 기록과 그 응답에서 작성 입력을 재구성했다. 성패 미제공은 59회, 성패 제공은 80회였고 후자는 원천의 공식 성패 한 값과 일치했다. 작성 문구와 선택·필수 저장 도구 설정도 실험에서 의도적으로 바꾼 요소다. 자율 조회를 제공한 상담 59건은 전부 목록·본문 조회와 메모 노출이 0건이었다. [선택 작성과 결측](20261124-banking-storage-review.md) · [필수 작성](20261124-banking-required-results.md) · [성패 제공](20261125-banking-result-feedback.md)

**비공개 고객 대본.** 업무 요청 1,906개에서 과거 대화에 반복해서 나타난 일반 고객 메시지 7,056개를 모두 같은 상담의 시작 발화 또는 그 시점까지 실제 반환된 고객 발화에 연결했다. 작성 139요청, 고정 판단 416요청에도 같은 출처 검사를 적용했다. 과제 지시를 공백 정규화해 120자씩 60자 간격으로 옮겨 가며 찾고 60자 이상의 완전한 문장도 검색했다. 입력 안의 JSON 본문도 풀어서 대조했다. 대본과 문자 그대로 겹친 것은 작성 50요청, 고정 판단 248요청, 전체 상담의 업무 요청 609개였다. 모두 공개 고객 발화에서 그 문구를 찾았다. 검증 범위는 작성 입력의 출처와 문자 그대로 겹친 문구다. 의미만 같은 바꿔 쓰기의 출처는 이 방법으로 판정할 수 없다. [검사 범위와 분모](../../artifacts/analyses/20261125-banking-fixed-decision-audit.json)

**난이도·고객·비용.** 두 원천 묶음의 공식 성공은 각각 1/4였다. 모든 작성·전달 조건을 합친 후속 상담 성공은 008이 24/29, 014가 8/10, 015가 12/30, 002가 3/10, 001이 2/30, 010이 0/30, 016과 017이 각각 0/10이었다. 001은 두 번 성공했고, 016에서 확인한 것은 당시 원천에 지정 상품 문서가 없었다는 점이다. 과제별 분모에는 서로 다른 메모와 작성 정책이 함께 들어간다. 낮은 성공률만으로 절차 기억의 개선 가능성까지 배제하기는 어렵다. 8과제 중 5개는 확인 가능한 4고객에 연결되고 3개는 이 기준으로 미식별이다. 전체 번호 과제 97개 중 59개에서 32고객을 식별했고 014·015·036은 한 고객을 공유했다. 159상담의 실제 후속 풀이 비용 합계는 $0.573710463, 건당 평균은 $0.003608242, 범위는 $0.000391823∼$0.008228926이었다. 원천 상담·메모 작성 비용은 별도다. [고객 식별 기준과 재현](20261125-framework-integrity-review.md) · [과제·비용 원장 집계](../../artifacts/analyses/20261125-banking-fixed-decision-audit.json)

### 다음 비교에서 답할 두 질문

1. **이 메모가 바로 다음 업무 판단을 개선하는가?** 노출 전의 같은 1회차 판단에서 업무 입력·도구·안내를 고정한다. 같은 양식으로 실제 메모와 빈 메모를 넣고 같은 수로 섞어 반복한다. 업무 행동과 인수, 불필요한 재검색·오답을 사전 지정한다. 빈 메모 반복은 그 지점의 변동을 보여준다. 길이와 형식까지 맞춘 무관한 메모를 쓰면 별도 비교로 기록한다. 개발 지점은 과거 공개 근거와 현재 입력의 단서 유무로 선별한다. 빈 메모가 전부 실패해도 원천에서 뽑은 단서가 행동을 회복시키면 이득을 측정할 수 있고, 전부 성공한 지점에서는 손해나 추가 비용을 잴 수 있다. 목표 행동은 벤치 어댑터가 선언한다. 은행에서는 검색 문서 도달, 소매에서는 목표 주문의 첫 조회, BFCL에서는 함수·인수 선택을 쓸 수 있다. 같은 과제의 과거 풀이에서 얻은 단서를 쓰는 경우와 새로운 과제로 전이시키는 경우를 구분하고, 검증 고객은 개발 고객과 분리한다.
2. **이 저장·조회 정책을 쓰면 상담 전체가 나아지는가?** 열린 메모와 열리지 않은 메모까지 모든 배정을 포함한다. 목록 제목 노출, 본문 반환, 다음 업무 요청에 들어간 근거, 업무 행동, 공식 결과를 연결하고 도구 호출·고객에게 되물은 횟수·작성 및 풀이 비용을 센다. 같은 입력의 반복은 실행 변동을 추정하고, 고유 원천·고객은 다른 사례에 일반화할 범위를 정한다. 새 2회차 무기억 상담은 0건이라는 기존 결정을 유지한다. 이번 검산에서 새 유료 실행은 없었다. [연구 질문과 측정 단위](../research/plan.md)

다음 탐색에서는 새 고객의 무기억 원천을 한 번씩 확보해 같은 궤적에서 다섯 형식을 만들고, 고정 판단에서 실제·빈 메모를 같은 횟수로 섞어 반복하는 쪽을 권한다. 기존 015에서 유용했던 도구 이름이 메모와 검색식에 함께 나타난 것은 개발용 양성 대조의 근거다. 같은 고객 014·036은 독립 확인에 쓸 수 없다. 3∼5회는 한 입력의 변동을 드러내는 반복 수 후보이며, 필요한 고객 수나 검정력을 보장하지 않는다. **같은 과제에서 무기억 1회차를 추가로 반복할 일은 전체 정책의 무기억 대비 순이득이 주 질문이 될 때**다. 그때는 비용을 별도로 등록하고, 같은 실행 시기에 깨끗한 세션으로 교차 배정한다. 지금도 1회차 대비 회복·손실의 관측값은 보고할 수 있다. 빈 메모 반복으로 알 수 있는 것은 같은 메모 인터페이스 안에서 내용이 더한 효과다. [개발 단서와 도달 문서](20261125-common-policy-measurements.md)

JSON으로 감싼 답변은 본문 노출이 없던 자율 조회에서도 나타났다. 일반 문장 렌더링을 시험한다면 이미 등록된 JSON 표현을 보존하고, 같은 지점·내용·안내에서 표현만 바꾼 새 비교로 감싼 비율과 목표 행동을 함께 본다. 공용 고정 판단 실행기에는 공급자 오류 후 대기가 이미 있다. 036의 전체 상담 복구는 원본 결측을 보존한 채, 어느 배정을 재시도하고 비용·중복을 어떻게 처리할지 별도 등록할 사항이다. 확인용 BFCL 한 과제의 고정 스크립트 검증과 공용 결과표는 이미 실행됐다. [응답 포장의 실제 전달](#5-json으로-감싼-답변이-고객에게-도달했는가) · [036 중단 지점](../research/banking-replacement-case.md) · [BFCL 검증 범위](20261125-common-policy-measurements.md)

## 원기록 재현

은행 고정 판단의 세 열은 아래 코드로 다시 셀 수 있다. 각 배정의 봉인 원장과 요청·응답을 열고, 같은 요청 해시 안에서 원응답·행동 종류·등록된 목표 점수를 비교한다.

```python
import json
from collections import defaultdict
from pathlib import Path

from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions

targets = {"before_handoff": "investigate", "after_handoff": "acknowledge",
           "after_identity_check": "verified_lookup", "answer_from_current_terms": "conditional_answer"}
cohorts = [
    ("tau-banking-clause-decisions-01", "20261125-banking-policy-cycle-development.json", "rows"),
    ("tau-banking-scoped-decisions-01", "20261125-scoped-decision-results.json", "trials"),
]
for cohort, filename, field in cohorts:
    groups = defaultdict(list)
    rows = json.loads((Path("artifacts/analyses") / filename).read_bytes())[field]
    for row in rows:
        folder = Path("runs", cohort, f"slot-{row['slot']:03}")
        log = RecordedDecisions(folder, row["run"], row.get("journal") or row["journal_sha256"])
        req, = log.requests
        assert req["request_id"] == (row.get("request") or row["request_id"])
        log.blobs.get(req["request_ref"])
        assert log.finishes[req["request_id"]]["response_ref"]["sha256"] == row["response_sha256"]
        message = log.reply(req)["message"]
        calls = tuple((c["function"]["name"], c["function"]["arguments"]) for c in message.get("tool_calls") or [])
        response = (message.get("content"), calls)
        kind = calls[0][0] if calls else ("TEXT" if message.get("content") else "EMPTY")
        target = row["behavior"] == "target_action" if field == "rows" else row["label"] == targets[row["role"]]
        groups[req["request_ref"]["sha256"]].append((response, kind, target))
    assert all(len(observations) == 4 for observations in groups.values())
    changes = [sum(len({r[i] for r in observations}) > 1 for observations in groups.values()) for i in range(3)]
    print(cohort, len(groups), changes)
```

저장소 루트에서 프로젝트 Python으로 실행한다. 출력은 발화 집계와 미노출 20쌍의 검산이다. 과제별 배정이 가리킨 실행과 원장 해시를 공통 판독기로 확인한다.

```python
import json
from collections import Counter, defaultdict
from pathlib import Path

from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions
from agent_memory_eval.analysis.response_format import reply_format_counts

design = [("memory", "source"), ("policy-check-sources", "source"),
          ("required", "solve"), ("result-feedback", "solve"), ("policy-check", "solve")]
pairs = defaultdict(dict)
for cohort, stage in design:
    root = Path("runs", f"tau-banking-{cohort}-01")
    by_mode = defaultdict(Counter)
    for path in sorted(root.glob(f"task-*/{stage}/allocations.json")):
        for row in json.loads(path.read_bytes()):
            result = row["result"]
            log = RecordedDecisions(path.parent / row["directory"], row["run_id"], result["log_sha256"])
            mode = row["arm"].split("__")[-1]
            by_mode[mode].update(reply_format_counts(log))
            if mode != "on_demand" or cohort not in {"required", "result-feedback"}:
                continue
            worker = [q for q in log.requests if q["actor_role"] == "worker"]
            calls = [log.reply(q)["message"].get("tool_calls") or [] for q in worker]
            assert not log.of_type("memory.input_included")
            assert not any(c["function"]["name"] in {"memory_list", "memory_get"} for step in calls for c in step)
            assert [c["function"]["name"] for c in calls[0]] == ["grep"]
            log.blobs.get(worker[0]["request_ref"])
            key = (row["task_id"], row["arm"])
            assert key not in pairs[cohort]
            pairs[cohort][key] = (worker[0]["request_ref"]["sha256"],
                                  json.loads(calls[0][0]["function"]["arguments"]), result["official_success"])
    print(cohort, dict(by_mode))
old, new = pairs["required"], pairs["result-feedback"]
assert old.keys() == new.keys() and len(old) == 20
assert all(old[k][0] == new[k][0] and old[k][1] != new[k][1] for k in old)
assert Counter((old[k][2], new[k][2]) for k in old) == {
    (True, True): 4, (False, False): 11, (False, True): 2, (True, False): 3}
```
