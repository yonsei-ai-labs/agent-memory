# 이전 실험의 지적으로 다시 본 기억 평가 프레임워크

2026-11-24 · 보관된 실행과 현재 작업 트리의 오프라인 검토 · 새 모델 요청 0건

**같은 무기억 경험에서 여러 메모를 만들고, 후속 판단에서 메모가 어디까지 쓰였는지 비교하는 구조는 원래 연구 질문에 맞는다.** τ²에서는 작성자가 본 근거부터 저장 버전, 선택 직전 후보, 실제 요청에 들어간 본문, 업무 행동, 최종 상태와 비용까지 추적할 수 있다. 현재 가장 명확한 이득은 지난 주문을 찾아가는 첫 행동과 고객의 수고에서 나왔다. 어떤 기억 정책이 새 과제의 최종 성공률을 높이는지, 또는 자율 선택이 간단한 선택 규칙보다 나은지는 별도의 검증이 필요하다. [연구 계획](../research/plan.md) · [상세 설계](../design/architecture.md) · [메모 문장과 행동의 사례](20261123-memory-claim-evidence.md)

## 이전 메모의 지적과 현재 판정

후속 점검은 보관된 항공 5개 실험군의 실제 회차별 요청을 비교하고 소매 1,254건을 다시 재생했다. 아래 결과를 인용할 때는 같은 회차 안의 비교인지, 실행 조건이 달라진 회차 간 관측인지 확인한다. [원천과 후속 실행의 격리 감사](20261124-experiment-isolation-audit.md)

11월 14일 실험팀 인계는 같은 경험을 일반 요약과 적용 조건을 담은 메모로 비교하고, 제목이나 본문에 처음 노출되기 전 같은 판단 지점에서 갈라보기를 제안했다. 15일 기준선 실패 검토는 일반적인 주의 환기, 이미 잘하던 판단의 악화, 고객과 채점기의 영향을 짚었다. 19일 항공 설계 검토와 22일 소매 계획 점검에는 그 우려를 실제로 확인할 수 있는 대조 자료가 남아 있다. [실험팀 인계 검토](../handoff/experiment-handoff-review.md) · [기준선 실패 메모](../research/baseline-failure-review-note.md) · [항공 설계 검토](20261119-memory-experiment-design-review.md) · [소매 계획 점검](20261122-retail-plan-alignment.md)

| 이전에 제기한 문제 | 현재 기록으로 확인한 내용 | 실험 해석에 미치는 영향 |
| --- | --- | --- |
| 무노트 성적이 올라간 원인이 세션 간 유입인가 | 이전 감사는 소매 상담 1,254건의 요청을 새 상담원·고객·DB에서 재구성했다. 상담원 16,041건과 고객 8,144건의 요청 본문이 보관본과 같았다. 6건의 최종 대화 정렬 문제는 해당 DB 채점을 바꾸지 않았다. | 감사한 범위에서 유입을 뒷받침하는 증거는 없다. 수행 변동과 고객의 정정도 결과를 바꿀 수 있다. [세션 격리 감사](20261120-retail-session-isolation.md) |
| 같은 대조를 여러 번 세거나 서로 다른 입력을 같은 반복으로 간주한다 | 종전 27.3%는 빈 메모와 무노트의 성패가 갈린 50/183쌍이다. 안내와 고객 대화가 다르다. 소매 전이 재실험의 주 비교 103쌍에는 독립 무노트 상담 20개, 고객 14명이 있었다. 과거에 관측한 신뢰구간을 최소 검출 효과로 해석할 수도 없다. | 과제·고객·고유 원천 수, 개선·악화·결측을 함께 보고해야 한다. 같은 1회차를 여러 형식에 연결해도 독립 표본 수는 늘지 않는다. [계획 점검](20261122-retail-plan-alignment.md) · [성패가 갈린 31쌍](20261122-retail-transfer-outcome-causes.md) |
| 메모를 열 기회와 실제로 열 이유가 다르다 | 초기 소매 자기·혼합 메모 528배정의 상담원 요청 6,965건에 조회 도구가 있었고 조회는 0건이었다. 이후 고객이 “지난번 그 주문”이라고만 한 시점에 같은 조건부 안내에서 목표 주문만 바로 연 횟수는 실제 메모 106/154, 빈 메모 34/154였다. | 무엇을 저장했는지, 열었는지, 그 정보로 어떤 판단이 달라졌는지에 각각 답할 수 있다. 이전에 평가한 고객 77명의 첫 판단 결과이므로 새로운 고객에서의 상담 성공은 별도 질문이다. [활용 기회 점검](20261122-retail-plan-alignment.md) · [조건부 조회 실험](20261123-retail-conditional-retrieval.md) |
| 공개 경험이 틀렸거나 작성자가 잘못된 교훈을 남길 수 있다 | 소매 32번은 이관 없이 공식 성공했지만 작성자는 이관하지 않은 일을 문제로 남겼다. 이관을 언급한 메모를 받은 31번의 채점 상담 7회 중 5회가 변경 없이 이관했다. 다른 원천에서는 다품목 도구가 마지막 상품의 가격·옵션을 다른 상품에도 기록했다. 관련 목표 상담 72건의 행동과 참조를 함께 교정해 DB 판정을 재생하자 바뀐 성적은 0건이었다. | 사실을 정확히 옮겼는지, 그 사실로부터 적절한 교훈을 냈는지, 지금 적용해도 되는지를 따로 판독해야 한다. 사후 DB 교정으로 당시 상담원이 올바른 반환을 봤을 때의 행동까지 알 수는 없다. 작성자에게 성패만 알려 주는 조건의 소매 비교는 초안이다. [작성·이관 사례](20261122-retail-handoff-factors.md) · [도구 오류 재생](20261122-retail-plan-alignment.md) · [보류된 피드백 초안](../research/handoff-wording-pilot-v1.md) |
| 공식 성공이 실제 업무 수행과 고객 부담을 충분히 반영하는가 | 별도의 소매 **자동 제공** 전체 상담에서는 실제·빈 메모 양쪽 모두 153/153건이 목표 주문을 취소했다. 어느 주문인지 되물은 상담은 128건에서 24건으로, 잘못된 취소 제안은 26건에서 5건으로 줄었다. 항공 채점기에 도구 호출 없이 `Goodbye.`로 끝나는 합성 대화를 넣으면 22번은 0점, 48번은 1점이었다. | 첫 판단, 업무 요건·승인, 최종 상태, 고객 발화·호출·비용을 각각 보고할 이유가 있다. 채점기의 성공·실패 예시도 확인해야 한다. [소매 전체 상담](20261122-retail-two-visit-results.md) · [항공 채점기 검토](20261119-memory-experiment-design-review.md) |

7명으로 한 초기 주문 선택 실험도 다시 해석됐다. 실제 메모 자동 제공과 빈 메모를 비교해 4명이 개선되고 3명은 같았다(양측 부호검정 p=0.125). 판단 횟수의 순증 7건 중 6건은 목표 **주문**이 목록 맨 앞이 아닌 경우에서 나왔다. 위치별 분류는 이 결과를 본 뒤 정했다. 아래에서 새로 검산한 위치는 상담원이 연 **메모**의 목록 위치다. [초기 보고서에 반영된 검토](20261122-retail-order-choice-replay.md)

이관 분석의 예비 메모에도 정정이 있다. `Transferable caution`이라는 표현을 이관 언급으로 잘못 분류한 메모 한 건을 바로잡자, 이관 언급 메모가 배정된 상담의 짝지은 무노트 대비 성공률 차이는 −16.5%p에서 **−11.9%p**(−21.6∼−3.6)로 바뀌었다. 메모·무노트의 변경 0건 이관 **행동** 비율은 8.8%·5.0%이며, 예비 메모의 7.6%·3.2%는 실패로 분류된 이관을 센 값이다. 이관을 언급할지는 무작위 배정하지 않아 특정 문구가 손해를 일으킨 크기로 읽기 어렵다. [정정 내역과 조건별 분모](20261122-retail-handoff-factors.md)

한 문장의 **출처**와 그 문장의 **영향**은 증거가 다르다. 공통 분석기는 공개 경험의 원문 위치, 저장 버전, 실제 판단 입력을 검증한다. 한 소매 사례에서 메모의 주문번호는 첫 방문의 고객 발화와 도구 결과에 있었고, 취소 의사는 재방문의 고객 발화에서 처음 나왔다. 메모가 적절한 교훈인지에 대한 세 사례의 판독자는 한 명이다. 항공에서는 작성자가 아무것도 저장하지 않은 경우에도 양쪽에 성적이 있는 31쌍에서 4개선·6악화가 나왔다. 특정 문구의 영향을 확인하는 비교는 최초 노출 전 같은 상태에서 그 문구만 바꿔야 한다. 교훈의 적절성에 대해서는 조건을 가린 독립 판독으로 해석의 일치도를 점검할 수 있다. [문장별 근거와 작성하지 않은 대조](20261123-memory-claim-evidence.md) · [판독 검증 계획](../development/framework-scoring-plan.md)

## 자율적으로 고른 메모에 필요한 정보가 더 있었나

조건부 안내와 실제 메모를 받은 154건에서 131건은 첫 업무 판단 전에 본문을 열었다. 그중 **둘 이상을 보고 고를 수 있던 116건**의 첫 본문 조회를 대조했다. 같은 고객의 두 가능한 방문 이력은 각 실행에 하나씩 배정됐다. 상담원이 본 후보는 **그 과거 상담 하나**를 사실·일기·교훈·절차로 작성해 저장한 항목들이었고, 표시 순서도 이 형식 순서였다. 116건 모두 실제 표시된 목록과 봉인한 저장 항목의 순서·버전이 일치했다. 상담원은 첫 항목을 109회, 두 번째 항목을 7회 골랐다. [조건과 목록 순서](../../protocols/pilots/tau2-retail-conditional-retrieval-v1.json) · [저장 항목 결합 코드](../../scripts/run_retail_conditional_retrieval.py) · [배정별 첫 선택](../../artifacts/analyses/20261123-retail-memory-selection.json)

| 제목 또는 본문에 등록된 목표 주문번호 전체가 그대로 있었는가 | 선택한 항목에 있음 | 선택한 항목에 없음 |
| --- | ---: | ---: |
| 목록 첫 항목에 있음 | 85 | 1 |
| 목록 첫 항목에 없음 | 2 | 28 |

선택한 항목에는 **87/116건**, 첫 항목에는 **86/116건**에서 번호가 있었다. 첫 항목을 건너뛴 7건 중 2건은 번호를 얻었고 1건은 놓쳤으며 4건은 어느 쪽에도 없었다. 13건에서는 모든 후보에, 20건에서는 어디에도 번호가 없었다. 일부 후보에만 번호가 있던 83건으로 좁혀도 선택한 항목 74건, 첫 항목 73건이다. 비교한 것은 **첫 선택으로 정확한 번호가 적힌 항목에 도달했는지**다. 번호가 없어도 유용한 절차가 있을 수 있고, 번호가 있어도 과거 거래에만 유효할 수 있다. 첫 항목을 강제한 상담의 행동·성패는 관측하지 않았다.

연구자가 추가한 조건부 안내는 현재 요청이 어떤 주문을 가리키는지 불명확하면 고객에게 묻기 전에 이전 메모를 볼 수 있다고 했다. 어떤 번호나 항목을 고를지는 알려 주지 않았다. 기존 안내와 실제 메모에서는 35/154건이 목표 주문만 바로 열었다. 조건부 안내에서 메모를 통해 번호가 판단 입력에 들어간 94건 중 91건은 그 주문만 바로 열었다. 재방문 화면을 그대로 두고 같은 고객의 **과거 주문만** 달리했을 때 두 목표를 각각 맞힌 고객도 실제 메모 37/77명, 같은 안내와 빈 메모 0/77명이었다. 따라서 106 대 34의 전체 배정 비교는 **메모가 있는 조회 정책이 첫 행동을 바꿨나**에 답한다. 여러 과거 경험 중에서 가장 관련 있는 항목을 고르는 능력을 검증하려면 후보가 실제로 서로 다른 경험을 담은 조건이 필요하다. [안내 원문](../../protocols/instructions/tau2-retail-conditional-retrieval-v1.txt) · [고객별 두 과거 주문 비교](20261123-retail-conditional-retrieval.md) · [노출과 첫 행동](20261123-memory-selection-efficiency.md)

## 다음 정책을 판단할 기준

1. **보관된 1회차 무기억을 재사용한다.** 같은 과제라면 한 1회차에서 나온 메모 가지마다 개선·악화·유지·미채점을 센다. 이것은 회차 간 관측 변화다. 과거 소매 기록에서는 첫 실패 13과제 중 8개가 무기억 재시도만으로도 회복됐다. 정책 채택의 근거는 같은 상태·조건에 배정한 **2회차 기존 메모 대 후보 메모**의 차이다. 새 과제로 전이할 때는 대상 자체의 비교 가능한 기준선이 있어야 회차 간 변화를 낼 수 있다. 새 2회차 무기억 없이 두 비교를 운영할 수 있다. [소매 재시도 결과](20261119-retail-full-forms.md) · [두 회차 비교 설계](../development/memory-policy-improvement.md)
2. **업무 결과가 달라질 수 있는 과제를 먼저 고른다.** 예를 들어 과거 결제 선호를 다시 써도 되는 경우와 고객이 이번 거래의 결제 방식을 바꾼 경우를 함께 둔다. 조건에 맞게 결제를 마친 예와 과거 수단을 잘못 쓴 예를 채점기가 구분하는지 먼저 확인한다. 선택을 시험할 때는 같은 은행의 순서만 바꾸고, 여러 이력 중에서 골라야 하는 경우도 넣는다. 전체 상담에서는 새 고객에서 최종 성공, 기존 성공을 잃은 비율, 되물음·업무 호출·토큰·비용을 본다. 작성 비용은 재사용 횟수에 맞춰 별도로 반영한다. [항공 결제 개발 초안](../../protocols/drafts/tau2-airline-payment-memory-v2.json) · [효율 지표의 분모](20261123-memory-selection-efficiency.md)
3. **여러 벤치와 자동 정책 개선은 실제 연결로 검증한다.** 작성 근거·노출·효율의 공통 분석과 근거를 인용한 작성 지침 후보 생성 경로는 있다. 항공 v2 초안의 사전 점검은 개발 원천 2개, 작성 4개, 후속 메모 상담 4개, 후속 무기억 0개이며 확인 평가 집합은 비어 있다. 개발용 2회차 결과로 다음 저장·조회 후보를 만들고, 보지 않은 고객에서 기준을 넘은 정책을 채택한 기록은 아직 없다. 두 번째 벤치에서도 간격이 보인다. AppWorld 3과제와 BFCL 155문항의 공식 채점은 재현되지만, BFCL 고유 읽기·쓰기는 현재 공통 라우터에서 기억 허용 조건에도 차단된다. 두 번째 실제 환경에서 같은 경험→쓰기→선택→행동→채점 경로 하나가 끝까지 이어져야 이식성을 확인할 수 있다. [공통 측정 계획](../development/framework-scoring-plan.md) · [후보 생성과 채택 기준](../development/memory-policy-improvement.md) · [벤치 호환성 실측](20261123-framework-portability.md)

11월 22일의 “점수화가 실험마다 따로 구현된다”는 지적은 일부 해소됐다. 현재는 공통 코드가 선택 전 입력과 반환, 짝의 득실·결측·효율을 계산한다. ‘올바른 첫 주문 조회’와 ‘항공 공식 성공’을 판정하는 코드는 여전히 각각의 분석기에 있다. 새 벤치는 어떤 업무의 어느 시점을 성공으로 셌는지와 채점기 버전을 제공하고, 공통 보고서는 분모·판정 근거·비용을 보존해야 비교의 의미가 유지된다. 대표 소매 본 실행·복구 결과를 고정 커밋 재현 목록에 등록하는 일도 남아 있다. [공통 분석과 실험별 판정의 경계](20261123-framework-portability.md) · [고정 재현 목록의 보완 계획](../development/framework-scoring-plan.md)

## 재현

소매 계획 점검·이관 분석, 항공 실험 설계 검토, 메모 문장별 근거, 소매 첫 메모 선택, AppWorld·BFCL 호환성 점검을 각 보고서의 로컬 재현 명령으로 실행했다. 여섯 결과 JSON이 저장소의 결과와 바이트 단위로 일치했다. 저장소 기본 검사 세 가지와 현재 작업 트리의 전체 단위 테스트 556개도 통과했다. 위의 새 선택 표는 저장소 루트와 보관된 실행을 사용해 다음과 같이 다시 센다. 목표번호는 집계할 때만 사용한다.

```bash
uv run python - <<'PY'
import json
from collections import Counter
from pathlib import Path

from agent_memory_eval.analysis.selection import worker_choices
from agent_memory_eval.recording.assets import asset_text
from scripts.run_retail_conditional_retrieval import READY, frozen_banks

root = Path.cwd()
selection = json.loads((root / "artifacts/analyses/20261123-retail-memory-selection.json").read_bytes())
result = json.loads(asset_text(root, selection["source"]))
folder = root / "runs" / result["study"]
registration = json.loads(asset_text(folder, selection["registration"]))
jobs = {j["run"]: j for j in registration["jobs"]["decision"]
        if j["arm"] == selection["arm"] and j["mode"] == selection["mode"]}
banks, hashes = frozen_banks(root, json.loads((root / READY).read_bytes()), {j["source"] for j in jobs.values()})
assert len(jobs) == 154 and set(jobs) == {case["run"] for case in selection["cases"]}
table, coverage, ranks = Counter(), Counter(), Counter()

for case in selection["cases"]:
    if case.get("page_size", 0) < 2:
        continue
    job = jobs[case["run"]]
    items, writers = banks[job["source"]]
    assert writers == job["writer_runs"] and hashes[job["source"]] == job["items_sha256"]
    trace = worker_choices(folder / case["run"], through_request=case["first_get"])
    choice = trace["first_body_choice"]
    page, = choice["matched_list_entries"]
    fields = trace["choices"][-1]["visible_memory"]
    visible = [f for f in fields if f["via"] == "list" and f["field"] == "title" and f["pointer"] == page["pointer"]]
    assert [(f["item_id"], f["version_id"], f["title"]) for f in visible] == [
        (item["id"], item["version"], item["title"]) for item in items]
    position = page["position"]
    assert page["size"] == len(items) == case["page_size"] and position == case["position"]
    assert choice["returned_status"] == "ok" and choice["item_id"] == items[position - 1]["id"]
    contains = [any(job["target"] in item[k] for k in ("title", "body")) for item in items]
    table[contains[0], contains[position - 1]] += 1
    coverage["all" if all(contains) else "some" if any(contains) else "none"] += 1
    ranks[position] += 1

print([table[first, chosen] for first in (True, False) for chosen in (True, False)])  # [85, 1, 2, 28]
print(dict(sorted(coverage.items())), dict(sorted(ranks.items())))  # all 13, none 20, some 83 / 1: 109, 2: 7
PY
```
