# 여러 벤치에서 기억 실험을 같은 기준으로 평가할 수 있는가

2026-11-23 · 보관 실행과 공식 채점기를 이용한 오프라인 점검

**현재 같은 규칙으로 확인할 수 있는 것은 요청에 들어간 기억, 공식 점수의 출처, 짝지은 조건의 개선·악화·결측이다.** 실제 모델의 1회차 경험부터 2회차 결과까지 연결한 환경은 τ²다. 소매와 항공은 그 안의 두 도메인이다. AppWorld와 BFCL에서는 독립된 공식 채점기로 대조값을 확인했다. 두 벤치 모두 공통 실행기로 실험 전 과정을 반복할 수 있는 단계는 남아 있다. 새 모델 요청은 0건이다. [고정 입력과 버전](../../protocols/diagnostics/benchmark-portability-v1.json) · [기계 판독 결과](../../artifacts/analyses/20261123-framework-portability.json)

| 환경 | 직접 확인한 범위 | 기억 실험에서 아직 검증할 범위 |
| --- | --- | --- |
| τ² 소매·항공 | 종료 기록 5개의 실제 요청 81건에서 요청 원문과 기억 노출 기록을 대조했다. 전체 상담 4건의 기록 점수는 각각 원본 에피소드의 보상과 일치했다. | 첫 판단만 측정한 1건과 공급자 오류로 중단된 1건에는 공식 성공 값이 없다. 장애·재개와 미시작 배정까지 포함한 공통 실행기는 남아 있다. |
| AppWorld | 학습 과제 3개의 공식 채점기를 초기 DB → 보관 완료 DB → 초기 DB 순서로 실행했다. 세 과제 모두 원래 초기 요구사항별 판정으로 돌아왔다. | 실제 모델의 코드 실행, 기억 전달과 다음 요청, Python 변수·대화까지 포함하는 세션 격리는 여기서 측정하지 않았다. |
| BFCL V4 Memory | 155문항 모두에서 공식 채점기가 기준 답을 통과시키고 무관한 답을 탈락시켰다. 공식 요약 메모리 백엔드에서 저장 후 새 인스턴스 두 개가 같은 값을 읽었다. | 현 공통 라우터는 기억을 허용한 조건에서도 고유 쓰기·읽기 두 호출을 모두 거절했다. 실제 모델의 선행 대화부터 질문 답변까지 연결한 관측은 없다. |

## 같은 기준으로 비교할 수 있는 것

τ² 소매에서는 같은 조건부 조회 안내 아래 실제·빈 메모의 **목표 주문을 첫 행동에서 조회했는지**를 154쌍 모두에서 판정했다. 실제 메모에서만 맞힌 75쌍과 빈 메모에서만 맞힌 3쌍이 있었다. 항공에서는 작성 형식과 무노트에 배정된 224쌍의 **전체 과제 공식 성공**을 보았다. 양쪽 점수가 있는 149쌍에서 개선 18쌍·악화 16쌍이었고 75쌍은 결측이었다. 같은 함수가 두 비교의 분모와 방향을 보존한다. 지표, 대조 조건, 표본이 달라 두 수치를 하나의 기억 효과로 합칠 수 없다. [기억 문장의 작성 근거와 배정별 비교](20261123-memory-claim-evidence.md)

소매의 ‘목표 주문만 조회’와 항공의 ‘공식 성공’은 기존 실험별 분석기가 판정한다. 공통 코드가 받는 입력은 어느 조건과 짝지었는지, 그 지표가 참·거짓·미관측 중 어느 값인지다. 새 벤치에서도 기억이 쓰일 판단과 관찰 가능한 행동을 먼저 정의해야 같은 비교를 적용할 수 있다. [지표별 입력을 구성하는 코드](../../scripts/analyze_memory_claims.py) · [공통 짝비교 함수](../../src/agent_memory_eval/analysis/claim_evidence.py)

원기록 여섯 개에는 공식 성공 2건, 실패 2건, 첫 판단까지 정상 종료해 최종 채점이 없는 1건, HTTP 520으로 열린 채 끝난 1건이 있다. 중단 기록에는 미확정 비용 요청 1건이 남고, 공통 요청 감사기는 해당 기록의 완료 인증을 거부한다. 미채점·실패·비용 미확정을 구별해야 반복할수록 성공률 분모나 비용이 달라지는 원인을 찾을 수 있다. [요청 감사기](../../src/agent_memory_eval/analysis/requests.py) · [표본별 판정](../../artifacts/analyses/20261123-framework-portability.json)

## 다른 벤치에서 드러난 차이

**AppWorld.** 선택한 학습 과제의 요구사항은 각각 6개·6개·9개다. 초기 DB에서 충족한 것은 1개·1개·1개였고, 완료 DB에서는 6개·6개·9개였다. 초기부터 참인 3개는 공식 라벨이 모두 `no_op_pass`였다. 이를 에이전트가 이룬 진척으로 세면 어떤 행동도 하기 전에 세 과제의 점수가 오른다. 원래 요구사항, 보존 조건, 과제 전체 성공을 함께 남겨야 이 차이를 찾을 수 있다. 공개 결과에는 요건별 판정의 해시를 남겼고 원본 DB와 채점 자료는 모델에 보내지 않았다. [공식 AppWorld 채점기](https://github.com/StonyBrookNLP/appworld/blob/42b5bcf3cd334fee33f0c37c02070a9f5807add5/src/appworld/evaluator.py)

**BFCL.** 기준 답과 무관한 답을 직접 넣은 310판정은 채점기 대조다. `rec_sum` 복원에서는 같은 snapshot을 두 번 열고 첫 번째 사본의 메모를 메모리 안에서 지운 후에도 두 번째 사본에서 원래 값을 읽었다. 공식 선행 대화 절차 전체와 다른 백엔드는 이번 점검 범위 밖이다. [BFCL 공식 기억 평가 절차](https://gorilla.cs.berkeley.edu/blogs/16_bfcl_v4_memory.html) · [고정한 공식 채점기](https://github.com/ShishirPatil/gorilla/blob/6ea57973c7a6097fd7c5915698c54c17c5b1b6c8/berkeley-function-call-leaderboard/bfcl_eval/eval_checker/agentic_eval/agentic_checker.py)

공통 라우터의 권한 검사를 보려고 두 조건 모두에서 같은 모의 작업자가 `memory_append`와 `memory_retrieve`를 호출하게 했다. 무기억 조건과 기억 허용 조건 모두 두 호출에 `POLICY_DENIED`를 반환했고, 공식 백엔드에 도달한 호출은 0건이었다. 기본 라우터가 `memory_`로 시작하는 이름을 자체 세 도구 목록으로 판정하기 때문이다. 두 조건의 모의 요청 6건은 요청 감사기를 통과했다. 앞선 호출의 인수에 표식 문자열이 있어도 조회 응답이 다음 판단 입력의 기억으로 기록돼야 전달 성공으로 인정했다. [공통 라우터](../../src/agent_memory_eval/memory/router.py) · [능력 검증 코드](../../scripts/audit_benchmark_portability.py)

## 다음 벤치에 적용할 판정 기준

| 확인할 질문 | 반복해서 남길 증거 |
| --- | --- |
| 같은 과제를 같은 의미로 채점했는가? | 벤치·과제·채점기 버전과 원본 점수, 알려진 성공·실패 대조, 요구사항별 결과, 첫 판단/전체 과제의 측정 단위, 미채점 사유 |
| 작성할 때 무엇을 알 수 있었고 무엇이 실제로 전달됐는가? | 작성자가 본 공개 경험의 위치, 저장 문장과 버전, 조회 반환, 행동을 생성한 요청에서의 제목·본문 위치. 독립적인 문장 판독과 대조 개입 여부도 명시한다. |
| 정책에 따라 행동과 결과가 달라졌는가? | 같은 초기 상태의 실제 메모·빈 메모·무기억 배정, 새 세션, 실제 업무 행동, 요구사항·최종 결과, 비용, 개선·악화·결측. 문장 하나의 효과를 묻는 경우에는 그 문장만 바꾼 대조가 필요하다. |
| 중단되거나 벤치가 바뀌어도 같은 판정을 재현할 수 있는가? | 지원하는 초기화·snapshot·재생 범위, 격리 검증, 미시작·중단·완료 배정, 원본 입력과 결과 해시, 고정 재현 명령 |

다음 구현 순서는 기존 소매 대표 결과 네 개를 고정 재현 목록에 등록하고, 두 번째 환경에서 쓰기 한 번·읽기 한 번을 끝까지 검증하는 것이다. 이번 점검에서는 BFCL이 그 경계를 가장 작게 드러냈다. 허용 조건에서 고유 연산·버전·반환을 다음 입력까지 추적하고, 무기억 대조에서는 같은 연산이 차단되며, 공식 채점 대조가 그대로 재현되는지를 함께 보면 된다. 공통 실행기의 시작점은 아직 `FakeBenchmark`와 `FakeWorker`를 직접 생성하고, 실제 τ² 실험은 별도 실행기를 쓴다. 두 번째 환경 전체 연결과 실행기의 일반화는 계속 별개로 확인한다. [실행기 현재 구현](../../src/agent_memory_eval/runner/offline.py) · [어댑터 설계](../design/architecture.md) · [단계별 계획](../development/framework-scoring-plan.md)

## 다시 실행하는 방법

저장된 수치·분모와 검증 규칙은 아래 테스트로 확인할 수 있다.

```bash
uv run --frozen --no-sync python -m unittest discover -s tests -p 'test_benchmark_portability.py'
```

공식 채점기를 다시 돌리려면 고정한 BFCL 체크아웃, AppWorld의 해당 버전 전용 Python과 세 과제의 초기·보관 완료 DB, 그리고 τ² 원실험의 로컬 실행 기록이 필요하다. 프로필에 각 입력의 해시를 기록했다. BFCL 체크아웃은 아래 로컬 경로에 준비돼 있으며 다른 머신에서는 공식 저장소의 `6ea57973c7a6097fd7c5915698c54c17c5b1b6c8` 체크아웃을 지정할 수 있다. AppWorld의 원자료는 임시 사본에서만 열고, 결과 파일은 새 경로에 쓴다. [입력별 해시](../../protocols/diagnostics/benchmark-portability-v1.json) · [점검 실행기](../../scripts/audit_benchmark_portability.py)

```bash
check_dir=$(mktemp -d)
uv run --frozen --no-sync --with overrides==7.7.0 --with filelock==3.20.0 \
  python -B -m scripts.audit_benchmark_portability \
  --bfcl-checkout .local/benchmark-portability/bfcl-6ea5797 \
  --appworld-runtime /home/depa/ai-labs/.local/runs/appworld-relations-0006-replay-d/runtime \
  --appworld-python /home/depa/ai-labs/.local/venvs/appworld/bin/python \
  --output "$check_dir/report.json"
cmp "$check_dir/report.json" artifacts/analyses/20261123-framework-portability.json
```

11월 23일 점검에서는 엄격 검사에서 무기억 차단만 충족해 종료값 1을 받았다. 기본 명령은 당시 기록을 재현한다. 11월 25일 버전에서 새 출력 경로와 `--require-bfcl-native-routing`을 지정하면 명시적으로 연결한 BFCL 고유 백엔드의 양쪽 대조까지 실행하고 v2 결과를 저장한다. [고유 도구의 실제 반환·다음 입력·공식 채점 검증](20261125-common-policy-measurements.md)
