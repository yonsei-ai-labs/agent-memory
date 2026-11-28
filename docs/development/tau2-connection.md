# OpenRouter·Luna·τ² 연결 확인

2026-11-14 · 무기억 단일 과제의 실제 연결과 기록

OpenRouter의 `openai/gpt-6-luna`를 작업자, `deepseek/deepseek-v4.1-flash-1017`을 사용자 시뮬레이터로 사용한다. 사용자의 모델 변경 요청을 반영한 현재 기본 설정이다. 역할별 제공자는 각각 OpenAI와 Fireworks로 고정한다. `OPENROUTER_API_KEY`를 환경변수에서 읽으며 `OPENAI_API_KEY`로 직접 호출하지 않는다. 키와 인증 헤더는 모델 입력·journal·커밋에 저장하지 않는다.

이 프로필의 지원은 **무기억 연결 확인**이며 외부 기억 도구를 거절한다. 별도의 [기억 binding/두 회차 진단](tau2-memory.md)에서 기억 왕복·혼합 호출·사후 작성·새 세션 조회를 연결했다. 이 무기억 확인을 기억 정책의 이득·회귀·전이 실험으로 해석하지 않는다.

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

실행 출력 폴더는 새/빈 경로만 허용한다. `--live`가 없으면 과제/도구·정책·모델/endpoint 목록·파라미터·출처와 한도를 확인하며 유료 생성 요청은 보내지 않는다. 공개 목록 조회와 Git 다운로드는 네트워크를 사용한다. `--audit-only`는 저장 원본의 해시·요청 manifest·역할·파라미터·사용량/비용·채점 보고서를 읽기만 하며 API를 호출하지 않는다.

## 별도 연결 프로토콜

[연결 설정](../../protocols/checks/openrouter-tau2-smoke.json)은 [전용 v1.1 스키마](../../schemas/connection-check-v1.1.schema.json)를 따른다. `intent=connection_check`이며 연구용 draft/ready 계획을 대신하지 않는다. 기존 연구 초안을 임의 값으로 채우지 않았다. 이전 GPT-4.1의 [v1.0 설정](../../protocols/checks/openrouter-tau2-smoke-v1.0.json)·[스키마](../../schemas/connection-check.schema.json)·원본 실행 기록은 이력 확인용으로 보존하며 CLI의 유료 실행에서는 거절한다.

| 항목 | 이번 확인에 고정한 값 |
| --- | --- |
| 과제 | airline train 첫 과제 ID `0`, 1회. 결과를 보고 교체하지 않음 |
| 작업자 | Luna, reasoning low, 출력 상한 2,048, seed 0. 미지원 temperature를 붙이지 않음 |
| 사용자 | DeepSeek V4.1 Flash 1017, temperature 0, max_tokens 512, reasoning.enabled false, seed 전달 안 함 |
| 경로 | OpenRouter → 역할별 OpenAI/Fireworks only, fallback 금지, 파라미터 지원 필수 |
| native 한도 | max_steps 40, max_errors 5, DB 지역화 false |
| 연결 한도 | 생성 요청 최대 24, 경과 180초, 보고 비용 $0.25 기준의 후속 요청 차단 |
| 전송 | 비스트리밍·상태 없는 전체 요청, 자동 재시도 0, 요청 bytes 상한 64,000, HTTP 대기 최대 45초와 남은 경과 시간 중 작은 값 |
| 전달 내용 채점 | native evaluator의 지원 모드 `substring`. 기본 semantic 판정의 별도 LLM 호출은 수행하지 않음 |
| 캐시 | native LiteLLM 전체 응답 캐시 비활성화, OpenRouter 응답 캐시 플러그인 없음. 제공자 prefix 캐시 사용량은 원 응답에 보존 |

금액 한도는 제공자 보고와 bytes 기반 사전 예상량으로 다음 요청을 막는 클라이언트 제약이다. 선택한 endpoint의 광고 가격과 시간대/문맥별 override 중 높은 가격으로 사전 예약한다. 제공자의 청구 상한을 강제하는 기능은 아니다. 비용을 확인할 수 없으면 후속 유료 호출을 중단한다. 사용량·미확인 비용을 0으로 채우지 않는다. seed를 보냈다는 이유로 호스팅 모델의 결정성을 가정하지 않는다. 모델 alias·목록의 canonical slug·실제 응답 모델/제공자를 함께 보존한다.

τ²는 benchmark seed 0을 두 역할의 LLM 인수에 자동 추가한다. 현재 Fireworks endpoint에는 seed 지원이 없으므로 v1.1의 `forward_native_seed=false`가 사용자 역할의 wire에서만 이를 제거한다. native 주입 값부터 검증하고 승인된 최종 요청/manifest를 만든다. Luna는 기존 seed 0과 reasoning low를 유지한다. 사용자 비추론 모드는 짧은 사용자 발화의 연결 확인을 위한 명시적 설정이며, temperature 0도 결정성을 보장하지 않는다.

OpenRouter의 [라우팅 규칙](https://openrouter.ai/docs/guides/routing/provider-selection)·[chat API](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request)·[추론 제어](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)를 확인했다. 모델 가용성·파라미터·가격은 실행 전 공개 models/endpoint API에서 확인하고 당시 항목을 원본에 저장한다. endpoint 목록의 출력 상한 명칭 `max_tokens`는 OpenRouter의 `max_completion_tokens` 별칭과 동등하게 검사하며 wire의 명시된 이름을 바꾸지 않는다.

## 계측과 정보 경계

[Tau2Bridge](../../src/agent_memory_eval/adapters/tau2.py)는 native agent/user의 메시지 구성·파싱과 환경/채점 코드를 유지하면서, 두 역할의 생성 경계와 completion 전송을 해당 확인의 범위 안에서 연결한다. 전역 교체는 context manager 종료 때 복원하며 병렬 실행을 지원하지 않는다. 예상하지 않은 추가 모델 호출은 전송 전에 거절한다.

worker/user별로 native system prompt를 고정하고, 새 native 상태 메시지는 수신 역할이 붙은 `benchmark_message` 생산 사건으로 기록한다. 요청과 manifest를 함께 구성하고 native wrapper가 메시지·도구·파라미터를 바꾸면 거절한다. 사용자 전용 시나리오와 채점 과제 원문은 내부 자료이며 worker 요청에 전체를 붙이지 않는다. 이 검증은 native adapter 경계의 생산자를 신뢰한다. 실제 기억 도구를 추가하면 자기 작성/기억 반환 경로와 사용자 투영을 다시 검증해야 한다.

[RecordingGateway](../../src/agent_memory_eval/recording/provenance.py)와 [OpenRouterClient](../../src/agent_memory_eval/models/openrouter.py)가 실제 JSON 요청·응답·usage·provider 보고 cost를 저장한다. native 비용 함수의 가격표/오류 시 0 반환을 사용하지 않고 관측한 비용을 native message에도 연결한다. 전송 전 거절·HTTP 응답 오류·전송 불명·완료 응답을 구분한다.

### 진행 중인 공급자 장애에 대응하기

Linux에서 등록된 상담 묶음을 실행할 때는 원장에 기록되는 공급자 응답으로 전송 속도를 조절할 수 있다. 실행기가 연구 디렉터리를 연 뒤 아래 명령을 별도 셸에서 실행한다. `controller_pid`는 해당 실행기의 PID, `study_dir`은 그 실행의 원본 디렉터리, `operator_log`는 운영 기록을 남길 파일이다. 감시기는 실행기가 해당 디렉터리를 열고 있는지와 자식 프로세스의 신원을 확인한다.

```sh
python3 scripts/watch_registered_provider.py --controller "$controller_pid" --study "$study_dir" --role user_simulator --report "$operator_log"
```

감시 대상 역할에서 30초 안에 HTTP 429·502·503·504가 두 번 나오면 해당 실행의 전송을 잠시 멈춘다. 대기 시간은 120·600·1,800초 순으로 늘리고 공급자가 요청한 대기가 더 길면 그 시간을 따른다. 진행 중인 배정 하나에서 새 요청의 정상 응답을 확인하면 같은 실행을 재개한다. 묶음 사이에 진행 중인 상담이 없으면 다음 등록 묶음을 감시하면서 연다. 기존 오류와 비용은 원장에 남으며, 관측에 사용한 요청도 원래 배정에 포함된다. [운영 도구](../../scripts/watch_registered_provider.py) · [역할별 영수증과 프로세스 격리 검증](https://github.com/yonsei-ai-labs/agent-memory/blob/40f881aa1f70f5772f52484a4f1a19d0de01162d/tests/test_registered_provider_watch.py)

## 현재 v1.1 실제 확인 결과

[DeepSeek 1017 확인 기록](../../artifacts/connection-checks/20261114-openrouter-tau2-deepseek1017.json)에 성공 실행과 앞선 실패 2건을 함께 남겼다. 성공 원본은 `runs/tau2-openrouter-luna-deepseek1017-check-03/`이며 이전 실행을 덮어쓰지 않았다.

| 관측 | 결과 |
| --- | --- |
| 실제 생성 요청 | Luna 8회, DeepSeek 1017 사용자 5회, 총 13회 응답 완료 |
| 업무 도구 호출 | 4회, native 도구 오류 0 |
| native 종료/채점 | 17단계, user_stop, substring 모드 점수 1.0/성공 true |
| 응답 경로 | OpenAI 8회, Fireworks 5회. 요청한 두 모델 식별 일치 |
| 추론 | Luna 요청 모두 low, 보고 추론 토큰 합 173. 사용자 요청 enabled false, 보고 추론 토큰 모두 0 |
| 보고 사용량/비용 | 성공 실행 35,036토큰, $0.0015058204, 해당 실행의 미확인 비용 없음 |
| 원본 점검 | 요청 출처·역할·파라미터·응답 제공자/모델·사용량/비용·채점 대조 통과, 실제 키 값 미포함 확인 |

앞선 `check-01`·`check-02`는 같은 DeepSeek 모델의 제공자를 `deepseek`로 지정했다가 첫 사용자 요청에서 HTTP 404로 중단했다. 두 번째 기록의 제한된 오류 분류는 `data_policy`이며, 계정 정책과 일치하는 endpoint가 없다는 응답이었다. 원문 오류/인증 헤더는 저장하지 않는다. 계정 설정을 바꾸지 않고 동일 모델의 `fireworks` endpoint를 명시해 새 실행으로 확인했다. 자동 fallback이나 다른 모델로의 교체는 없었다. 실패 2건에는 usage/cost가 없어 비용 미확인으로 남겼으며 0원이라고 단정하지 않는다.

이 확인은 사용자 모델 변경 뒤의 무기억 연결 검증이다. 이전 GPT-4.1 실행과 성공률·비용 우열을 비교하거나 기억 정책 효과로 해석하지 않는다.

## 이전 v1.0 실제 확인 결과: 보존 기록

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

단위/통합 테스트는 모의 HTTP만 사용한다. [전송 테스트](../../tests/test_openrouter.py)는 경로·비밀 값·한도·미확인 비용·지원 파라미터·원 usage 보존·역할 차단을 검사한다. [native 테스트](https://github.com/yonsei-ai-labs/agent-memory/blob/40f881aa1f70f5772f52484a4f1a19d0de01162d/tests/test_tau2_connection.py)는 추가 기능과 checkout이 있을 때 모의 응답으로 실제 도구 왕복·native 채점·읽기 전용 재검증을 확인한다. 테스트 명령 자체는 유료 모델을 호출하지 않는다.
