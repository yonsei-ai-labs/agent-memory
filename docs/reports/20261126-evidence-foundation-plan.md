# 판단 근거 수집과 개선 기록의 기반

2026-11-26 · 은행과 BFCL에서 같은 기억 정책을 비교하기 위한 개선 노트와 검증 결과

현재 작업 세션이 탐구를 맡는다. 한 라운드에서 쓴 공개 근거, 바꾼 정책, 예측, 실제 결과와 결정을 보관하면 다음 세션은 그 판단부터 이어갈 수 있다. 이번 구현 범위는 과제 종료 후 필수 작성과 새 세션에서의 시작 제공·자율 조회다. [연구 목표](../research/plan.md#현재-방향) · [탐구 에이전트의 역할](20261126-agent-explorer-direction.md)

## 한 정책으로 두 벤치를 실행하기

한 선언에 작성 지침, 다섯 기억 형식, 기존 지침에 더할 후보 문구, 작성자에게 주는 성패 피드백, 읽기 안내와 전달 방식을 둔다. 실행기는 두 벤치에 같은 형식별 작성 문구를 만들고 등록한다. 벤치마다 업무 입력, 사용자 시뮬레이션, 모델, 도구와 공식 채점기는 원래 경로를 쓴다. 전달 방식은 라운드 전체에서 시작 제공 또는 자율 조회 하나로 고정한다. 같은 1회차 공개 경험에서 여러 기억을 쓰고, 저장된 각 기억을 독립된 2회차에서 평가한다. 회복·악화의 대조는 해당 과제의 무기억 1회차다. [공용 실행기](../../src/agent_memory_eval/runner/policy_round.py) · [개발용 선언 예시](../../protocols/diagnostics/portable-writer-round-v1.json)

```bash
plan=protocols/diagnostics/portable-writer-round-v1.json
uv run --offline python -m agent_memory_eval.runner.policy_round prepare --plan "$plan"
```

`prepare`는 파일을 읽고 양쪽의 정책 해시와 배정을 계산한다. `register`는 같은 인수로 설계, 두 벤치의 실행 설정과 판정 기준을 새 라운드 폴더에 고정한다. `preflight --adapter banking-opening`은 은행의 원래 배정과 비용 승인에 쓸 파일을 생성한다. BFCL의 `preflight --adapter bfcl-relevance --bfcl-checkout <경로>`는 고정 벤치 데이터를 검증한다. `run`도 같은 어댑터를 지정한다. 은행은 해당 배정에 대한 기존 실행기의 승인 검사를 거치고, BFCL은 `--model-python`과 `--checkpoint`로 지정한 로컬 모델을 사용한다. [선언 스키마](../../schemas/policy-round-v1.schema.json)

확인 실행 전에는 가설·예측·판정 기준과 정책 버전, 근거 자료의 해시 및 과제를 함께 고정한다. 은행은 고객, BFCL은 등록한 질문 계열을 기준으로 개발·확인 겹침을 검사한다. 개발에 사용한 자료와 파생 질문의 계열을 선언에 정확히 기록해야 한다. 앞선 결정 파일을 다음 선언에 연결하면 그 결정에서 본 과제도 개발 자료에 포함된다. `report`는 두 원장의 등록과 실제 작성 입력·전달을 검산한다. `decide`는 판독 결과의 해시, 채택·보류·기각과 연구자의 이유를 남긴다. 설명형 판정 기준의 과학적 타당성은 연구자가 판단한다. 예시는 이미 살펴본 자료를 쓰는 개발 라운드다. [개발·확인 기준](../development/memory-policy-improvement.md#정책을-채택하는-기준)

## 전체 배정에서 근거 읽기

공용 판독 명령의 `--policy-evidence`는 모든 1회차와 2회차 배정을 읽는다. 각 행에서 첫 공개 판단이 함수 호출인지, 호출 없는 응답인지, 응답을 관측할 수 없었는지와 공식 성패를 함께 본다. 미시작과 미채점은 원래 사유를 유지한다. 상담 전체의 도구 호출·토큰은 양쪽 모두 성공한 짝에서 비교한다. 토큰 영수증이 일부 누락된 실행은 완전한 사용량 비교에서 빠진다. 작성 비용은 같은 기억을 쓴 후속 상담 수만큼 중복해서 더하지 않는다. 은행에서는 확인 비용과 미확정 보류액을 포함한 총한도 사용액을 각각 표시한다. [결과 스키마](../../schemas/policy-evidence-v1.schema.json) · [전체 판독 코드](../../src/agent_memory_eval/analysis/policy_evidence.py)

- 후보는 1회차 공개 업무 기록에서 먼저 수집한다. 공용 규칙은 함수명, 문자열 인수와 그 안의 식별자, 공개 문장에 나온 복합 표기 단서를 읽는다. 첫 공개 행동에서 새로 나온 후보도 보존한다. 공개된 고객·작업자·업무 도구의 내용이 대상이다. 채점 정답과 전송용 호출 ID는 후보에 들어가지 않는다.
- 작성 요청에서는 실제 입력과 출처 명세를 대조해 지침, 공개 경험, 성패 피드백을 찾는다. 작성별로 원천, 저장 버전, 실제 지침 해시를 남긴다. 판단별로 저장된 값, 실제로 노출된 제목·본문, 그 밖의 입력과 업무 행동의 표현을 찾아낸다. 같은 표현이 여러 위치에 있으면 각 위치에 표시한다. 현재 입력에는 작업자 자신의 앞선 발화도 포함된다. [작성 출처 판독](../../src/agent_memory_eval/analysis/decision_cues.py)
- 일치는 정확 일치, 대소문자·공백을 정리해야 일치, 검사한 필드에서 일치 없음, 미관측으로 기록한다. ID·함수명은 보수적으로 비교하고 구두점을 보존한다. 원천에 있어도 저장하지 않은 단서, 노출됐어도 행동에서 찾을 수 없는 단서도 집계한다. 단서는 한 판단에서 한 번씩 센다. 반복 상담과 여러 기억 형식은 같은 원천을 공유한다.
- 이 집계로 절차·교훈의 의미상 사용이나 기억의 인과 기여를 확정할 수는 없다. 설명이 필요한 판단은 인용 위치와 실제 요청·응답까지 돌아가 확인한다. 정책 성과는 벤치별 공식 성패, 진척, 회복·악화, 공동 성공 효율과 전체 비용을 함께 읽는다. [개별 판단 검산](../development/trajectory-outcomes.md#한-판단에서-단서의-출처를-검산하기)

## 검증 범위

| 보관 기록 | 2회차 배정 | 시작 | 공식 채점 | 첫 판단에서 함수 호출 없음 | 그중 공식 성공 |
| --- | ---: | ---: | ---: | ---: | ---: |
| τ³ 은행 계좌 개설 | 1,140 | 660 | 487 | 17 | 0 |
| BFCL 함수 관련성 | 80 | 80 | 80 | 32 | 30 |

은행의 무기억 1회차 19개, BFCL의 무기억 1회차 8개도 전체 결과에 들어간다. 은행의 두 작성 정책이 함께 채점된 210쌍은 모두 실패였다. BFCL은 같은 기억을 자동 제공하거나 자율 조회하게 한 40쌍 중 양쪽 성공 33쌍, 자율 조회에서만 성공 2쌍, 양쪽 실패 5쌍이었다. 과거 두 연구는 서로 다른 비교를 등록했다. 보관 표는 새 코드가 각 연구의 분모·근거·결과를 보존하는지 확인한 것이다. [두 원장의 재집계](../../artifacts/analyses/20261126-policy-evidence-summary.json)

은행에서 관측한 2회차 첫 판단 660건 전체를 대상으로 `EcoCard`라는 공개 상품명을 사후 조회했다. 그중 420건은 해당 판단에 연결된 1회차 공개 기록에 이 표기가 있었다. 여러 판단이 같은 1회차 기록을 공유한다. 그중 60건은 같은 표기가 저장된 기억, 작업자에게 노출된 기억, 첫 업무 검색 인수에 모두 있었고 그 시점의 다른 입력과 작성 지침에서는 발견되지 않았다. **60건 모두 058번 과제의 같은 고객**이었다. 최종 채점은 39건 실패, 21건 미채점이었다. 이 표기로 공개 단서가 검색까지 이어진 경로와 그 빈도는 설명할 수 있다. 상품명 전반의 효과를 추정하려면 독립된 고객과 상품을 더 관측해야 한다. 기존의 49/198은 같은 기억으로 세 번 반복했을 때 첫 검색의 필수 문서 적중 여부가 달라진 묶음 수다. [원래 사례·반복 정의·전체 원장 판독 명령](../development/trajectory-outcomes.md)

위 문서의 은행 판독 명령에 `--policy-evidence`를 붙여 전체 출력 파일을 만든 뒤, 같은 방식으로 다른 공개 표기도 조회할 수 있다.

```bash
jq --arg term EcoCard '
  [.observations[] | select(.phase == "solve" and .decision != null)] as $observed
  | [$observed[] | select(any(.cues[]; .term == $term and .source_candidate
      and .source == "exact" and .stored == "exact" and .exposed == "exact"
      and .action == "exact" and .current == "absent" and .guidance == "absent"))] as $hits
  | {observed: ($observed|length), matching: ($hits|length),
     customers: ($hits|map(.cluster)|unique|length), tasks: ($hits|map(.task)|unique|length),
     graded: ($hits|map(select(.official_success != null))|length),
     successful: ($hits|map(select(.official_success == true))|length)}' bank-evidence.json
```

공통 정책의 양쪽 실제 실행 경로는 외부 통신을 차단한 별도 시험에서 확인했다. 자동 제공과 자율 조회를 각각 시험했고, 각 방식에서 은행 1과제·BFCL 1질문으로 1회차 2건, 작성 20건, 2회차 20건을 실행했다. 자율 조회 시험에서는 별도 작업 폴더에 둔 작성·조회 지침을 두 벤치에 적용했다. 모의 작업자가 실제 요청에서 조회 안내를 확인하고 목록에 반환된 ID로 본문을 읽도록 했다. 실제 저장 버전, 다음 업무 입력의 노출, 공식 채점, 비용 원장과 결정 기록을 검증했다. 모의 응답의 성적과 조회율은 모델의 정책 성능 추정에서 제외한다. [재현 가능한 통합 시험](../../tests/replay_policy_round.py)

깨끗한 구현 체크아웃에서 아래 명령으로 두 전달 방식의 실행·판독을 다시 확인할 수 있다. 원래 은행 실행기가 구현 커밋을 검사한다. 출력은 매번 새 임시 폴더에 쓴다.

```bash
fixture_dir=$(mktemp -d)
for delivery in preload on_demand; do
  PYTHONPATH=.:tests TAU2_DATA_DIR="$PWD/data/tau2-bench/data" LITELLM_LOCAL_MODEL_COST_MAP=True \
    uv run --offline python tests/replay_policy_round.py --delivery "$delivery" \
    --bfcl-checkout .local/benchmark-portability/bfcl-6ea5797 --output "$fixture_dir/$delivery"
done
```

현재 두 실행기는 한 작성자가 한 원천을 받아 쓰는 조건을 검증했다. 여러 원천을 누적하는 작성, 의미가 같은 절차를 바꾸어 표현한 경우의 판독, 새 확인 집합에서의 실제 모델 효과는 후속 검증 범위다.
