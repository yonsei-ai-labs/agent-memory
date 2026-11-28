# τ² 무기억 탐색 실행기

2026-11-15 · 오프라인 구현·native 모의 HTTP 검증 · 실제 무기억 탐색 결과 연결

**공식 train 과제의 무기억 반복을 단일 v1.1 journal에 기록하는 전용 실행기를 구현했고, 별도로 동결한 프로필로 실제 측정했다.** [측정 결과](../reports/20261115-tau2-baseline-screening-results.md)는 계획 60회 중 56회 시작·55회 채점·44회 공식 성공이다. HTTP 503 요청의 비용 미확인으로 중단해 오류 1회·미시작 4회를 보존했다. 기억 정책 비교나 60회 전체 완료로 보고하지 않는다.

원래 초안은 그대로 두고 [ready 프로필](../../protocols/pilots/tau2-baseline-screening-v1.json)과 [실행 전 동결 기록](../../artifacts/pilots/20261115-tau2-baseline-screening-protocol.json)에 새 $1 한도를 고정했다. 확인된 보고 비용은 $0.1055333152에 미확인 요청 1건이 더해진다. 중단한 원본을 자동 재개하지 않는다.

## 준비와 실행 방법

[프로필 초안](../../protocols/drafts/tau2-baseline-screening-v1.json)은 검토용 제안이다. 전체 train 30개 × 2회, native seed 0·1, 배정 seed 20261115를 명시한다. 각 반복에서 30개 순서를 따로 섞으며 같은 과제가 여러 출처를 공유할 수 있다는 사실은 유지한다. 다른 seed가 실행 독립성을 보장한다고 해석하지 않는다.

검토용 한도는 run마다 native 200단계·도구 오류 10회·모델 요청 200회·보고 비용 $0.10·600초, 전체 요청 12,000회·10,800초다. **전체 비용 상한은 null이고 status는 draft라서 유료 실행이 차단된다.** 이 한도들은 이전 연결 확인의 제한을 바꾼 새 제안이며 사용자 승인된 실험값으로 표현하지 않는다.

다음 두 명령은 모델·catalog HTTP 호출과 실행 폴더 생성 없이 동작한다. `--preflight`는 고정 checkout·설치 commit·공식 train 포함·자산 7개의 해시·배정표를 확인한다. 실제 native 소스와 전체 응답 캐시의 검사는 live 준비와 native 모의 검증에서 수행한다.

```bash
uv run --frozen --extra tau2 python scripts/run_tau2_baseline.py
uv run --frozen --extra tau2 python scripts/run_tau2_baseline.py --preflight
```

기본 초안의 preflight는 30과제·60배정과 미정 필드 `guards.max_reported_cost_usd`를 보고한다. 배정 해시는 `dbb7abf3818b5b3cd8cea8fb1efa0583ef416180a7d1a7196d8eb623b64f3cb6`이다. 실행 전 새 프로필의 모든 한도·비용·반복과 구현을 확정하고 해시를 보존한다. live는 ready 프로필과 새/빈 출력 폴더를 요구한다. 과거 폴더의 자동 재개·덮어쓰기는 지원하지 않는다.

## 실행 경로와 기록

- [실행기](../../src/agent_memory_eval/runner/tau2_baseline.py)는 고정 native 생성기와 기존 `Tau2Bridge`를 사용한다. 기억 서비스·은행·snapshot·기억 지침을 생성하지 않으며, native가 제공하지 않은 도구·system 문맥을 덧붙이지 않는다.
- [별도 배치 스키마](../../schemas/baseline-screening.schema.json)가 선택 과제·반복별 seed·native 한도·run/전체 guard를 검증한다. 기존 v1.1 연결 파일은 역할·모델·제공 경로의 원본으로 검증한다. 배치에서 계산한 runtime 설정의 seed·한도는 새 배치 계약을 따르며, 이전 연결 스키마의 제한을 수정하거나 계산한 설정을 그 스키마의 새 연결 프로필로 내보내지 않는다.
- 매 배정에 새 worker·사용자·업무 환경/DB·session을 만든다. worker seed는 해당 반복의 native seed와 맞추고, 지원하지 않는 DeepSeek wire seed는 기존 역할 정책에 따라 생략한다. reasoning low·OpenAI/Fireworks 경로·fallback 끔·사용자 reasoning 끔을 유지한다.
- 첫 원본 commit에 실제 프로필·원본 연결 텍스트·벤치 자산·배정표·모델/endpoint 메타데이터·구현 소스 해시를 기록한다. run별 native 설정·정본 prompt·도구·task 참조를 모델 요청 전에 기록한다. 채점 정답과 분석 문서는 worker의 요청 출처로 추가하지 않는다.
- run/전체 비용·요청·시간 제한을 함께 적용한다. 두 범위의 사전 검사가 모두 통과한 호출만 HTTP 시도로 센다. 금액 검사는 가격 메타데이터와 바이트 기반 사전 예약 및 반환 사용량을 사용하며 provider가 강제하는 절대 결제 상한은 아니다.
- 기존 transport의 직렬화 요청 64,000 bytes·개별 HTTP 최대 45초 제한도 적용된다. 입력 크기로 중단하면 별도 오류로 기록한다. 출력 한도는 원본 프로필의 worker 2,048·사용자 512이며, 원응답의 finish reason도 보존한다.

## 중단·결측과 재검증

run의 요청·시간·금액 한도는 해당 run의 미완료로 기록하고 남은 배정을 계속한다. 전체 guard 또는 미확인 비용이 발생하면 남은 배정마다 생략 이유를 기록한다. 복구되지 않은 일반 runtime 오류도 배치를 중단한다. 공식 채점이 있는 native 단계/시간/오류 한도 종료는 점수를 보존하면서 `bounded`를 별도로 기록한다. 제한을 만났다는 사실로 실패의 인과 원인을 확정하지 않는다.

요약은 planned·started·graded·successful·skipped·ungraded·bounded, run별 종료/오류·공식 결과·원본 보고서 참조, 전체 및 역할별 요청·사용량·보고 비용·미확인 비용을 제공한다. 무기억 실패를 재실행 성공으로 교체하거나 미시작 배정을 분모에서 숨기지 않는다.

저장 증가를 줄이기 위해 run 종료 시 **현재 head의 provenance 사전만 비운다.** 과거 요청의 manifest·출처 registry와 해당 commit의 불변 state blob은 남는다. [분석기](../../src/agent_memory_eval/analysis/tau2_baseline.py)는 요청이 확정된 commit의 출처를 읽으므로 최종 head에 모든 과거 run을 누적 복사할 필요가 없다. 코어의 journal/event/manifest v1.1은 유지하며 기존 실행기는 수정하지 않았다. 고유 확정 state blob의 개수·바이트도 요약에 남긴다. 이는 실제 60회의 저장량 측정이나 기존 원본의 압축·변경 결과가 아니다.

완료한 새 원본은 checkout·API 없이 아래처럼 읽기 전용으로 감사할 수 있다. 저장된 소스·배정·요청 출처·정본 prompt·도구·역할별 설정·제공자·사용량/비용·native 결과·요약을 대조한다. 중단된 run·미확정 꼬리·미완료 요청은 완전한 원본으로 승인하지 않으며 그대로 보존한다.

```bash
uv run --frozen --extra tau2 python scripts/run_tau2_baseline.py --audit-only --output runs/NEW_BASELINE_RUN
uv run --frozen --extra tau2 python -m unittest discover -s tests -p 'test_tau2_baseline.py'
```

[행동 검증](https://github.com/yonsei-ai-labs/agent-memory/blob/40f881aa1f70f5772f52484a4f1a19d0de01162d/tests/test_tau2_baseline.py)은 fake HTTP를 사용한다. 과제별 실제 Luna 성공률·기억 효용·일반 전이의 결과와 구분한다. 실제 실행과 실패 경로 주석·경험 재사용 후보의 선정은 이 구현 뒤의 실증 단계다.

실제 호출 전 새 12개를 포함한 전체 128개 테스트가 통과했다. 기본 설계·링크·보관본 검사, 새 배치 스키마의 메타 검증, 이전 48회 동결 파일 64개 해시 대조도 통과했다. 이후 별도 ready 프로필의 측정과 읽기 전용 원본 감사가 완료됐다. [결과 receipt](../../artifacts/pilots/20261115-tau2-baseline-screening-results.json)·[행동 검토](../../artifacts/analyses/20261115-tau2-baseline-behavior-review.json)·[오프라인 DB 채점 진단](../../artifacts/analyses/20261115-tau2-train-grading-diagnostic.json)은 분석 전용이며 worker/writer 입력으로 사용하지 않는다. 새 공개 분석 테스트 4개는 기존 전체 128개 실행 이력과 별도로 통과했다.
