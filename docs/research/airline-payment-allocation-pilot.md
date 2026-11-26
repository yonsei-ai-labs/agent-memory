# 지난 결제 배분을 기억하고, 오늘 바뀐 지시를 따르는가

2026-11-24 · 항공 23번 개발 실험 · 기본 작성 지침의 실제 실행 완료

이 설계의 세 고객 대본은 과거 선택의 복원과 변경을 살펴본 개발용 변형이다. 보관된 1회차와 메모리만 바꾸는 본 비교에는 원래 고객 대본을 그대로 사용하고 원천의 모델 시드·업무 설정까지 일치시킨다. 이번 15건 중 이 기준을 충족한 사례는 0건이었다. [대본·시드·한도 대조](../reports/20261124-airline-allocation-forms.md#1회차와-메모리만-달랐는가)

기본 지침의 작성 5회와 후속 상담 15건을 마쳤다. 승객별 증서 번호를 복원할 수 있는 메모는 0/5였고, 후속 상담에서 실제 배분 일치는 13/15, 공식 성공은 7/15였다. 보완 지침과의 비교는 다음 단계다. [실험 결과와 재현 방법](../reports/20261124-airline-allocation-forms.md)

**같은 1회차 무기억 경험을 정보·경험 일기·절차·교훈·자유 구성의 다섯 방식으로 남기면, 어느 방식이 지난 배분을 복원하고 오늘의 변경도 잘 반영하는지 비교한다.** 자유 구성에서는 LLM이 남길 내용과 표현을 고르고, 여러 종류를 섞거나 저장을 생략할 수 있다. 고객이 정한 대상과 값, 승인의 적용 범위를 보존하라는 보완 지침은 각 방식에 동일하게 적용해 비교한다. 항공 23번은 이미 분석에 사용한 개발 과제다. [연구 질문](plan.md#문제와-연구-질문) · [기억 정책 개선 설계](../development/memory-policy-improvement.md)

## 이 과제를 먼저 쓸 근거

1회차 무기억 상담에서 고객은 세 승객의 예약을 따로 만들고, Raj와 Liam에게는 **서로 다른 250달러 여행 증서**를 쓰라고 지정했다. 상담원은 승인받은 배분으로 발권했고 공식 점수는 1이었다. 현재 출발 DB의 지갑에는 두 증서의 번호와 금액이 있지만 승객과의 대응은 없다. 이 고객의 기존 예약 한 건도 두 증서의 결제 이력을 담고 있지 않다. [개발 원천과 1회차 공개 기록 추출 방법](../development/memory-policy-improvement.md#현재-실행할-수-있는-개발-단계)

같은 원천을 준 과거 두 반복에서 정보·절차·교훈은 각각 2/2회 저장했고, 경험 일기는 0/2회였다. 본문을 저장한 6건 어디에도 두 승객의 이름이나 두 증서 번호의 마지막 네 자리가 나오지 않았다. 정보 메모는 결제 총액을, 절차 메모는 발권 전 승객별 배분을 설명하고 확인받는 순서를, 교훈 메모는 보유 총액과 예약 한 건에 쓸 수 있는 수단의 차이를 남긴 사례가 있다. 이 관측은 한 원천에서 나온 내용과 저장 여부다. 이번에 추가하는 자유 구성은 예전에 별도로 작성한 일반 요약과도 다른 지침이다. [기존 네 작성 형식과 요약 지침](../../protocols/drafts/tau2-two-pass-memory-forms-v1.json)

고정된 τ² 채점기로 1회차 성공을 재현한 뒤, 배분만 다르게 한 업무 행동을 재생했다. 네 경우 모두 취소·예약 도구 호출이 정상 처리됐고, 마스터카드 청구 총액은 1,286달러, 필수 금액 안내는 모두 정답이었다. 아래에서 ‘교환’은 두 승객의 증서를 서로 바꾼 개발용 과제 변형이다. 이 표는 실행 전 채점기 검산이다. 실제 후속 상담 성적은 결과 보고서에 수록했다.

| 고객이 지정한 증서 배분 | 원래 배분으로 발권한 점수 | 교환해 발권한 점수 |
| --- | ---: | ---: |
| 원래 배분 | 1 | 0 |
| 교환한 배분 | 0 | 1 |

## 같은 경험에서 무엇을 남길지

각 작성자는 같은 공개 상담을 독립된 세션과 빈 저장소에서 받는다. 네 지정 형식에는 기존 영문 작성 지침을 사용한다. 아래 예시는 분석자가 원문과 결과를 대조할 때 사용하는 예다. 작성자의 공용 지침에는 승객 이름·증서 번호·정답 배분을 넣지 않는다.

| 작성 방식 | 표현의 차이와 확인할 내용 |
| --- | --- |
| 정보(사실) | 누가 어떤 선택을 했고 그 사실이 어느 거래에 해당했는지 남기는가. 나중에 승객별 배분을 복원할 수 있는가. |
| 경험 일기 | 요청, 고객의 확인·정정, 발권과 도구 응답을 시간순으로 남기는가. 처음 제안한 값과 실제 실행한 값을 구별할 수 있는가. |
| 절차 | 현재 지갑 조회, 승객별 대조, 승인 후 발권 같은 확인 순서와 멈출 조건을 남기는가. 대상이 달라져도 쓸 수 있는가. |
| 교훈 | 같은 액수의 증서에도 서로 다른 배분이 있을 수 있다는 주의점과 적용 조건을 근거 있게 남기는가. 과거 한 건에서 규칙을 과도하게 일반화하는가. |
| 자유 구성 | 무엇을 어떤 형태로 남길지 LLM이 정한다. 사실과 절차를 한 항목에 묶거나, 고객 선호·예외·이전에 실패한 시도처럼 경험에서 발견한 내용을 별도 항목으로 만들 수 있다. 실제 조합과 저장 생략을 모두 기록한다. |

**작성 방식과 보완 지침을 교차한다.** 기본 조건은 각 방식의 원래 지침을 쓴다. 보완 조건은 같은 지침에 ‘경험에서 어떤 선택을 남기기로 했다면, 그 대상과 선택값 사이의 관계 및 허락이 적용된 범위를 원문이 뒷받침하는 만큼 보존하라’는 공통 한 문장을 덧붙인다. 동일한 지침 수준에서 다섯 방식을 비교하고, 각 방식 안에서 기본·보완의 차이를 본다. 정보·교훈·절차가 실제로 한 메모에 함께 나타날 수 있으므로 배정된 형식과 실제 저장된 내용은 각각 기록한다.

기존 작성기의 동일한 상한(저장소당 4항목, 전체 본문·제목 용량과 모델 요청 한도)을 모든 조건에 적용한다. 자유 구성도 하나의 작성 세션에서 자기 저장소만 만든다. 다른 형식의 작성물이나 2회차 고객 대본·채점 정보는 볼 수 없다. 정상적으로 아무것도 남기지 않은 저장소도 그대로 2회차에 배정하고, 기술 실패는 별도로 기록한다. [작성 한도와 격리 규칙](../../protocols/drafts/tau2-two-pass-memory-forms-v1.json) · [저장 동작](../../src/agent_memory_eval/experiments/forms_write.py)

## 작성한 메모에서 확인할 근거

문헌은 세부 사실이 빠져 생긴 손실과 과거 절차를 새 조건에 맞지 않게 적용한 손실을 각각 보고한다. 23번의 개발 실험은 다섯 작성 방식에 동일한 시작 제공을 적용한다. 아래 항목은 **작성 조건과 2회차 결과를 가린 분석자가 읽을 질문**이다. 작성자는 기존 형식별 지침과 1회차 공개 경험을 받으며, 어떤 항목을 남길지는 스스로 정한다. [원논문 일곱 편의 비교와 적용 범위](related-work.md#기억-작성-방식의-실험-근거)

| 판독 질문 | 기록할 근거 |
| --- | --- |
| 메모만 읽고 지난 선택을 복원할 수 있는가 | 승객·증서 배분을 완전히 복원할 수 있는지, 일부만 남았는지, 빠졌는지 기록한다. 기준은 두 승객에 대한 원래 지정이며 다른 방식으로 대상을 명확히 가리킨 표현도 인정한다. 원문과 모순되는 값은 따로 표시한다. |
| 무엇을 보고 쓴 문장인가 | 메모 문장과 이를 뒷받침하거나 반박하는 1회차 고객 발화·도구 응답 위치를 짝짓는다. 고객이 요청한 것, 상담원이 제안한 것, 실제 발권된 것을 구분한다. 원문에서 확인할 수 없는 교훈·실패 원인은 추정으로 표시한다. |
| 과거 선택이 오늘까지 적용되는가 | 작성자가 당시 한 번의 승인으로 남겼는지, 계속 유효한 선호로 주장했는지, 범위를 밝히지 않았는지 적는다. 오늘 고객이 배분을 바꾸면 현재 발화에 맞춘 첫 제안·최종 발권과 대조한다. |

예를 들어 ‘지난 예약에서는 고객이 두 승객의 증서를 따로 지정했다’는 사건만 담으므로 두 사람의 배분을 복원하기에는 부족하다. ‘발권 전에 승객별 배분을 읽어 주고 승인받는다’는 다음 거래에도 쓸 수 있는 절차다. 한 메모에 실제 대응 관계와 확인 절차가 함께 있을 수 있다. 어떤 조건이 더 좋았는지는 세 대본을 각각 수행한 뒤 판단한다. ‘지난번처럼’에서 정확한 첫 제안이 늘어도, 변경 대본에서 옛 배분을 고집했다면 두 결과를 함께 보고한다. 승인에 필요한 확인과 이미 명시한 값을 다시 묻는 행동도 따로 센다.

원문 인용은 먼저 분석 기록에 남긴다. 이후 작업자도 그 근거를 읽을 수 있게 할 때는 같은 작성물과 동일한 조회 안내를 사용하고, 1회차 공개 원문에 접근할 수 있는지만 바꿔 별도로 비교할 수 있다. [문장과 득실을 연결하는 기준](metrics.md#메모-문장과-득실을-연결하는-근거)

보관 원천의 공개 메시지 배열은 0부터 센다. 25번에서 고객이 승객별 배분을 직접 지정하고, 26번에서 상담원이 분할 발권 내역을 제안하며, 27번에서 고객이 금액을 확인한다. 31번과 33번의 업무 도구 응답에는 각각 Raj와 Liam의 실제 발권·결제 내역이 들어 있다. 이 위치는 원문과 메모를 판독할 때 사용한다.

## 2회차에서 비교할 것

| 현재 고객 요청 | 기대하는 행동 | 찾아낼 수 있는 손해 |
| --- | --- | --- |
| “지난번과 같은 배분으로 해 주세요.” | 과거 상담의 배분을 찾아 현재 유효한 증서를 확인하고 제안한다. 근거가 부족하면 묻는다. | 기억에 대응이 빠져 되묻거나, 액수가 같은 두 증서를 추측해 잘못 배정한다. |
| 오늘도 원래 배분을 명시한다. | 오늘 받은 지시대로 배분한다. | 이미 제공된 정보를 다시 묻거나 불필요한 업무 조회를 한다. |
| 오늘 두 승객의 배분을 바꿔 명시한다. | 현재 지시에 맞춰 바뀐 배분을 제안한다. | 과거 승인을 그대로 적용해 잘못된 승객에게 청구한다. |

- **규모.** 원천 하나에서 각 작성 조건을 한 번 실행하고 그 저장소를 세 고객 요청에 각각 한 번씩 제공하면, 다섯 방식 × 두 지침으로 작성 10회·상담 30건이다. 작성 방식만 먼저 탐색하는 축소안은 기본 지침 다섯 개로 작성 5회·상담 15건이다. 두 지침의 효과까지 평가할 본 비교에서는 열 조건을 미리 정하고 실행 순서를 섞는다. 상담 건수는 독립 고객 수나 모델 요청 수와 다르다.
- **읽기와 상담.** 각 2회차에는 해당 작성자가 남긴 저장소 하나를 시작할 때 같은 방식으로 제공한다. 동일 모델·현재 지갑·예약·고객 응답 및 승인 규칙·비용 한도를 유지한다. 매 배정은 두 증서가 모두 유효한 새 DB 사본에서 시작한다. 여러 종류를 담은 저장소에서 무엇을 자율 조회하는지는 후속 실험에서 같은 저장소 버전과 균형 잡힌 표시 순서로 비교한다.
- **판정.** 주 지표는 전체 상담의 공식 성공이다. 조건·성적을 가린 판독자가 실제 메모 문장을 원문과 대조해 사실·절차·교훈·사건 경과를 복수로 표시한다. 네 범주 밖의 내용은 해당 문장을 인용해 별도로 기술한다. 형식별 저장률, 승객과 증서의 연결, 당시 승인의 범위, 현재 재확인 조건을 기록하고 각 문장이 발권 전 입력에 들어갔는지 확인한다. 첫 제안과 고객의 정정·승인, 실제 발권 DB, 최종 성패를 연결한다. 되물음·업무 호출·토큰·작성 및 상담 비용은 전체 배정과 양쪽 모두 성공한 짝에서 각각 보고한다. 23번에는 결제는 맞고 수하물만 틀려 최종 실패한 기록이 있어 단계별 행동과 전체 성패를 함께 보고한다. 특정 메모 문장이 행동을 바꿨다는 가설은 메모에 노출되기 전의 동일한 판단 상태에서 그 문장만 변경해 검증할 수 있다. [공식 점수와 수하물 오류](../reports/20261118-two-pass-memory-forms-final-results.md) · [문장과 득실의 근거 기준](metrics.md#메모-문장과-득실을-연결하는-근거)
- **기준선과 확인 평가.** 보관된 1회차는 원래와 동일한 전체 과제의 회차 간 변화에 쓴다. 고객 대본이나 배분이 바뀐 과제에서는 같은 2회차 조건끼리 짝짓는다. 새 2회차 무기억은 0건이다. 미저장·혼합 작성도 원래 배정에 포함하고, 실제로 쓴 내용별 분석은 행동을 설명하는 자료로 사용한다. 23번에서 원천·작성·노출·행동·성패를 연결해 다음 지침을 개선한다. 확인 평가에서는 개발에 쓰지 않은 고객과 원천을 사용하고 승객·지갑의 표시 순서와 배분 방향을 균형 있게 배정한다. 정책 채택 기준은 성공률, 잘못된 배분의 증가, 비용을 바탕으로 실행 전에 정한다.

첫 개발 실행은 기본 지침 다섯 개로 작성 5회와 상담 15건을 배정한다. 세 대본 모두 같은 네이티브 과제 ID, 상담원 정책, 업무 도구, 초기 DB를 사용한다. ‘오늘도 동일’은 보관된 원래 과제와 완전히 같다. ‘지난번처럼’의 고객은 먼저 예전 배분을 지칭하며, 상담원이 구체적인 배분을 묻거나 잘못 제안하면 이전 값을 알려 줄 수 있다. ‘오늘은 변경’은 원래 대본의 두 수혜자와 참조 발권의 증서만 맞바꾼다. 원래 배분과 바꾼 배분을 세 대본 모두에서 별도로 채점해 여섯 점수를 검산한다. [고정 프로토콜과 대본](../../protocols/pilots/tau2-airline-allocation-forms-v1.json) · [실행기](../../scripts/run_airline_allocation.py)

저장소 루트에서 다음 명령으로 작성 입력 다섯 개, 상담 15건의 배정·실제 고객 지침·DB·채점 대조를 오프라인에서 재구성한다. 유료 실행은 깨끗한 구현 체크아웃에서 만든 `--freeze` 결과와 이번 실행의 명시적 총한도를 받는다. 등록된 공급자 오류의 미확인 요금은 고정 단가와 전송 요청으로 계산한 최대액을 원장에 보류하고 이미 시도한 배정은 반복하지 않는다. 판독 도구의 `writers` 및 `dialogue` 출력은 조건·성적을 가린다. `results`는 작성 저장소와 실제 상담원 입력을 대조하고 공식 점수·최종 DB를 엄격 재생한다. [고정 실행 배정·채점 표](../../artifacts/pilots/20261124-airline-allocation-forms-preflight.json) · [판독과 재채점 도구](../../scripts/analyze_airline_allocation.py)

```sh
uv run python -m scripts.run_airline_allocation --preflight
```

기본·보완 지침을 모두 교차한 작성 10회·상담 30건과 새 확인 고객은 후속 설계다. 두 지침을 짝지어 원래 과제에서 작성 입력을 준비한 이전 오프라인 점검은 아래에 보존한다. [기존 두 조건 개발 초안](../../protocols/drafts/tau2-airline-payment-memory-v2.json)

다음 개발 사례인 21번에서는 **결제 기준**을 기억하되 현재 잔액을 확인해 다른 카드를 고를 수 있어야 한다. 당시 두 상담에서 카드 거절 뒤 고객 지시가 서로 달랐으므로, 각 작성 조건에 같은 공개 지시와 지갑을 준다. [기존 결제 행동과 DB 재생](../reports/20261118-two-pass-memory-success-factors.md)

<details>
<summary>23번 원천으로 다섯 작성 방식의 기본·보완 입력 점검</summary>

아래 코드는 고정 원천과 네 형식의 지침을 읽고 자유 구성 지침을 추가한다. 각 쌍에서 보완 조건만 같은 한 문장을 더 받는다. v2의 사전 점검 함수가 원래 과제의 작성자 입력을 구성하고 2회차 상담 배정을 검산한다. 새 모델 요청은 0건이다.

```bash
uv run --offline --frozen --no-sync python - <<'PY'
import json
from pathlib import Path

from agent_memory_eval.experiments.forms_batch import prepare
from agent_memory_eval.experiments.prompt_comparison import compile_config
from agent_memory_eval.recording.assets import asset_text

root = Path.cwd()
parent = json.loads((root / "protocols/drafts/tau2-airline-payment-memory-v2.json").read_bytes())
base = json.loads(asset_text(root, parent["base_design_ref"]))
forms = {form: base["writing"]["lines"][form] for form in ("fact", "episode", "procedure", "lesson")}
forms["free"] = (
    "If you write, choose how to organize whatever may help with later work. "
    "You may combine different kinds of observations in one note or keep them in separate notes."
)
scope = (
    "If you retain a choice from the experience, preserve the link between its target and the "
    "selected option or value, and the scope of any permission, as supported by the public record."
)
source, = [row for row in base["source"]["cohort"] if row["task_id"] == "23"]
writers = set()
for form, line in forms.items():
    conditions = [dict(id=f"{form}_{level}", representation=form, prompt_version=f"{form}-{level}-v1",
                       prompt=prompt) for level, prompt in (("base", line), ("scope", line + " " + scope))]
    config = dict(parent, name=f"payment-allocation-{form}", status="draft", guards=None, conditions=conditions,
                  task_sets={"development": ["23"], "evaluation": []},
                  selection_reason="Payment allocation on one previously inspected development task.")
    design = compile_config(root, config)
    plan = prepare(root, root, root / "data/tau2-bench", design)
    assert plan["sources"]["23"].public_context_sha256 == source["public_context_sha256"]
    assert design["writing"]["proposed_limits"] == base["writing"]["proposed_limits"]
    assert design["second_pass"]["read_mode"] == "preload" and not design["second_pass"]["matched_controls"]
    for phase in ("write", "solve"):
        arms = [j["arm"] for j in plan["jobs"] if j["phase"] == phase]
        assert sorted(arms) == sorted(c["id"] for c in conditions)
    assert plan["summary"]["writer_initial_wires"] == 2
    writers.update(job["arm"] for job in plan["jobs"] if job["phase"] == "write")
    print(form, "2 writer inputs / 2 original-task assignments")

targets = ("past_only", "explicit_same", "explicit_swapped")
planned = {(writer, target) for writer in writers for target in targets}
assert len(writers) == 10 and len(planned) == 30
assert sum(writer.endswith("_base") for writer, target in planned) == 15
print("planned three-request comparison:", len(writers), "writer assignments,", len(planned), "second-pass assignments")
PY
```

</details>

<details>
<summary>보관된 작성물과 네 채점기 대조를 재현하는 방법</summary>

저장소 루트, 고정된 τ² 설치, 로컬 원본 상담과 작성 기록을 사용한다. 다음 명령은 원본을 읽어 표를 출력하며 모델 요청은 0건이다.

```bash
uv run --offline --frozen --no-sync python - <<'PY'
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from loguru import logger
from agent_memory_eval.adapters.tau2 import native_modules, verify_checkout
from agent_memory_eval.contracts.core import digest
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, read_experiment

root = Path.cwd()
checkout = verify_checkout(root / "data/tau2-bench")
logger.disable("tau2")
native_modules(checkout)
from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage
from tau2.data_model.semantic_communication import CommunicateScoring
from tau2.data_model.simulation import SimulationRun
from tau2.data_model.tasks import Task
from tau2.domains.airline.environment import get_environment
from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
from tau2.multilingual.runtime import language_environment_kwargs
from tau2.runner.presets import reload_recorded_call

source = json.loads((root / "protocols/drafts/tau2-two-pass-memory-forms-v1.json").read_bytes())["source"]
baseline = root / source["root"]
summary = json.loads(asset_text(baseline, {"path": "baseline-summary.json", "sha256": source["summary_sha256"]}))
record, = [r for r in summary["runs"] if r["task_id"] == "23" and r["repetition"] == 1]
assert record["official_success"] and not summary["memory_enabled"]
sim = SimulationRun.model_validate(BlobStore(baseline).get(record["report_ref"]))
options = language_environment_kwargs(reload_recorded_call(sim.language_context, "airline")) or {}
tasks = json.loads((checkout / "data/tau2/domains/airline/tasks.json").read_bytes())
original, = [t for t in tasks if t["id"] == "23"]
actions = original["evaluation_criteria"]["actions"]
pair = [actions[i]["arguments"] for i in (2, 3)]
assert [a["passengers"][0]["first_name"] for a in pair] == ["Raj", "Liam"]
vouchers = [a["payment_methods"][0] for a in pair]
assert all(v["amount"] == 250 and v["payment_id"].startswith("certificate_") for v in vouchers)
a, b = [v["payment_id"] for v in vouchers]
assert a != b
booked = [c.arguments["payment_methods"] for m in sim.messages for c in (getattr(m, "tool_calls", None) or [])
          if c.name == "book_reservation"]
assert booked == [step["arguments"]["payment_methods"] for step in actions if step["name"] == "book_reservation"]
db = get_environment(**options).tools.db
owner = pair[0]["user_id"]
assert all(db.users[owner].payment_methods[k].amount == 250 for k in (a, b))
previous = [r for r in db.reservations.values() if r.user_id == owner]
assert len(previous) == 1 and all(p.payment_id not in (a, b) for p in previous[0].payment_history)

writers = root / "runs/tau2-two-pass-forms-luna-deepseek-01"
seal = json.loads((writers / "completion.json").read_bytes())
jobs = json.loads(asset_text(writers, {"path": "allocations.json", "sha256": seal["allocation_sha256"]}))
counts = []
for job in jobs:
    if job["task_id"] != "23" or job["phase"] != "write" or job["arm"] not in ("fact", "episode", "lesson", "procedure"):
        continue
    folder = writers / job["directory"]
    assert digest((folder / "log.jsonl").read_bytes()) == job["result"]["log_sha256"]
    _, state, _ = read_experiment(folder)
    items = snapshot_items(BlobStore(folder), state, job["result"]["snapshot_id"])
    body = "\n".join(item["title"] + "\n" + item["body"] for item in items).lower()
    counts.append((job["arm"], bool(items), any(key in body for key in ("raj", "liam", a[-4:], b[-4:]))))
for form in ("fact", "episode", "procedure", "lesson"):
    rows = [(saved, hit) for arm, saved, hit in counts if arm == form]
    print(form, "writing assignments, nonempty, containing a name or suffix:",
          len(rows), sum(saved for saved, hit in rows), sum(hit for saved, hit in rows))

def allocation(swapped):
    steps = deepcopy(actions)
    if swapped:
        steps[2]["arguments"]["payment_methods"][0]["payment_id"] = b
        steps[3]["arguments"]["payment_methods"][0]["payment_id"] = a
    return steps

def grade(task, messages):
    return evaluate_simulation(simulation=sim.model_copy(update={"messages": messages, "reward_info": None}),
        task=Task.model_validate(task), evaluation_type=EvaluationType.ALL, domain="airline", solo_mode=False,
        env_kwargs=options, strict_replay=True, customer_language=sim.language_context.provider_language,
        communicate_scoring=CommunicateScoring(scorer="substring"))

with patch("socket.socket.connect", side_effect=AssertionError("offline")), patch("socket.socket.connect_ex", side_effect=AssertionError("offline")):
    assert grade(original, sim.messages).reward == sim.reward_info.reward == 1
    for expected_swap in (False, True):
        task = deepcopy(original)
        task["evaluation_criteria"]["actions"] = allocation(expected_swap)
        prior = f"{a} for Raj, and the other certificate for Liam"
        request = f"{a} for Liam, and the other certificate for Raj" if expected_swap else prior
        scenario = task["user_scenario"]["instructions"]
        assert prior in scenario["task_instructions"]
        scenario["task_instructions"] = scenario["task_instructions"].replace(prior, request)
        for actual_swap in (False, True):
            env = get_environment(**options)
            messages = [UserMessage(role="user", content="I approve these reservations and the split with " + request + ".")]
            charged = 0
            for i, step in enumerate(allocation(actual_swap)):
                call = ToolCall(id=f"replay-{i}", name=step["name"], arguments=step["arguments"])
                response = env.get_response(call)
                assert not response.error
                messages.extend((AssistantMessage(role="assistant", tool_calls=[call]), response))
                if call.name == "book_reservation":
                    booking = env.tools.db.reservations[json.loads(response.content)["reservation_id"]]
                    charged += sum(p.amount for p in booking.payment_history if p.payment_id.startswith("credit_card_"))
            messages.append(AssistantMessage(role="assistant", content="Gift cards $327, certificates $1000, Mastercard $1286."))
            result = grade(task, messages)
            assert charged == 1286 and all(c.met for c in result.communicate_checks)
            assert result.db_check.db_match == (expected_swap == actual_swap)
            assert result.reward == float(expected_swap == actual_swap)
            print(expected_swap, actual_swap, result.reward, charged)
PY
```

</details>
