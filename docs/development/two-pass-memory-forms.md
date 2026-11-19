# 같은 과제의 두 회차와 기억 작성 형태 비교

[연구 설계](../research/two-pass-memory-forms-v1.md)의 584개 배정을 한 번씩 실행한다. 실행 프로필은 [동결 설정](../../protocols/pilots/tau2-two-pass-memory-forms-v1.json)에 있다. 원천은 보존한 항공 train 무기억 벤치의 첫 반복 28개다. 배정과 무기억 대조·빈 메모·네 작성 형태·네 진단 과제의 범위는 [설계 배정표](../../protocols/drafts/tau2-two-pass-memory-forms-v1.json)에 고정했다.

## 입력과 실행

작성기는 원본 로그·요약·채점 영수증을 대조한 뒤 마지막 작업자 요청의 정책, 작업자가 본 대화와 마지막 공개 응답으로 공개 경험을 복원한다. 분석용 1회차 점수는 운영 기록에 둔다. 매번 새 작성 세션에서 공통 지침 끝에 배정 문장 하나를 붙이고 피드백 없음과 저장 도구만 제공한다. [작성기](../../src/agent_memory_eval/experiments/forms_write.py)

컨트롤러는 232개 작성을 끝낸 다음 352개 2회차 배정을 순서대로 처리한다. 각 배정의 별도 v1.2 journal과 전체 배정 목록을 보존한다. 작성 실패가 발생하면 해당 은행을 필요로 하는 배정은 미시작으로 기록한다. 빈 완료는 빈 은행으로 2회차에 진입한다. 각 solver는 봉인된 은행의 원본 로그 해시와 과제·반복·작성자 ID를 대조한다. [컨트롤러](../../src/agent_memory_eval/experiments/forms_batch.py)

매회 새 native 사용자·작업자·DB에서 시작한다. 과거 메모 안내와 JSON은 작업자의 모델 요청에서만 구성한다. 사용자와의 공개 업무 대화나 도구 이력에 과거 대화를 삽입하지 않는다. 정상 종료에서는 고정 τ²의 DB·COMMUNICATE/substring 결과를 보존한다. 조기 종료가 만든 0은 raw native 결과에 두고 공식 성공과 실패에서는 미채점으로 남긴다. [2회차 실행기](../../src/agent_memory_eval/experiments/forms_solve.py)

## 검증과 실행 기록

실행 전 원천·순서·옛 대화 포함 입력 크기를 다시 계산한다. 이 명령은 로컬 데이터와 잠긴 τ² 설치본을 사용한다.

```bash
uv run --no-sync python scripts/run_two_pass_forms.py --preflight
```

실제 실행은 프로필에 명시한 구현 commit에서 실행한다. `--workspace`는 데이터 및 원본 보관소의 실제 루트다. 지정된 출력 디렉터리는 새로 생성하며, 송신 전 모든 작업을 배정하고 수신 후 각 작업의 상태와 청구액을 기록한다. 예산은 배치 전체에서 공유한다. 미확인 청구가 발생하면 이후 송신을 멈춘다. [검증 기록](../../artifacts/analyses/20261118-two-pass-forms-preflight.json) · [모델 경로와 단가](../../artifacts/pilots/20261118-two-pass-forms-endpoints.json)

```bash
PYTHONPATH="$PWD/src" /home/depa/agent-memory/.venv/bin/python scripts/run_two_pass_forms.py --workspace /home/depa/agent-memory --live /home/depa/agent-memory/protocols/pilots/tau2-two-pass-memory-forms-v1.json
```

완결된 journal은 외부 모델을 호출하지 않고 다시 읽는다. `completion.json`이 전체 배정 목록과 설정의 해시를 고정하고, 배정별 해시는 해당 원본 journal을 고정한다. 감사기는 기록된 요청·비용·입력 노출, 공급된 은행, native 공식 점수와 모든 계획 쌍의 분모를 대조한다. [분석 코드](../../src/agent_memory_eval/analysis/forms_results.py)

```bash
uv run --no-sync python scripts/run_two_pass_forms.py --audit runs/tau2-two-pass-forms-luna-deepseek-01
```


## 원본 고정 commit 재현

[전용 registry](../../protocols/reproductions/two-pass-forms-20261118.json)는 실행 `7e30fa24`와 분석 `3ae7211b`를 구분한다. 각 배정의 journal과 공식 점수를 검토한 [재현 receipt](../../artifacts/reproductions/20261118/two-pass-forms-01.json)에 원본·사본·코드 해시 및 금지 I/O 검사가 있다. 재현 출력은 새 디렉터리로 지정한다.

```bash
uv run --no-sync python scripts/reproduce_experiment.py --registry protocols/reproductions/two-pass-forms-20261118.json --experiment two_pass_forms --inputs-root . --run --output outputs/reproductions/two-pass-forms-another-check
```

## 보충 실행

[보충 프로필](../../protocols/pilots/tau2-two-pass-memory-forms-supplement-v1.json)과 [사전 검증](../../artifacts/analyses/20261118-two-pass-forms-supplement-preflight.json)은 실제 48개 검사와 원본 비용·은행 해시를 고정한다. 원본의 미시작 행 277∼584만 원래 배정·run_id·시드·자기 작성 은행으로 수행한다. 대상 308개 중 203개는 기존 writer의 은행을 사용하며, 나머지는 원문 또는 대조다. 시작 전 원본 종료 기록·전체 배정·기존 로그·모든 필요한 은행을 대조한다. 독립 디렉터리 `runs/tau2-two-pass-forms-luna-deepseek-supplement-01`에 먼저 배정과 시작을 적는다. 기존 디렉터리가 있으면 새로운 실행을 시작할 수 없다. 원본의 알려진 비용과 요청 수, 429 비용 예약액 및 최초 시각을 이월한다. 보충에서 청구 미확인인 HTTP 429는 해당 전송의 원본 요청에서 예약액을 계산해 남기고 다음 배정으로 넘어간다. 공급자 대기 지시와 최소 60초 중 더 긴 시간을 적용하며, 다른 미확인 전송은 배치를 종료한다. [보충 컨트롤러](../../src/agent_memory_eval/experiments/forms_supplement.py)

```bash
uv run --no-sync python scripts/run_two_pass_forms_supplement.py --preflight
```

합산 검증은 두 원본의 종료 해시를 각각 대조한 뒤 격리된 분석 사본에서 진행한다. 채점 자료의 사본, 요청에 실제 포함된 기억과 역할별 원가, 공식 점수를 확인한다. 별도로 `regrade_two_pass_forms.py`는 모델 접속을 차단한 상태에서 τ²의 실제 업무 도구와 DB를 다시 구성하고 공식 평가 전체를 수행한다. 공개 대화, 결제 및 과거 예약 번호의 경로는 23·42번을 대상으로 검사한다.

```bash
uv run --no-sync python scripts/audit_two_pass_forms_supplement.py --output outputs/two-pass-forms-combined-audit-01
uv run --no-sync python scripts/regrade_two_pass_forms.py outputs/two-pass-forms-combined-audit-01/combined-view outputs/two-pass-forms-regraded-01.json
```

보충 종료 뒤 원본과 합쳐 66개의 공식 점수를 모두 엄격 재채점했다. 배정·비용·미채점의 분리와 504 개별 과금 조회는 [실제 합산 보고서](../reports/20261118-two-pass-memory-forms-combined-results.md)를 따른다.

HTTP 오류 응답에서 수치형 과금·토큰·생성 ID·Retry-After를 보존하는 전송 변경이 보충 commit `5d3895d`에 포함됐다. 이후 독립 검토에서 HTTP 본문이 잘리는 별도 예외 경로를 찾았다. 현재 전송 코드는 429 헤더를 받은 뒤 본문 읽기에 실패해도 429 상태와 대기 지시를 남기며, 정상 헤더 후 본문 오류와 전송 여부 자체가 불명확한 오류를 각각 기록한다. 이 경로는 모의 HTTP 검사로 검증했다. 실제 36번 빈 메모의 오류는 생성 ID가 포함된 JSON 504로 기록됐다. [전송 구현](../../src/agent_memory_eval/models/openrouter.py) · [회귀 검사](../../tests/test_openrouter.py)

두 원본·고정 τ² checkout으로 66개 공식 점수의 도구·DB·전달 재채점을 재현한 [registry](../../protocols/reproductions/two-pass-forms-combined-20261118.json)와 [실제 실행 증거](../../artifacts/reproductions/20261118/two-pass-forms-combined-01.json)가 있다.

두 번째 보충은 고정 commit `42025693`에서 303∼584번의 미시작 282개에만 진입한다. 186개 기존 작성 은행과 원래 시드·DB·업무 인터페이스로 연결한다. 확인된 HTTP 429와 수신된 생성 ID 동반 504에 한해 미확인 청구의 원래 요청 예약액을 보존한다. 기존 청구 미확인 2건의 예약 합계는 $0.0100881이고 새 송신 전까지 누적 1,861요청이다. 사전 native 모의 전송에서 504를 반환한 첫 배정의 미채점을 보존하고 다음 배정이 공식 채점되는 경로, 모호하거나 ID가 없는 오류의 종료를 검증했다. 57개 고정 검사에 생략은 없었다. [두 번째 보충 설정](../../protocols/pilots/tau2-two-pass-memory-forms-followon-v1.json) · [사전 검사](../../artifacts/analyses/20261118-two-pass-forms-followon-preflight.json)


두 번째 보충은 169개를 시도해 168개를 채점했고 471번의 미확인 HTTP 502에서 봉인했다. 오프라인 감사는 세 원본의 종료 해시, 원래 배정 순서, 비용과 예외 자격, 실제 기억 노출을 검사해 고유 배정 352개를 복원한다. 234개의 공식 점수를 모두 엄격 재채점했고, 42개 가림 행동 비교와 두 지정 과제의 결제·예약·보상권 행동을 연결했다. [세 원본 합산 보고서](../reports/20261118-two-pass-memory-forms-final-results.md) · [독립 감사](../../scripts/audit_two_pass_forms_followon.py) · [고정 commit 재현 registry](../../protocols/reproductions/two-pass-forms-final-20261118.json) · [실제 재현 영수증](../../artifacts/reproductions/20261118/two-pass-forms-final-01.json)
