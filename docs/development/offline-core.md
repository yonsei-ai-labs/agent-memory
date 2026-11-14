# 오프라인 코어 실행과 검증

2026-11-14 · 실제 로컬 파일·요청 구성·도구 실행을 연결한 첫 구현

SQLite·API 키·GPU·벤치 다운로드 없이 실행한다. `FakeWorker`와 `FakeBenchmark`는 정해진 행동과 작은 업무 상태를 사용하는 합성 구성이다. 이 실행은 파일/정보 경계의 행동 검증이며 Luna의 능력·기억 정책 효과·τ² 공식 점수를 측정하지 않는다.

## 실행

저장소 checkout과 잠금 환경에서 실행한다. 출력은 새 디렉터리 또는 빈 디렉터리만 허용한다. 기존 실행에 덧붙이거나 덮어쓰지 않는다.

```bash
uv sync --frozen
uv run python scripts/run_offline_demo.py --output runs/offline-example
uv run python scripts/run_offline_demo.py --output runs/offline-example --analyze-only
```

기본 builder는 두 원천 업무를 수집하고 자기 경험/혼합 snapshot을 만든 뒤 각 업무의 무기억·자기 경험·혼합 풀 probe를 수행한다. 총 8개 run과 4개 결과 짝을 응답 전에 배정한다. 각각 새 대화·작업자·환경·은행 분기를 사용한다. 원천/혼합 은행은 작성한 동일 항목 버전을 공유하고 probe의 사후 쓰기는 해당 분기에만 남는다.

페이지 크기 4, run별 최대 합성 요청 12는 이 builder의 테스트 설정이다. 실제 계획을 채우거나 `ready`로 승격시키지 않는다. 저장 시점 네 종류는 `OfflineRunner.run()`의 명시적 정책으로 행동 검증한다. 기본 예제는 종료 후 작성이다.

```text
실행 디렉터리/
  log.jsonl
  writer.lock
  blobs/sha256/…
  views/banks/…
  views/snapshots/…
  views/events.jsonl
  analysis/<synthetic_revision>/summary.json
```

원본 로그와 불변 blob만으로 상태를 읽으며 views는 재생성할 수 있다. 최초 실행은 새 analysis revision에 집계를 저장하고, `--analyze-only`는 원본과 집계 파일에 쓰지 않고 재계산해 출력한다. 개별 실행의 `official_success`·공식 점수·USD 비용은 null이다. `synthetic_outcomes`는 가짜 업무의 상태 검사 결과다. 합성 요청 수를 유료 모델 호출 수로 표시하지 않는다.

## 구현 위치와 확인한 행동

| 구현 | 확인한 동작 |
| --- | --- |
| [Journal](../../src/agent_memory_eval/recording/journal.py) | blob 선기록/동기화, 순서·자기/연결 해시, 단일 기록자 잠금, tail 보존·append 차단, 보기 재생성 |
| [MemoryService](../../src/agent_memory_eval/memory/service.py) | 불변 버전·CAS·소유권·봉인·snapshot/fork/compose·같은 연산 재전송·다른 모델 call 보존 |
| [MemoryPolicy](../../src/agent_memory_eval/contracts/core.py) | 단계별 쓰기·읽기, 무기억, 명시적 페이지·항목/본문/반환량 한도 |
| [입력 구성과 OfflineGateway](../../src/agent_memory_eval/recording/provenance.py) | 승인 생산자·역할·run·시점에서 요청/manifest 동시 구성, 최종 요청 변형 차단, 자기 작성/반환/재전송 구간 |
| [ToolRouter](../../src/agent_memory_eval/runner/router.py) | 기록된 응답과 call 대조, 혼합 부작용/결과 순서, 종료 batch 실행 전 거절, 업무/기억 전이 분리 |
| [OfflineRunner](../../src/agent_memory_eval/runner/offline.py) | 사전 배정, 새 세션, 2원천 수집·자기/혼합·무기억, 종료 결과 고정 뒤 사후 작성 |
| [합성 분석](../../src/agent_memory_eval/analysis/offline.py) | 원본 해시/출처 재검증, 노출 사건 누락 검출, 배정한 짝·공식 결측·합성 결과·횟수 재계산 |

정상 기억 도구 반환의 JSON content UTF-8 bytes를 누적 한도로 센다. list/get의 본문·제목·메타데이터와 store 성공 ACK도 포함한다. 실패는 한도와 head를 바꾸지 않는다. 항목/본문 한도는 현행 head에 적용하고 과거 버전 파일의 보존량과 구분한다. 목록 순서는 명시적인 생성 순서이며 파일 열거·mtime을 사용하지 않는다. 커서는 불투명하고 은행/읽기 보기/페이지 크기에 묶인다.

은행 생성·봉인·커서·사전 배정 등 내부 상태를 보존하기 위해 **journal envelope v1.1**에 `state_ref`를 추가했다. 내부 상태만 바뀌는 commit은 `events=[]`를 허용한다. 사건 v1.1·입력 manifest와 기존 journal v1.0/fixture는 그대로 보존한다. `commit_seq`는 모든 commit, `event_seq`는 그 안의 사건만 연속 번호를 갖는다. [코어 계약](../design/core-contracts.md)을 따른다.

첫 구현은 commit마다 전체 내부 상태의 불변 blob을 참조한다. 작은 순차 흐름의 정확성을 우선한 방식이며 큰 실험의 저장량·처리 성능은 아직 검증하지 않았다.

공개 경험은 journal의 공개 생산 사건에서 번호와 cutoff를 확인하고, 해당 접두부를 불변 JSONL blob으로 저장한다. 이후 공개 사건이 추가돼도 이전 접두부 bytes는 바뀌지 않는다. 모델 공개 도구 오류는 별도 `policy_result` 출처로 연결하며 내부 사건/채점 자료 전체를 입력으로 직렬화하지 않는다.

## 행동 검증

```bash
uv run python -m unittest discover -s tests
uv run python scripts/validate_design.py
uv run python scripts/check_repository.py
uv run python scripts/verify_legacy_data.py
```

[오프라인 테스트](../../tests/test_offline_core.py)는 실제 임시 파일에서 다음을 확인한다. blob 동기화 예외·로그 확정 전후의 주입 중단, 재열기 후 같은 연산의 중복 확정/차감 방지, 버전/한도 실패의 부분 변경 방지, snapshot/분기 격리, compose 충돌, 커서와 봉인, 혼합 호출·종료 사전 검사, 요청 wrapper의 힌트 차단, 자기 작성 인수와 반환/미전송의 구분, 채점용 표식 차단, 요청 한도 후 채점과 채점 오류 결측을 포함한다.

내구성 검증은 Linux 로컬 파일 연산과 주입한 예외에 한정한다. 전원 차단·디스크 장애·모든 OS/네트워크 파일시스템의 보장을 입증한 것은 아니다. 로그 쓰기/동기화 중 실패한 기록자는 재사용하지 않으며 재열기·검사를 요구한다. 미확정 tail은 자동 삭제하지 않는다. 이미 배포한 업무 batch는 자동 재실행하지 않는다.

## 남은 작업

[OpenRouter·τ² 무기억 연결](tau2-connection.md)은 별도 확인했다. 실제 τ² 기억 통합·AppWorld/BFCL 어댑터, 연구 계획 의미/준비 검사, preload·가공·다른 목록 순서, 누적·교차·개입·통계 분석은 후속이다. 코어 계약의 source/role 검사는 실제 provider wrapper와 어댑터에서도 다시 검증해야 한다. 현재 패키지는 저장소 checkout의 스키마를 사용하며 독립 wheel 배포는 아직 지원 범위가 아니다.

전체 세션 복구·동시 실험 실행과 원격 DB를 추가하지 않았다. 연구 범위와 초기 Luna·τ² 방향은 유지한다. [만들면서 확인할 실증 질문](../design/direction-notes.md#만들면서-확인할-실증-질문)은 후속 파일럿의 질문이며 가짜 작업자의 결과로 답한 것으로 취급하지 않는다.
