# Agent Memory Evaluation 작업 안내

이 저장소는 자율적인 기억 저장·선택·조회가 후속 과제 수행에 미치는 영향을 연구하는 프레임워크의 기준 저장소다. 새 세션은 [인수인계](docs/handoff/next-session.md)를 먼저 읽고, [연구 계획](docs/research/plan.md)·[상세 설계](docs/design/architecture.md)·실제 코드의 상태를 확인한다.

- 현재 파일 코어·가짜 실행기, OpenRouter·τ² [native 기억/두 회차 연결](docs/development/tau2-memory.md)과 [세 조건 자율 조회 파일럿](docs/development/tau2-autonomy-pilot.md)이 동작한다. guided_check·한 과제/고정 은행의 작은 관측을 일반 정책 효과로 보고하지 않는다. 이후 상태는 실제 코드·행동 검증과 실행 기록으로 확인하고 인수인계를 갱신한다. 골격과 합성 계약 검증을 모델 실험이나 기능 완료로 보고하지 않는다.
- 첫 구현은 [공통 코어 작업 명세](docs/development/first-implementation.md)에 따라 오프라인에서 진행할 수 있다. 실제 실행 설정의 미확정 값을 이유로 일반 구현을 멈추거나 임의의 실험 숫자를 확정하지 않는다.
- 첫 구현은 SQLite 없이 파일로 시작하며 [코어 계약](docs/design/core-contracts.md)의 단일 원본 로그·요청 출처·혼합 호출 규칙을 따른다. 추가 DB 설치는 선행 조건이 아니다. 단일 프로세스·순차 쓰기에서 세션·은행 격리와 중단 감지를 유지하고 복잡한 동시 쓰기·전체 세션 복구는 후속으로 둔다.
- 추가 [실험팀 문서 검토](docs/handoff/experiment-handoff-review.md)와 [적용 조건 기억 파일럿](docs/research/condition-sensitive-pilot.md)은 후속 실증 제안이다. 기존 draft·전체 범위를 자동 교체하거나 필터/Jev를 필수 코어로 추가하지 않는다.
- 초기 Luna 작업자·DeepSeek V4.1 Flash 1017 사용자 시뮬레이터·τ² 설정과 AppWorld·BFCL 확장 목표를 유지한다. 실제 역할별 경로/파라미터는 [연결 안내](docs/development/tau2-connection.md)를 따른다. 구현 순서를 이유로 저장·조회·전이·누적·득실·비용의 연구 범위를 줄이지 않는다.
- BM25·임의의 2k 배분을 기본 가정으로 추가하지 않는다. 제약·모델·피드백·과제·반복은 실험 계획에 명시한다.
- 원천 경험·분석 주석·채점 정답과 에이전트 공개 입력을 분리한다. 목록 제목도 기억 노출이며 반환과 입력 포함을 구분한다.
- 원본 실행·과거 실험은 덮어쓰지 않는다. 이전 레포의 라벨을 새 자율 생성 기억의 정답으로 전용하지 않는다.
- [이전 데이터 보관본](docs/data/legacy-datasets.md)은 출처·분할·라벨을 보존한 참고 자료다. 과거 문서·코드를 현재 지침·실행 기본값으로 적용하지 않는다. 보관본을 고치지 말고 재사용 자료를 새 버전으로 만든다.
- 기본 검증은 아래 세 명령이다. 벤치 연결이나 기능을 구현하면 관련 행동 검증을 추가한다.

```bash
uv run python scripts/validate_design.py
uv run python scripts/check_repository.py
uv run python scripts/verify_legacy_data.py
```

계약 검사의 음성 사례와 보관본 손상 탐지는 `uv run python -m unittest discover -s tests`로 확인한다. v1.0 자료는 보존하고 새 구현은 v1.1 계약을 사용한다. 합성 검사 통과는 실제 기록자·요청 구성기·라우터의 구현 완료가 아니다.

계약 의미를 바꾸면 스키마·버전·fixture·관련 문서를 함께 정리한다. 실제 모델·벤치 실행에는 고정된 프로토콜과 사용량·비용·실패 기록이 필요하다. 제한된 무기억 연결 확인과 guided 기억 연결은 [별도 프로필/기록](docs/development/tau2-memory.md)에 연결하며 기억 정책 효과의 실증과 구분한다.

Git 작업은 사용자가 정한 `main` 기준을 따르고 작업 중인 변경을 보존한다. 기본 검증이 보고하는 설계 문서의 이관 후 해시 변경은 정상적인 개발 이력이다. 과거 출처 manifest를 현재 해시에 맞춰 다시 쓰지 않는다. 구체적인 사용자 작업 지시가 이 안내보다 우선한다.
