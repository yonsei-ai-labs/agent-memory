# τ² 자율 조회 파일럿

2026-11-14 · 한 과제·고정 기억은행의 세 조건 반복 비교

Luna reasoning low 작업자와 DeepSeek V4.1 Flash 1017 사용자를 유지하고, 기억 조회를 지시하지 않는 비교를 연결했다. 모델/제공 경로·native 한도는 [기존 연결 설정](tau2-connection.md)을 따른다. 범용 연구 계획 실행기와 구분한 작은 탐색 파일럿이다.

## 비교와 고정 항목

| 조건 | 기억 도구/지침 | 시작 은행 |
| --- | --- | --- |
| no_memory | 없음 | 없음 |
| empty_bank | list/get, 사용 여부를 선택하는 지침 | 같은 빈 snapshot의 새 복제본 |
| memory_bank | 빈은행과 동일한 도구/지침 | 같은 고정 snapshot의 새 복제본 |

세 조건 모두 `Tau2MemoryRunner`·`Tau2MemoryBridge`와 같은 응답 직렬화 경로를 사용한다. 빈은행과 기억은행은 실제 전송 system·도구·파라미터를 대조한다. 무기억의 추가 기억 지침/도구, 평가 중 쓰기, 사용자에게 전달되는 내부 기억 왕복은 허용하지 않는다. 조회 결과의 제목·본문은 실제 다음 입력에서 구분해 센다.

과제는 기존 airline train `0`, 조건별 3회다. 순서 배정 seed `20261114`로 세 조건의 위치를 고르게 섞은 3블록을 실행한다. 배정 ID·순서·블록 내 비교 상대는 모델 호출 전에 확정한다.

1. empty_bank → memory_bank → no_memory
2. memory_bank → no_memory → empty_bank
3. no_memory → empty_bank → memory_bank

벤치/작업자 seed는 연결 설정의 0을 반복하며, 사용자 모델에는 미지원 seed를 보내지 않는다. 이는 같은 설정에서 관측한 반복이며 독립 난수 표본이나 호스팅 모델의 결정성을 보장하지 않는다. 모델 응답 캐시는 끄고 제공자의 prefix 캐시 사용량은 원 usage에 남긴다.

## 고정 기억의 출처

처음 [v1.0 파일럿](../../protocols/pilots/tau2-autonomous-memory-v1.json)은 원천 경험을 새로 수집하고 사후 작성 여부를 선택하게 했다. 실제 첫 시도에서는 memory_store 호출이 없었고 writer는 `{"message":"No memory was saved."}`라고 응답했다. 저장하지 않은 이유는 확인되지 않았다. 비어 있는 은행을 기억 조건으로 쓰지 않고 사전 배정한 9개 평가를 `source_empty`로 생략했다. 이 실행과 비용·원본은 보존한다.

현재 [v1.1 파일럿](../../protocols/pilots/tau2-autonomous-memory-v1.1.json)은 앞서 [guided 연결 확인](tau2-memory.md)에서 Luna가 작성한 **기억 1개**를 고정해 재사용한다. 저장 문구/개수의 효과는 이번 비교의 대상이 아니다. 해당 source는 기억 생성을 지시한 실행에서 나왔으므로 자율 작성 결과로 분류하지 않는다.

[가져오기 코드](../../src/agent_memory_eval/memory/imports.py)는 원본 로그 해시·실험/run/snapshot ID·봉인·생산 사건·공개 경험 접두부를 검증한다. 기존 본문·항목/버전·정렬을 바꾸지 않고 로컬 snapshot으로 옮긴다. 모델이 새로 저장한 것처럼 memory.stored 사건을 만들지 않는다.

원본 로그와 blob은 추적을 위한 내부 보관본으로 복사한다. 모델에는 현재 run의 도구가 반환한 선택 기억만 제공하며, 예전 대화·사용자 전용 정보·채점 자료를 붙이지 않는다. 모델 요청 출처와 은행 권한은 현재 run에서 다시 검증한다. 보관본 덕분에 원래 실행 폴더가 없어도 audit할 수 있다. 원천 경험/작성의 과거 비용과 새 9회 평가 비용은 별도로 보고한다.

## 실행과 준비 검사

```bash
uv run --extra tau2 python scripts/run_tau2_autonomy_pilot.py --output runs/autonomy-preflight
uv run --extra tau2 python scripts/run_tau2_autonomy_pilot.py --output runs/autonomy-live --live
uv run --extra tau2 python scripts/run_tau2_autonomy_pilot.py --output runs/autonomy-live --audit-only
```

출력은 새/빈 디렉터리만 허용한다. 고정 source의 raw 실행은 Git 밖에 있으므로 `--live` 전에는 프로필에 적힌 원본이 필요하다. 원본이 없거나 해시·출처가 다르면 준비 검사에서 거절한다. 기본 저장소 검증은 raw 원본 없이 스키마·지침 해시를 검사할 수 있고, native 단위 테스트는 별도의 임시 source를 만든다.

각 run의 생성 요청 상한은 24, native steps/errors는 40/5다. 현재 v1.1 전체 한도는 요청 216·경과 1,200초·보고 비용 $0.24 기준의 후속 요청 차단이다. 첫 시도의 보고 비용 $0.0018703952까지 합쳐 새 작업의 보고 비용 한도를 $0.25 안에 둔다. 제공자의 청구 상한을 강제하는 기능은 아니며 비용이 미확인이면 후속 실행을 중단한다.

모델/사용자 파라미터·지침·캐시·과제 분할·도구 충돌·snapshot 출처를 확인한다. 이 검사는 이 파일럿을 위한 닫힌 준비 검사다. 일반 연구 draft를 ready로 승격하거나 다른 벤치/실험 조합의 준비를 보장하지 않는다.

## 관측과 해석

`pilot-summary.json`은 원본 로그에서 재계산한다. 조건별 배정/시작/채점 수, 성공 수, list/get 호출, 제목/본문 입력이 있었던 run 수, 업무 호출/인수·최종 DB 해시, 모든 역할의 사용량/비용을 남긴다. 채점 미완료·미실행은 null/생략 사유로 보존하며 실패나 0원으로 채우지 않는다.

블록별 성공→실패·실패→성공과 업무 호출 변화, 무기억 반복 사이의 결과 불일치를 따로 보고한다. 무기억 3회에서 만드는 3개 쌍은 서로 독립이 아니다. 자연 반전을 단순 차감해 기억의 인과 효과로 확정하거나 유의성·일반 성공률을 주장하지 않는다. 반환과 입력 포함은 활용의 증명이 아니며, 정책상 금지된 행동의 판정은 호출/문맥을 별도로 검토한다.

이 과제의 채점에는 별도 한계가 있다. 고정 τ²의 `tasks.json`에서 과제 `0`은 actions/communicate_info가 비어 있고 reward_basis는 DB/COMMUNICATE다. ‘취소 진행을 거절해야 한다’는 NL assertion은 이 basis에 포함되지 않는다. 따라서 DB를 유지한 native 성공만으로 적절한 거절 설명이나 업무 품질을 확정하지 않는다. 이번 파일럿의 중심은 자율 조회와 실제 노출·도구 행동 관측이다.

준비 검사·세 조건의 wire 대조·미조회/빈은행 처리·알 수 없는 비용 중단·snapshot 가져오기/대화 격리·보관본만으로의 audit는 [모의 native 테스트](../../tests/test_tau2_autonomy.py)로 확인한다. 기존 [guided 두 회차](tau2-memory.md)와 그 원본은 유지한다.

## 실제 관측

[receipt](../../artifacts/pilots/20261114-tau2-autonomous-memory.json)에 두 시도의 프로필·원본 해시·채점 범위·비용과 audit를 남겼다. 완료한 평가 원본은 `runs/tau2-autonomy-luna-deepseek1017-pilot-02/`, 저장이 없었던 첫 시도는 `pilot-01/`에 보존한다.

| 조건 | native 성공 | list/get이 있었던 평가 | 제목/본문 노출이 있었던 평가 | 업무 호출 수, 블록 순서 | 세 평가 보고 비용 |
| --- | --- | --- | --- | --- | --- |
| 무기억 | 3/3 | 0/3, 도구 없음 | 0/3 | 4, 2, 2 | $0.003648152 |
| 빈은행 | 3/3 | 0/3 | 0/3 | 3, 2, 4 | $0.004066357 |
| 기억은행 | 3/3 | 0/3 | 0/3 | 2, 4, 2 | $0.002921039 |

실제 9회는 90요청·243,176토큰·$0.010635548이었다. OpenAI의 Luna 53회, Fireworks의 DeepSeek 1017 37회로 요청/응답 모델·제공자가 일치했고 미확인 비용은 없었다. 첫 시도까지 새 작업의 보고 비용 합계는 **$0.0125059432**다. 재사용한 과거 source의 경험/작성 비용 $0.0015464252는 별도이며 이 합계에 다시 청구된 것처럼 더하지 않는다.

이 조건에서는 기억은행이 있어도 모델이 읽지 않았다. 실제 기억 내용이 입력에 없었으므로 기억 내용의 효용·회귀를 판정할 수 없다. 무기억 반복의 native 결과 불일치는 0/3쌍이었지만, 무기억 안에서도 업무 호출 수는 4·2·2로 달랐다. 기억은행의 낮은 보고 비용을 기억 내용 덕분이라고 해석하지 않는다.

전 회차에서 get_reservation_details와 transfer_to_human_agents가 각각 1회 호출됐고 get_flight_status가 합계 7회 호출됐다. 이관의 적절성은 별도 문맥/정책 검토 대상이며 native 점수로 자동 확인됐다고 보지 않는다. 이번 과제의 점수 제약과 적은 반복 수를 함께 유지한다.

다음 질문은 미조회의 원인이다. 현재 입력만으로 충분했는지, 유용한 과거 정보가 있어도 조회 선택이 일어나지 않는지를 이번 관측만으로 구분하지 못한다. 과거에만 있는 정보와 현재 문맥의 정보 차이를 통제하는 과제, 업무 행동을 판별할 채점/주석을 후속 후보로 둔다. 이번 결과를 보고 지침을 강제 조회로 바꾼 뒤 같은 실험인 것처럼 합치지 않는다.

## 보고서용 행동 보조 분석

원래 `actions_changed`는 순서·함수·모든 인수를 비교한다. [별도 분석기](../../scripts/analyze_tau2_pilot_actions.py)는 원본 audit 후 상담원 이관의 `summary` 인수만 제외한 비교를 추가한다. 다른 인수와 순서는 유지하며 기존 로그·`pilot-summary.json`·지표를 바꾸지 않는다.

```bash
uv run --extra tau2 python scripts/analyze_tau2_pilot_actions.py --input runs/tau2-autonomy-luna-deepseek1017-pilot-02
```

[보조 분석 기록](../../artifacts/analyses/20261114-tau2-action-comparison.json)에서 원래 차이 9/9쌍 중 7/9쌍에 이관 요약 이외의 차이가 남고, 2쌍은 요약만 달랐다. 최종 DB 차이는 0/9쌍이다. 같은 실행을 공유하는 비교이며 사후 기술 분석으로 해석한다. 제외한 요약의 내용이 무의미하다는 판단이나 남은 차이의 유용성·위해성 판정은 아니다. 중요한 ID·다른 도구 인수·순서 차이를 유지하고 원본을 변경하지 않는지는 [행동 비교 테스트](../../tests/test_action_comparison.py)로 확인한다. 결과의 해석과 후속 순서는 [진행 보고서와 2주 계획](../reports/20261114-progress-and-two-week-plan.md)에 있다.
