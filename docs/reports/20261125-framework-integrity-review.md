# 기억 평가 프레임워크 점검과 다음 검증 순서

2026-11-25 · 현재 작업 트리, 보관된 공식 실행, 고정 버전의 벤치와 로컬 배포본을 사용한 오프라인 점검

**같은 무기억 경험에서 여러 메모를 만들고, 다음 판단에 도달한 정보와 그 뒤의 업무 행동을 추적하는 방향은 원래 연구 질문에 맞는다. 지금 먼저 보완할 곳은 결과를 비교하는 단위와 분모다.** 같은 고객의 다섯 형식, 고객 도구가 실제로 바꾼 DB, 원천 실행이 끊겨 생긴 결측을 벤치가 바뀌어도 같은 뜻으로 표시해야 한다. 새 실험 모델 호출은 0건이었다. [연구 계획](../research/plan.md) · [공통 점수 체계 계획](../development/framework-scoring-plan.md)

## 무엇까지 근거가 있는가

| 연구 질문 | 확인한 범위 |
| --- | --- |
| 같은 경험에서 여러 형태를 썼는가? | 필수 작성 검사는 원천 실행·공개 궤적의 해시, 피드백 조건, 제목·본문, 형식별 저장과 전달 연결을 검사한다. 은행 작성 정책 비교에서 기존·후보 각각 20개, 총 40개가 저장됐다. [필수 형식 검사](../../src/agent_memory_eval/experiments/forms_batch.py) · [은행 결과](20261125-banking-policy-cycle.md) |
| 업무 외에 무엇이 바뀌었는가? | 은행의 작성 입력 20쌍과 풀이의 첫 입력 20쌍을 다시 대조한 파일이 원본과 바이트 단위로 같았다. 두 정책의 작성 지시 또는 배정 메모 차이를 분리할 수 있다. 이후 새 고객 발화·업무 도구 결과가 달라지면, 그 뒤의 행동은 상류 조건까지 달라진 것으로 표시할 수 있다. [입력 감사](../../artifacts/analyses/20261125-banking-policy-cycle-input-audit.json) · [판단 비교기](../../src/agent_memory_eval/analysis/decision_attribution.py) |
| 실제로 선택·노출·사용됐는가? | 조회 반환과 다음 업무 요청의 제목·본문 노출은 구분된다. 은행 필수 작성 실험의 자동 제공은 20/20 본문 노출, 자율 조회는 0/20 조회였다. 소매에서는 여러 제목을 본 뒤 본문을 연 116건 중 109건에서 처음 고른 항목이 맨 앞이었다. 순서가 고정되어 관련성에 따른 선택과 위치 효과를 분리하기 어렵다. 문장의 출처와 현재 요청의 다른 정보도 대조해야 한다. [은행 결과](20261124-banking-required-results.md) · [선택 경로](20261123-memory-selection-efficiency.md) · [문장별 근거 추적](../../src/agent_memory_eval/analysis/claim_evidence.py) |
| 메모가 유용한 판단을 잡을 수 있는가? | 이전 소매 실험에서 고객이 지난 주문을 가리킨 같은 시점·같은 조회 안내로 비교했을 때 목표 주문을 바로 연 경우는 실제 메모 106/154, 빈 메모 34/154였다. 이 기록은 첫 행동 개선을 감지하는 양성 대조로 쓸 수 있다. 최종 상담 성공은 이 비교에서 측정하지 않았다. [고정 판단의 행동 근거](20261124-decision-attribution.md) |
| 적정 난이도와 성공 개선을 확인했는가? | 고정 판단 8곳 중 7곳에서 두 작성법 모두 전부 정답을 냈다. 전체는 기존 153/160, 후보 152/160이다. 반면 새 과제의 끝까지 진행한 상담에서는 기존 7/20, 후보 4/20이었고 016과 017은 두 정책에서 모두 공식 성공 0/5였다. 다음 과제는 난이도와 함께, 메모가 어느 구체적인 결정에 필요한지 사전에 선별할 필요가 있다. [고정 판단](20261125-banking-scoped-writer-results.md) · [전체 상담](20261125-banking-policy-cycle.md) |

## 결과 해석에 영향을 주는 세 가지

**1. 개발과 검증 사이에 같은 고객이 있다.** 은행 공식 데이터의 고객 식별자, 이름·전화번호를 대조했다. 개발 015번, 검증 014번, 새 사례 036번은 같은 고객이다. 036번의 본인확인 행동과 014·015번의 시나리오가 같은 식별자를 가리킨다. 새 세션과 메모 저장소를 분리한 상태에서도 과제 간 고객은 겹칠 수 있다. 사전등록도 과제 ID 분리까지만 약속했다. 공동 성공 3쌍은 전부 014번이다. 참고로 이 과제 하나를 빼면 나머지 세 과제에서 기존 2/15, 후보 1/15이고 공동 성공은 0쌍이다. 이는 결과가 한 과제에 얼마나 집중됐는지 보여주는 사후 점검이다. 다음에 새 고객에게도 적용된다고 주장하려면 등록 시점에 원천·고객 단위의 겹침을 검사하고, 판별할 수 없는 경우도 남긴다. 이 식별 정보는 분석 계층에 둔다. [등록한 분할 규칙](../../protocols/pilots/tau-banking-policy-cycle-v1.json) · [과제별 결과](../../artifacts/analyses/20261125-banking-policy-cycle-validation.json) · [036 사례](../research/banking-replacement-case.md)

다음 분할의 가능성도 확인했다. 은행 고정본의 번호가 있는 과제 97개에서 공식 본인확인 행동의 고객 ID 또는 시나리오에 함께 나온 DB상의 이름·전화번호를 대조했다. 59개는 이 기준으로 고객 한 명에 연결됐고 고유 고객은 32명이었다. 나머지 38개는 이 방식으로 식별되지 않았다. 익명 문의나 신규 신청은 별도 집단으로 다룰 수 있다. 미식별 38개를 새 고객 38명으로 계산할 근거는 없다.

**2. 고객이 직접 실행한 업무가 기존 효율 표에서 빠진다.** 40개 상담의 공식 보고서와 실행 로그 해시를 확인했다. 기존 업무 호출 244건은 상담원 호출과 일치한다. 고객도 7개 상담에서 총 8번 업무 도구를 호출했다. 002번의 카드 신청 6번과 017번의 이의신청 2번이다. 후자 두 건은 발화 텍스트가 없는 고객 차례였고 목표 거래 한 건과 비대상 거래 한 건이 실제 접수됐다. 현재 효율 함수는 텍스트가 있는 고객 발화와 상담원 업무 호출을 센다. 새 결과표에는 행위자별 업무 호출과 고객에게 넘긴 일, 목표·비대상 변경의 시도/접수를 따로 둔다. 비용은 작성·조회·풀이를 구분하고 공동 성공 효율에는 공동 성공 쌍 수를 함께 쓴다. [효율 집계](../../src/agent_memory_eval/analysis/efficiency.py) · [공식 접수와 대상 검산](20261125-banking-policy-cycle-diagnosis.md)

**3. 전부 결측인 비교가 상위 표에는 0으로 보인다.** 036번에서 고객 모델의 HTTP 429로 무기억 원천이 미채점으로 끝났다. 작성과 후속 상담은 각각 0/10건 시작했다. 원자료를 재분석하면 다섯 비교쌍이 전부 결측이다. 동시에 요약에는 두 정책의 성공 건수 0과 공동 성공 효율 합계 문자열 `"0"`이 나온다. 성공 **건수** 0은 맞지만 성공률과 효율을 추정할 분모는 0이다. 공통 `paired_effort`에는 관측 수를 표시하고 값이 없을 때 합계를 `null`로 두는 구현이 이미 있다. 배정·시작·채점·성공 건수를 정책별로 내고 공동 채점 수를 따로 표시하면 부분 결측도 다룰 수 있다. 원장 봉인, 실험 완료, 확인 비용($0.000751475)과 보류액($0.003572400) 역시 별개다. [036 원본 결과](../../artifacts/analyses/20261125-banking-replacement-original-results.json) · [은행 비교 집계](../../scripts/analyze_banking_memory.py) · [공통 짝비교](../../src/agent_memory_eval/analysis/efficiency.py)

## 다른 벤치와 정책 개선에 남은 일

- **패키지 설치.** 소스와 계약 스키마 디렉터리를 함께 복사해 wheel을 만들고 저장소 밖 임시 폴더에 설치했다. 소스 체크아웃에서는 계약 검증기 8개를 불러와 첫 메모 저장소를 만들었다. wheel에는 `.schema.json`이 0개여서 검증기도 0개였고 새 원장의 첫 메모 저장소 생성에서 `'journal-state'` 오류가 재현됐다. 런타임 스키마를 배포 자원에 포함하고, 저장소 밖 첫 기록을 회귀 검사로 삼을 수 있다. [빌드 설정](../../pyproject.toml) · [스키마 탐색](../../src/agent_memory_eval/contracts/validation.py)
- **두 번째 벤치.** BFCL `simple_python` 400과제의 첫 공개 입력을 대조했다. 별도 BFCL Memory 155문제의 공식 채점기는 기준 답 155개를 받아들이고 무관한 답 155개를 모두 거절했다. 공통 라우터는 기억이 허용된 경우에도 BFCL 고유 조회를 차단했다. AppWorld는 세 과제의 공식 채점과 초기 상태 복원을 재현했다. 이 호환성 결과 파일도 원본과 바이트 단위로 일치했다. 다음 이식성 검증은 BFCL 한 과제에서 **저장 → 새 세션 조회 → 다음 판단에 해당 버전 포함 → 업무 행동 → 공식 채점**을 끝까지 확인하는 것이다. [벤치별 실측](20261123-framework-portability.md) · [호환성 결과](../../artifacts/analyses/20261123-framework-portability.json)
- **LLM의 개입과 판독.** 공개 개발 경험을 내보내고 인용을 검증해 새 작성 지침 초안을 만드는 경로가 있다. 다음 제안에 2회차의 조회 선택·업무 행동·득실·비용을 돌려주는 공통 경로와 조회 정책 제안은 미구현이다. 독립 판독용 은행 판단 32건도 준비됐지만 사람·모델 간 판정 일치도는 미측정이다. 절차나 교훈의 사용을 판독할 때는 조건을 가리고 원문을 인용하게 한 뒤, 사람과의 일치도 및 판정 보류율을 평가한다. 공식 행동·DB 상태와 의미 판독을 결과표에서 구분한다. [정책 개선 경로](../development/memory-policy-improvement.md) · [가림 판독 설계](../development/memory-evidence-review.md)

## 다음 작업과 통과 조건

별도 세션의 여덟 가지 제안을 추가 점검했다. 같은 입력·미노출 20쌍의 5쌍에서 성패가 갈렸고 JSON으로 감싼 발화 312개 모두가 고객 입력으로 그대로 전달됐다. 다음 은행 판단을 고르는 기준과 검정 단위, 공통 응답 형식 진단을 함께 기록했다. [추가 검토와 원기록 재현](20261125-banking-feedback-audit.md)

1. **원기록으로 공통 결과표 한 장을 먼저 만든다.** 기존 40건과 미시작 036을 같은 분석기로 읽는다. 7/20·4/20, 공동 성공 3쌍의 출처, 상담원 244·고객 8회, 목표·비대상 접수가 동시에 재현돼야 한다. 036은 양쪽 채점 0건, 성공률·공동 효율은 산출 불가로 표시한다. 새 고객 성과를 보고하는 경우 개발·검증 간 고객/원천 겹침까지 검사한다. 보관한 원본 결과는 과거 재현 기준으로 쓴다.
2. **독립 설치한 공통 경로를 BFCL 한 과제로 검증한다.** 로컬 모의 작업자로 메모 버전이 실제 다음 업무 입력에 포함되는 경우와 접근을 차단하는 경우를 모두 확인한다. 기존 공식 채점 대조도 다시 통과해야 한다. 이 단계는 어댑터와 기록자의 동작 검증이다.
3. **그 뒤 작성 또는 조회 규칙 하나를 바꾸는 실험을 한다.** 같은 무기억 1회차, 다섯 필수 형식, 같은 첫 질문·DB·업무 도구를 유지하고 새 2회차 무기억 배정은 0건으로 둔다. 다섯 형식이 공유한 1회차는 한 원천으로 세며, 첫 판단 이후 달라진 고객·모델 응답과 회차 간 성패 전환을 함께 해석한다. 조회를 시험할 때는 같은 저장본에 읽기 정책만 바꾼다. 판단 직전의 대화·도구 결과에 이미 있는 정보, 메모가 새로 주는 단서, 과거 값이라 재확인이 필요한 경우를 사전에 표시한다. 개발에서 제안한 뒤 겹치지 않는 확인 집합에서 채택을 판단한다. 첫 행동, 최종 공식 성공, 비대상 변경, 고객 부담, 작성까지 포함한 비용을 같이 평가한다.

## 수치 재확인

아래 코드는 저장소 루트와 보관된 `data/`·`runs/`를 사용한다. 고객 원문을 출력하지 않고 신원 일치와 역할별 호출을 확인한다.

```python
import json
from collections import Counter
from hashlib import sha256
from pathlib import Path

def read(path):
    return json.loads(path.read_bytes())

domain = Path("data/tau2-bench/data/tau2/domains/banking_knowledge")
def task(number):
    return read(domain / "tasks" / f"task_{number:03d}.json")

customers = read(domain / "db.json")["users"]["data"]
customer_id = task(36)["evaluation_criteria"]["actions"][0]["arguments"]["user_id"]
customer = customers[customer_id]
for number in (14, 15, 36):
    scenario = task(number)["user_scenario"]["instructions"].casefold()
    assert all(customer[key].casefold() in scenario for key in ("name", "phone_number"))
    if number in (14, 15):
        assert customer_id.casefold() in scenario

identified = Counter()
unidentified = 0
for path in (domain / "tasks").glob("task_*.json"):
    case = read(path)
    scenario = case["user_scenario"]["instructions"].casefold()
    ids = {action["arguments"].get("user_id") for action in case["evaluation_criteria"]["actions"]
           if action["name"] == "log_verification"} & customers.keys()
    for key, person in customers.items():
        if all(person[f] and person[f].casefold() in scenario for f in ("name", "phone_number")):
            ids.add(key)
    assert len(ids) <= 1
    identified.update(ids)
    unidentified += not ids
assert (identified.total(), len(identified), unidentified) == (59, 32, 38)

totals = Counter()
for path in Path("runs/tau-banking-policy-check-01").glob("task-*/solve/allocations.json"):
    for row in read(path):
        folder = path.parent / row["directory"]
        ref = row["result"]["report_ref"]
        raw = (folder / ref["path"]).read_bytes()
        assert sha256(raw).hexdigest() == ref["sha256"]
        assert sha256((folder / "log.jsonl").read_bytes()).hexdigest() == row["result"]["log_sha256"]
        assert row["result"]["grading_status"] == "graded"
        calls = Counter(m["role"] for m in json.loads(raw)["messages"]
                        for c in m.get("tool_calls") or [] if not c["name"].startswith("memory_"))
        totals.update(calls)
        totals["consultations"] += 1
        totals["with_customer_call"] += bool(calls["user"])
assert totals == {"consultations": 40, "assistant": 244, "user": 8, "with_customer_call": 7}
```

이번에 다시 만든 은행 공식 결과, 은행 입력 감사, 036 원본 집계, BFCL·AppWorld 호환성 파일 네 개는 보관본과 각각 바이트 단위로 일치했다. 벤치 호환성 검증은 고정된 공식 체크아웃과 로컬 런타임을 사용했다. [은행 결과·입력 대조 재현](20261125-banking-policy-cycle.md#오프라인-재현) · [036 원본 집계 재현](../research/banking-replacement-case.md) · [두 벤치 재현](20261123-framework-portability.md)
