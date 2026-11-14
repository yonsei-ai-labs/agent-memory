# OpenRouter·Luna·τ² 연결 확인

2026-11-14 · 무기억 단일 과제의 실제 연결과 기록

OpenRouter의 `openai/gpt-6-luna`를 작업자, `openai/gpt-4.1`을 사용자 시뮬레이터로 사용한다. 두 역할 모두 OpenRouter를 통해 OpenAI 제공자로만 보낸다. `OPENROUTER_API_KEY`를 환경변수에서 읽으며 `OPENAI_API_KEY`로 직접 호출하지 않는다. 키와 인증 헤더는 모델 입력·journal·커밋에 저장하지 않는다.

현재 지원은 **무기억 연결 확인**이다. 실제 외부 기억 도구는 이 τ² binding에서 거절한다. 파일 기억 서비스와 가짜 두 회차 흐름은 별도로 동작하며, 실제 τ²의 기억 왕복·혼합 호출·사후 작성은 다음 연결 단계다. 이 확인을 기억 정책의 이득·회귀·전이 실험으로 해석하지 않는다.

## 설치와 실행

τ² 추가 기능에는 Python 3.12/3.13을 사용한다. 기본 오프라인 코어에는 τ² 설치가 필요하지 않다. DB 서버나 GPU는 설치하지 않는다.

```bash
uv sync --extra tau2
uv run --extra tau2 python scripts/setup_tau2.py
uv run --extra tau2 python scripts/run_tau2_check.py --output runs/tau2-preflight
uv run --extra tau2 python scripts/run_tau2_check.py --output runs/tau2-live-check --live
uv run --extra tau2 python scripts/run_tau2_check.py --output runs/tau2-live-check --audit-only
```

`setup_tau2.py`는 데이터/소스용 checkout을 Git 밖의 `data/tau2-bench/`에 준비한다. 기존 checkout이 다른 commit이거나 수정돼 있으면 덮어쓰지 않는다. 설치 패키지의 VCS commit과 Python 원문도 checkout과 대조한다. 고정 revision은 `c06b07dd7c4d4ec61ba6ff9bcdf0b15dd7ef36d4`다.

실행 출력 폴더는 새/빈 경로만 허용한다. `--live`가 없으면 과제/도구·정책·모델 목록·파라미터·출처와 한도를 확인하며 유료 생성 요청은 보내지 않는다. 공개 모델 목록 조회와 Git 다운로드는 네트워크를 사용한다. `--audit-only`는 저장 원본의 해시·요청 manifest·역할·파라미터·사용량/비용·채점 보고서를 읽기만 하며 API를 호출하지 않는다.

## 별도 연결 프로토콜

[연결 설정](../../protocols/checks/openrouter-tau2-smoke.json)은 [전용 스키마](../../schemas/connection-check.schema.json)를 따른다. `intent=connection_check`이며 연구용 draft/ready 계획을 대신하지 않는다. 기존 연구 초안을 임의 값으로 채우지 않았다.

| 항목 | 이번 확인에 고정한 값 |
| --- | --- |
| 과제 | airline train 첫 과제 ID `0`, 1회. 결과를 보고 교체하지 않음 |
| 작업자 | Luna, reasoning low, 출력 상한 2,048, seed 0. 미지원 temperature를 붙이지 않음 |
| 사용자 | GPT-4.1, temperature 0, 출력 상한 512, seed 0 |
| 경로 | OpenRouter → OpenAI only, fallback 금지, 파라미터 지원 필수 |
| native 한도 | max_steps 40, max_errors 5, DB 지역화 false |
| 연결 한도 | 생성 요청 최대 24, 경과 180초, 보고 비용 $0.25 기준의 후속 요청 차단 |
| 전송 | 비스트리밍·상태 없는 전체 요청, 자동 재시도 0, 요청 bytes 상한 64,000, HTTP 대기 최대 45초와 남은 경과 시간 중 작은 값 |
| 전달 내용 채점 | native evaluator의 지원 모드 `substring`. 기본 semantic 판정의 별도 LLM 호출은 수행하지 않음 |
| 캐시 | native LiteLLM 전체 응답 캐시 비활성화, OpenRouter 응답 캐시 플러그인 없음. 제공자 prefix 캐시 사용량은 원 응답에 보존 |

금액 한도는 제공자 보고와 bytes 기반 사전 예상량으로 다음 요청을 막는 클라이언트 제약이다. 제공자의 청구 상한을 강제하는 기능은 아니다. 비용을 확인할 수 없으면 후속 유료 호출을 중단한다. 사용량·미확인 비용을 0으로 채우지 않는다. seed를 보냈다는 이유로 호스팅 모델의 결정성을 가정하지 않는다. 모델 alias·목록의 canonical slug·실제 응답 모델/제공자를 함께 보존한다.

OpenRouter의 [라우팅 규칙](https://openrouter.ai/docs/guides/routing/provider-selection)과 [chat API](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request)를 확인했다. 모델 가용성·파라미터·가격은 실행 전 공개 models API에서 확인하고 당시 항목을 원본에 저장한다.

## 계측과 정보 경계

[Tau2Bridge](../../src/agent_memory_eval/adapters/tau2.py)는 native agent/user의 메시지 구성·파싱과 환경/채점 코드를 유지하면서, 두 역할의 생성 경계와 completion 전송을 해당 확인의 범위 안에서 연결한다. 전역 교체는 context manager 종료 때 복원하며 병렬 실행을 지원하지 않는다. 예상하지 않은 추가 모델 호출은 전송 전에 거절한다.

worker/user별로 native system prompt를 고정하고, 새 native 상태 메시지는 수신 역할이 붙은 `benchmark_message` 생산 사건으로 기록한다. 요청과 manifest를 함께 구성하고 native wrapper가 메시지·도구·파라미터를 바꾸면 거절한다. 사용자 전용 시나리오와 채점 과제 원문은 내부 자료이며 worker 요청에 전체를 붙이지 않는다. 이 검증은 native adapter 경계의 생산자를 신뢰한다. 실제 기억 도구를 추가하면 자기 작성/기억 반환 경로와 사용자 투영을 다시 검증해야 한다.

[RecordingGateway](../../src/agent_memory_eval/recording/provenance.py)와 [OpenRouterClient](../../src/agent_memory_eval/models/openrouter.py)가 실제 JSON 요청·응답·usage·provider 보고 cost를 저장한다. native 비용 함수의 가격표/오류 시 0 반환을 사용하지 않고 관측한 비용을 native message에도 연결한다. 전송 전 거절·HTTP 응답 오류·전송 불명·완료 응답을 구분한다.

## 실제 확인 결과

[확인 기록](../../artifacts/connection-checks/20261114-openrouter-tau2.json)에 설정·모델 목록 스냅샷·원본 해시와 재검증 결과를 남겼다. 전체 원본은 `runs/tau2-openrouter-luna-check-01/`에 있고 Git에 포함하지 않는다.

| 관측 | 결과 |
| --- | --- |
| 실제 생성 요청 | worker 7회, 사용자 5회, 총 12회 모두 응답 완료 |
| 업무 도구 호출 | 3회, native 도구 오류 0 |
| native 종료 | 15단계, user_stop |
| native 채점 | 1.0, 성공 true. 위 substring 모드의 단일 과제 결과 |
| 응답 경로 | OpenAI 12회, Luna/GPT-4.1 모델 식별 일치 |
| 보고 토큰 | 31,608. prefix/추론 등의 세부 항목은 원 응답에 보존 |
| 보고 비용 | $0.011176270, 미확인 비용 없음 |
| 원본 점검 | 요청 manifest·역할·파라미터·사용량/비용·채점 해시 검증, 키 값 미포함 확인 |

이 결과는 무기억 1개 train 과제의 연결 확인이다. 반복 성공률·기억 효과·시험 분할 일반화나 공식 leaderboard 재현을 주장하지 않는다. 이 연결 단계 이후 실제 기억 호출을 작업자 내부에서 처리하는 어댑터와 두 회차 격리를 검증한다.

## 회귀 검증

```bash
uv run --extra tau2 python -m unittest discover -s tests
uv run python scripts/validate_design.py
uv run python scripts/check_repository.py
uv run python scripts/verify_legacy_data.py
```

단위/통합 테스트는 모의 HTTP만 사용한다. [전송 테스트](../../tests/test_openrouter.py)는 경로·비밀 값·한도·미확인 비용·지원 파라미터·원 usage 보존·역할 차단을 검사한다. [native 테스트](../../tests/test_tau2_connection.py)는 추가 기능과 checkout이 있을 때 모의 응답으로 실제 도구 왕복·native 채점·읽기 전용 재검증을 확인한다. 테스트 명령 자체는 유료 모델을 호출하지 않는다.
