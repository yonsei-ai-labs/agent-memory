# τ² 기억 연결과 두 회차 확인

2026-11-14 · 같은 업무 환경을 새로 시작하는 guided_check

Luna 작업자의 `memory_store`·`memory_list`·`memory_get`을 파일 기억 서비스에 연결한다. 사용자 시뮬레이터는 DeepSeek V4.1 Flash 1017이며 [기존 연결 설정](tau2-connection.md)의 OpenRouter → Fireworks 경로를 유지한다. Luna의 추론은 low다.

현재 실행기는 **연결 진단용 두 회차**를 제공한다. 1회차 업무 종료·native 채점 뒤 같은 작업자의 공개 이력에서 기억을 작성한다. 2회차는 새 작업자·사용자·환경·대화에서 원 snapshot을 복제해 조회한다. 저장 내용은 모델이 작성한다. 이 확인은 저장·조회를 지시하는 `guided_check`이며 자율 조회율·기억 정책 효과의 측정과 구분한다.

## 실행

τ² extra와 고정 checkout 설치는 [기존 안내](tau2-connection.md)를 따른다. 각 출력 경로는 새/빈 디렉터리여야 한다.

```bash
uv run --extra tau2 python scripts/run_tau2_memory_check.py --output runs/tau2-memory-preflight
uv run --extra tau2 python scripts/run_tau2_memory_check.py --output runs/tau2-memory-live --live
uv run --extra tau2 python scripts/run_tau2_memory_check.py --output runs/tau2-memory-live --audit-only
```

`--live`가 없으면 공개 모델/endpoint 정보와 과제·도구·설정/지침 해시를 확인하고 유료 생성 요청은 보내지 않는다. `--audit-only`는 기존 로그와 blob만 읽으며 재실행·API 호출·쓰기 없이 출처·노출·은행·사용량/비용·채점 결과를 대조한다.

[진단 프로필](../../protocols/checks/openrouter-tau2-memory-smoke.json)은 [별도 스키마](../../schemas/memory-connection-check.schema.json)를 따른다. 기존 무기억 프로필과 지침 파일을 SHA-256으로 고정하고, 실제 사용한 원문·모델 metadata·코드 해시를 journal에 보존한다. 연구 draft를 ready로 바꾸지 않는다.

| 항목 | 연결 진단 설정 |
| --- | --- |
| 업무 | 기존 airline train 과제 `0`을 두 새 환경에서 수행 |
| 1회차 | 수행 중 기억 도구 없음, 종료 후 memory_store로 모델 작성 |
| 2회차 | 시작 snapshot 복제, memory_list와 memory_get, 쓰기 없음 |
| 읽기 보기 | run 시작 버전 고정, 목록 페이지 최대 4항목 |
| 저장/반환 한도 | 명시적인 null: 이 진단에서 별도 용량 상한 없음 |
| 실행 한도 | 각 run 생성 요청 24·native steps 40/errors 5. 두 run 합계 요청 48·경과 360초·보고 비용 $0.25 기준 후속 요청 차단 |
| 채점 | 기존 native substring 설정, 결과/정답은 writer에게 제공하지 않음 |
| 종료 후 작성 | 같은 작업자의 공개 이력, 기억 도구만 제공. 모델 완료 응답 또는 실행 한도로 종료 |

금액 한도는 클라이언트의 후속 요청 차단이며 제공자의 청구 상한 보장은 아니다. 미확인 비용은 0으로 채우지 않는다. 전송/작성 실패도 원본과 부분 기억을 보존하고 종료 사유를 남긴다. 수집이 실패하거나 비용이 미확인이면 2회차는 실행하지 않고 사전 배정 ID와 생략 사유를 기록한다.

## 작업자와 벤치의 경계

[Tau2MemoryBridge](../../src/agent_memory_eval/adapters/tau2_memory.py)는 native 작업자의 전체 이력에 기억 호출과 결과를 보관한다. 기억만 있는 응답은 내부에서 처리해 다음 작업자 요청으로 이어진다. 사용자 시뮬레이터·환경 전이·native 단계/오류 카운터는 진행하지 않는다. 모델 요청·경과·비용 한도는 계속 적용한다.

기억과 업무 도구가 섞인 응답은 [공통 라우터](../../src/agent_memory_eval/runner/router.py)가 원래 순서대로 부작용을 실행한다. 업무 호출은 native 실행기로 보내므로 업무 오류는 원래 오류 한도에 반영한다. native 대화/채점 trace에는 업무 호출과 업무 결과를 남기고, 작업자에게는 기억 결과까지 원래 순서로 합쳐 전달한다. 종료 도구가 다른 호출과 섞이면 기억을 포함해 모두 거절하고 native 종료/거절 trace를 유지한다.

native parser가 호출 인수를 다시 직렬화하므로 작업자의 후속 전송은 해당 응답의 **제공자 원문**을 승인 출처로 재사용한다. native 이력 위치·값을 대조해 원래 응답에 연결한다. 자기 작성 인수는 `self_authored`, 기억 결과는 `memory_return` 출처이며 제목·본문의 실제 입력 구간을 기록한다. 알 수 없는 도구 결과나 바뀐 작업자 이력은 전송 전에 거절한다.

사용자 시뮬레이터에는 native 업무 대화만 전달한다. writer는 공개 경험 접두부와 같은 작업자의 이력·작성 지침을 사용하고, 사용자 전용 시나리오·채점 결과·개발 문서는 입력으로 받지 않는다. 사후 작성 오류는 이미 고정한 업무 점수를 바꾸지 않는다. 각 평가 은행은 snapshot의 별도 복제본이며 원은행에 쓰기를 되돌리지 않는다.

native 보고서의 비용에는 내부 기억 요청과 사후 작성이 빠질 수 있다. **전체 비용의 기준은 journal의 모든 역할 요청과 제공자 원 usage/cost**다.

## 실제 연결 결과

[확인 receipt](../../artifacts/connection-checks/20261114-openrouter-tau2-memory.json)에 설정·모델 metadata·코드 참조·원본 해시·읽기 전용 audit를 보존했다. 원본은 `runs/tau2-memory-luna-deepseek1017-check-01/`에 있으며 Git에 포함하지 않는다.

| 관측 | 결과 |
| --- | --- |
| 1회차 | 업무 native 성공 1.0, 사후 기억 1개 작성·봉인 |
| 새 2회차 | 원 snapshot의 새 은행에서 list/get 각 1회, 업무 native 성공 1.0 |
| 업무 전이/오류 | 각 13/9단계, native 도구 오류 모두 0 |
| 모델 요청 | worker 12·writer 2·사용자 6, 총 20회 모두 완료 |
| 응답 경로 | OpenAI 14회, Fireworks 6회. 지정 Luna/DeepSeek 1017 식별 일치 |
| 실제 입력 노출 | 조회 기억의 제목/본문 구간 10개, 자기 작성 구간 2개. 재노출 포함 |
| 전체 보고 사용량/비용 | 58,325토큰, $0.0026238194, 미확인 비용 없음 |
| 원본 점검 | 요청 출처·은행/읽기 보기·반환/입력 포함·모델/사용량/비용·채점 audit 통과, 실제 키 값 미포함 |

저장과 조회를 지시한 한 과제의 연결 결과다. 두 회차의 성공이나 단계 차이를 기억의 인과적 이득으로 해석하지 않는다. 일반 실험에는 같은 binding/직렬화 경로의 무기억·빈은행 대조와 반복 변동 측정이 필요하다.

## 검증과 후속 범위

[native 행동 테스트](https://github.com/yonsei-ai-labs/agent-memory/blob/40f881aa1f70f5772f52484a4f1a19d0de01162d/tests/test_tau2_memory.py)는 HTTP를 모의 처리하며 유료 모델을 호출하지 않는다. 실제 native 환경·대화·채점에 연결해 다음을 확인한다.

- 두 회차의 새 객체/세션, snapshot 전달, 제목·본문·자기 작성 노출, 사용자/채점 trace의 기억 왕복 제외.
- 기억·업무·기억의 부작용/결과 순서, 기억 오류 11회에도 native 단계/오류 한도 보존, 종료 도구 batch 전체 거절.
- 복제본 갱신 후 원은행/다른 복제본 불변과 시작 버전 읽기 유지.
- 사후 작성 중단 시 기존 업무 점수·커밋된 기억 보존, 요청 한도 중단과 미실행 probe 명시.
- 수행 중 초안이 같은 공개 이력의 사후 작성에서 새 버전으로 갱신되는 병행 작성.
- 읽기 전용 audit와 요약 점수 변조 거절.

```bash
uv run --extra tau2 python -m unittest discover -s tests
uv run python scripts/validate_design.py
uv run python scripts/check_repository.py
uv run python scripts/verify_legacy_data.py
```

실제 모델 확인의 결과와 원본 해시는 별도 receipt에 남긴다. 일반 연구 계획의 의미/준비 검사, 자율 정책의 반복 비교, preload/가공·누적·교차·개입·통계와 AppWorld/BFCL은 후속이다. 수행 중·병행 저장은 native 모의 행동 경로에서 검증하며, 현재 공개 두 회차 프로필은 종료 후 작성과 자율 조회 도구의 연결 진단에 한정한다.
