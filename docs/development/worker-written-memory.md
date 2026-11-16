# 같은 공개 경험에서 기억을 작성하고 새 세션에 전달하기

2026-11-16 · 실제 기록·기억·native 연결의 mock 검증 · 실증 효과 미측정

`memory.writing.freeze_public_experience`와 `write_from_experience`를 추가했다. 하나의 종료된 source run에서 공개 관측을 고정하고, 서로 다른 새 writer 세션·빈 은행에서 선택적으로 기억을 작성한다. 작성된 불변 snapshot은 기존 `Tau2MemoryRunner.episode`의 새 세션·새 DB·분기 은행으로 전달한다. 별도 실험별 실행기·스키마·CLI 묶음을 복제하지 않고 기존 게이트웨이·기억 서비스·라우터·native 채점을 사용한다.

## 작성 입력과 피드백

원천은 같은 journal 안에서 종료·채점된 run이며, 아직 기억을 쓰지 않은 경험을 사용한다. 공개 task 문맥과 solve 단계의 `public_message_ref`만 투영한다. provider 추론 필드·사용자 시뮬레이터의 비공개 지침·채점 정답·분석 주석은 작성 입력에 넣지 않는다. 원천 종료 사건·commit 해시·관측 사건/참조를 별도 manifest에 고정하고 매 작성 전에 원천과 다시 대조한다.

| 피드백 | writer에게 추가로 제공하는 것 |
| --- | --- |
| `none` | 피드백이 없다는 표시만 |
| `result_only` | 실제로 채점된 source의 성공/실패 boolean만 |
| `critique` | 같은 결과와 명시적으로 지정한 비평 본문 |

비평의 출처는 필수로 별도 기록하며 writer에게 연구 조건 라벨이나 출처 경로를 보내지 않는다. 결과가 없으면 결과 기반 피드백을 거절한다. 분석 파일을 자동으로 비평으로 전용하지 않는다. 어떤 비평을 허용할지는 새 실험 프로토콜에서 정한다.

writer에게는 `memory_store`만 제공한다. 업무 호출은 거절하고 기억 0개도 `empty`로 보존한다. 요청 한도로 중단돼 일부 기억만 남은 경우는 `error`로 남기며 성공한 작성으로 대체하지 않는다. 실제 전송 경로를 사용할 때는 검증된 작업자 모델·endpoint와 반환 model/provider를 확인한다. 본문을 생성하도록 강제하거나 연구자가 사후 수정하지 않는다.

새 공통 감사 `analysis.requests.audit_requests`는 요청 시점 state에서 provenance를 읽어 실제 입력·기억 노출을 재검산한다. `audit_memory_writes`는 공개 경험·피드백 제한·writer 도구·분리 은행·봉인 snapshot을 추가로 검사한다. 라우터는 `memory/router.py`로 옮겼고 기존 `runner.router` import는 호환 경로로 유지했다.

## 새 비교에서 통일할 기억 제공 형식

새 비교는 `memory.context`의 **system에 명시한 과거 메모 안내 + messages[1]의 user 역할 JSON items**를 사용한다. 안내는 이 user 역할이 전송 관례이며 현재 고객의 발화가 아니고, 메모 속 ID가 현재 고객의 신원을 확인한 것으로 인정되지 않는다고 명시한다. 적용 조건·현재 정책과 대조하도록 한다.

현재 새 재전송 helper는 조회 도구 없는 읽기 전용 preload를 지원한다. 같은 renderer와 system suffix를 native 에피소드에서도 사용해 실제 mock wire 전체가 일치하는지 확인했다. v1.2의 기존 JSON items·출처 구간 형식은 유지한다. 이전 120요청의 system 메모와 과거 실행 지침은 변경하지 않으며 새 형식과 한 효과로 합산하지 않는다. 본문 작성 정책 비교에서는 이 안내·역할·위치도 조건 간 맞춘다.

`identity_trace`는 **주입한 메모가 제외된 native 대화**에서 첫 고객 데이터 도구 전 현재 사용자 ID의 문자 그대로 등장을 기록한다. 기억의 user 역할 문맥을 고객 발화로 잘못 세지 않는다. 질문·답변이 실제 신원 확인인지의 의미 판정은 독립 검토가 필요하며 이 trace나 공식 DB 점수만으로 인증 준수를 확정하지 않는다.

## 검증 범위와 다음 실험

```bash
uv run --frozen python -m unittest discover -s tests -p test_memory_writing.py
uv run --frozen --extra tau2 python -m unittest discover -s tests -p test_experience_transfer.py
```

두 명령은 mock 전송만 사용한다. native 통합은 고정 checkout·extra가 있어야 실행되며 없으면 생략된다. 이 환경에서는 생략 없이 전체 197개 테스트와 기본 검사 3개·원본 10개 감사가 통과했다. [검증 자료](../../artifacts/analyses/20261116-worker-memory-validation.json)에 코드 commit과 검사·측정 근거를 연결한다.

합성 전송 테스트는 같은 경험·세 피드백의 입력 격리, 추론/정답 누출 차단, 새 세션·빈 은행, 원천 변조 거절, 저장 0개, 업무 호출 거절, 요청 중단과 보존 기록의 감사를 확인한다.

native mock 통합에서는 task 40의 source 실패 → writer 작성 → 두 새 probe의 업무 DB 수정·공식 채점을 연결했다. source·probe의 초기 DB 해시는 같고 세 DB 객체·writer 세션은 독립이다. 모델 행동과 성공은 **테스트에서 지정한 응답**이므로 기억 효과의 실증이 아니다. probe와 사용자 시뮬레이터로 과거 transcript·writer 피드백이 넘어가지 않는 것, 잘못된 반환 provider의 거절도 검사한다.

[task 14 중심 실험 준비 초안](../../protocols/drafts/tau2-worker-written-memory-study-v1.json)은 실행 불가능한 설계 자료다. 새 source 수집, 작성 지침·비평, 1차 비교·반복·예산, 역할별 요청 한도·도달 상태 채점, 입력/은행 사전 검사·한도 종료 집계와 독립 주석을 확정해야 한다. 과거 baseline 14·2는 분석 참고이며 현재 API는 그 원본의 `model_message_ref`/native report를 자동 import하지 않는다. 새 native source 수집 경로는 연결돼 있다.

일반 주의 메모·빈 은행·원래 옳았던 상태를 대조에 두고 재전송으로 좁혀 전체 에피소드에서 확인하는 방향이다. 7·39는 에피소드 주 분석 제외 기준을 유지하며 17↔7 후보는 과정 판정에 한정한다. 14와 23의 동일 사용자·기억 속 ID가 절차에 미치는 영향도 분리한다. 어려운 과제를 고르는 것과 과거 정보가 필요한 과제를 만드는 것은 별도로 점검한다.

새 유료 실행 CLI·범용 PlanCompiler·과거 중간 상태 복원은 이번 기능에 포함하지 않는다. [개선 계획](experiment-layer-improvement-plan.md)의 전체 연구 범위를 유지한다.

새 프로토콜은 구현을 먼저 커밋하고 `git status --porcelain`이 비어 있음을 확인한 뒤 동결한다. 실행 구현 commit과 분석 후의 reproduction commit을 별도로 연결하고 고정 코드 재현을 한 번 확인한다. 이 절차를 새 유료 진입점에서 강제하는 도구는 후속이며, 현재 설계 초안을 실행 승인으로 사용하지 않는다.
