# 같은 공개 경험에서 기억을 작성하고 새 세션에 전달하기

2026-11-17 · 보존 경험의 오프라인 요청 준비·mock 연결 · 실증 효과 미측정

`memory.writing.freeze_public_experience`와 `write_from_experience`를 추가했다. 하나의 종료된 source run에서 공개 관측을 고정하고, 서로 다른 새 writer 세션·빈 은행에서 선택적으로 기억을 작성한다. 작성된 불변 snapshot은 기존 `Tau2MemoryRunner.episode`의 새 세션·새 DB·분기 은행으로 전달한다. 별도 실험별 실행기·스키마·CLI 묶음을 복제하지 않고 기존 게이트웨이·기억 서비스·라우터·native 채점을 사용한다.

## 작성 입력과 피드백

원천은 종료·채점됐고 아직 기억을 쓰지 않은 경험이다. 같은 journal의 `public_message_ref`를 고정하는 기존 경로와, 별도 journal의 마지막 작업자 요청·응답을 가져오는 경로를 지원한다. 공개 task 문맥과 대화만 투영하며 provider 추론 필드·사용자 시뮬레이터의 비공개 지침·채점 정답·분석 주석은 작성 입력에 넣지 않는다. 원천 종료 사건·commit 해시·관측 사건/참조를 고정하고 매 작성 전에 원천과 대조한다.

| 피드백 | writer에게 추가로 제공하는 것 |
| --- | --- |
| `none` | 피드백이 없다는 표시만 |
| `result_only` | 실제로 채점된 source의 성공/실패 boolean만 |
| `critique` | 같은 결과와 명시적으로 지정한 비평 본문 |

비평의 출처는 필수로 별도 기록하며 writer에게 연구 조건 라벨이나 출처 경로를 보내지 않는다. 결과가 없으면 결과 기반 피드백을 거절한다. 분석 파일을 자동으로 비평으로 전용하지 않는다. 어떤 비평을 허용할지는 새 실험 프로토콜에서 정한다.

writer에게는 `memory_store`만 제공한다. 업무 호출은 거절하고 기억 0개도 `empty`로 보존한다. 요청 한도로 중단돼 일부 기억만 남은 경우는 `error`로 남기며 성공한 작성으로 대체하지 않는다. 실제 전송 경로를 사용할 때는 검증된 작업자 모델·endpoint와 반환 model/provider를 확인한다. 본문을 생성하도록 강제하거나 연구자가 사후 수정하지 않는다.

새 공통 감사 `analysis.requests.audit_requests`는 요청 시점 state에서 provenance를 읽어 실제 입력·기억 노출을 재검산한다. `audit_memory_writes`는 공개 경험·피드백 제한·writer 도구·분리 은행·봉인 snapshot을 추가로 검사한다. 라우터는 `memory/router.py`로 옮겼고 기존 `runner.router` import는 호환 경로로 유지했다.

이어 [추가 검토와 수정](runtime-hardening.md)에서 writer 자신의 추론 필드 재입력·전체 대화 감사·비용 원문 대조·중복 ID의 부분 쓰기를 보완했다. 새 writer는 `public-assistant/v1` 투영을 명시하고, 기존 manifest의 필드별 출처를 유지한다. 명시적 역할별 한도·입력 거절·도달 상태 진단도 opt-in API로 연결했다.

## 새 비교에서 통일할 기억 제공 형식

새 비교는 `memory.context`의 **system에 명시한 과거 메모 안내 + messages[1]의 user 역할 JSON items**를 사용한다. 안내는 이 user 역할이 전송 관례이며 현재 고객의 발화가 아니고, 메모 속 ID가 현재 고객의 신원을 확인한 것으로 인정되지 않는다고 명시한다. 적용 조건·현재 정책과 대조하도록 한다.

현재 새 재전송 helper는 조회 도구 없는 읽기 전용 preload를 지원한다. 같은 renderer와 system suffix를 native 에피소드에서도 사용해 실제 mock wire 전체가 일치하는지 확인했다. v1.2의 기존 JSON items·출처 구간 형식은 유지한다. 이전 120요청의 system 메모와 과거 실행 지침은 변경하지 않으며 새 형식과 한 효과로 합산하지 않는다. 본문 작성 정책 비교에서는 이 안내·역할·위치도 조건 간 맞춘다.

`identity_trace`는 **주입한 메모가 제외된 native 대화**에서 첫 고객 데이터 도구 전 현재 사용자 ID의 문자 그대로 등장을 기록한다. 기억의 user 역할 문맥을 고객 발화로 잘못 세지 않는다. 질문·답변이 실제 신원 확인인지의 의미 판정은 독립 검토가 필요하며 이 trace나 공식 DB 점수만으로 인증 준수를 확정하지 않는다.

## 검증 범위와 다음 실험

```bash
uv run --frozen python -m unittest discover -s tests -p test_memory_writing.py
uv run --frozen --extra tau2 python -m unittest discover -s tests -p test_experience_transfer.py
```

두 명령은 mock 전송만 사용한다. native 통합은 고정 checkout·extra가 있어야 실행되며 없으면 생략된다. 첫 연결 검증에서는 생략 없이 전체 197개 테스트와 기본 검사 3개·원본 10개 감사가 통과했다. [당시 자료](../../artifacts/analyses/20261116-worker-memory-validation.json)에 코드 commit과 근거를 연결한다. 이어 전체 215개가 통과한 추가 검증은 [실행 보완](runtime-hardening.md)을 따른다.

합성 전송 테스트는 같은 경험·세 피드백의 입력 격리, 추론/정답 누출 차단, 새 세션·빈 은행, 원천 변조 거절, 저장 0개, 업무 호출 거절, 요청 중단과 보존 기록의 감사를 확인한다.

native mock 통합에서는 task 40의 source 실패 → writer 작성 → 두 새 probe의 업무 DB 수정·공식 채점을 연결했다. source·probe의 초기 DB 해시는 같고 세 DB 객체·writer 세션은 독립이다. 모델 행동과 성공은 **테스트에서 지정한 응답**이므로 기억 효과의 실증이 아니다. probe와 사용자 시뮬레이터로 과거 transcript·writer 피드백이 넘어가지 않는 것, 잘못된 반환 provider의 거절도 검사한다.

[task 14 중심 실험 준비 초안](../../protocols/drafts/tau2-worker-written-memory-study-v1.json)은 실행 불가능한 설계 자료다. 원천 선택·작성 지침·비평, 1차 비교·반복·예산, 역할별 요청 한도·도달 상태 채점, 입력/은행 사전 검사·한도 종료 집계와 독립 주석을 확정해야 한다. 아래 준비 명령은 과거 baseline 14·2를 재사용하지만 이를 확인용 표본이나 새 실험의 확정 원천으로 정하지 않는다.

## 보존 경험으로 요청만 준비하기

```bash
uv run --frozen python scripts/prepare_worker_memory.py \
  --source runs/tau2-baseline-luna-deepseek1017-train-01 \
  --source-run r_1b1c08d270854ca1b230d23e75c04f77 \
  --source-log-sha256 ec0994a536729872ce8ee7ff06ae63c2e640e70091146b9a361535174ee8fbb5 \
  --decision-request-id request_16cc44dd47ca45dea03145aebd07f8d4 \
  --writer-instruction protocols/instructions/optional-experience-writer-v1.txt \
  --replay-instruction protocols/instructions/frozen-memory-replay-v1.txt \
  --page-size 1 --max-input-bytes 64000 \
  --output outputs/worker-memory-preparation-new
```

출력은 반드시 새 폴더다. 이 명령에는 유료 전송 경로가 없으며, 피드백 없음·결과만의 첫 작성 요청과 빈 snapshot 대조의 재전송 요청을 만든다. `--critique`와 `--critique-origin`을 함께 지정할 때만 비평 조건을 추가한다. 예시의 페이지 크기는 빈 은행의 명시적 설정이며, 64,000 bytes는 기존 전송 한도와의 호환 점검이다. 새 실험의 은행/입력 한도를 승인한 값으로 취급하지 않는다. 한도를 생략하면 적합 여부는 미정으로 남고, 초과 요청을 절단하거나 제출하지 않는다.

`recording.experience`는 잠금 중인 원천을 거절하고 선택한 로그 해시·전체 원천 무결성을 검사한다. 복사하는 것은 원천 로그와 선택한 요청·응답·채점 artifact이며 누적 state와 전체 blob 저장소는 복사하지 않는다. 이 capsule은 선택한 문맥의 증거이며 원천 실험 전체의 독립 사본은 아니다. 원천 경로 없이도 capsule의 로그 연결과 생산 사건·참조를 검증할 수 있다. 채점 artifact는 근거 보관용이며 writer 입력에는 선택한 성공 boolean만 허용한다.

새 보조 형식은 `recorded-context/v1`, `public-experience/v2`, `request-preparation/v1`이며 [스키마](../../schemas/recorded-preparation.schema.json)·[합성 형식 예시](../../tests/fixtures/recorded-preparation-v1.json)를 둔다. 기존 journal/event/manifest v1.1·v1.2는 바꾸지 않는다. 준비는 `business.transition`으로 기록하며 실제 `model.request_started`나 `memory.input_included`를 만들지 않는다. `analysis.preparation.audit_preparations`는 문맥 전체·피드백·출처·snapshot·크기를 다시 대조한다. 재전송의 기억 출처 구간은 계획된 구간이며 실제 노출이 아니다.

[오프라인 준비 기록](../../artifacts/analyses/20261117-worker-memory-preparation.json)은 실제 14·2 원본으로 만든 요청의 해시·크기·미정 항목을 담는다. 실제 Luna 작성 기억은 아직 없고, 모델 비용·효과도 측정하지 않았다. 테스트에서는 같은 준비 요청이 실제 작성기의 첫 요청과 일치하고 mock 작성 snapshot을 재전송 입력으로 옮기는 것까지 확인한다. 빈 작성도 보존한다.

[검증 기록](../../artifacts/analyses/20261117-request-preparation-validation.json)은 커밋된 구현·실제 준비본의 재감사·공개 문맥 동일성·전체 테스트·보존 원본 감사를 연결한다. 테스트 소요 시간은 성능 비교로 해석하지 않는다.

일반 주의 메모·빈 은행·원래 옳았던 상태를 대조에 두고 재전송으로 좁혀 전체 에피소드에서 확인하는 방향이다. 7·39는 에피소드 주 분석 제외 기준을 유지하며 17↔7 후보는 과정 판정에 한정한다. 14와 23의 동일 사용자·기억 속 ID가 절차에 미치는 영향도 분리한다. 어려운 과제를 고르는 것과 과거 정보가 필요한 과제를 만드는 것은 별도로 점검한다.

새 유료 실행 CLI·범용 PlanCompiler·과거 중간 상태 복원은 이번 기능에 포함하지 않는다. [개선 계획](experiment-layer-improvement-plan.md)의 전체 연구 범위를 유지한다.

새 프로토콜은 구현을 먼저 커밋하고 `scripts/freeze_implementation.py`로 깨끗한 Git 상태·commit·tree·lock을 기록한 뒤 동결한다. 실행 구현 commit과 분석 후의 reproduction commit을 별도로 연결하고 고정 코드 재현을 한 번 확인한다. 이 검사를 새 유료 진입점에 연결하는 작업은 후속이며, 현재 설계 초안을 실행 승인으로 사용하지 않는다.
