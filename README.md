# Agent Memory Evaluation

**LLM 에이전트가 경험을 어떻게 저장하고 선택·조회하느냐에 따라 후속 작업의 성공·실패·진척·비용이 어떻게 달라지는지 평가한다.** 연구 제목은 「LLM 에이전트의 메모리 저장·조회 방식에 대한 평가 프레임워크」다.

초기 작업자 모델은 Luna, 초기 실증 환경은 τ²-bench다. 기억 관리·실험 제어·기록·분석을 벤치 실행과 분리하고 AppWorld·BFCL로의 확장을 설계한다. 저장 시점·형태·조회 방식, 재시도·전이·누적 변화와 이득·손해를 연구 범위로 유지한다.

현재는 **파일 기반 기억 서비스·기록자·입력 구성·도구 라우터·가짜 두 회차 실행과 원본 재계산**이 동작한다. [오프라인 코어 안내](docs/development/offline-core.md)에 실행 방법과 행동 검증을 정리했다. [OpenRouter·Luna·τ² 연결](docs/development/tau2-connection.md), [native 기억 두 회차](docs/development/tau2-memory.md)와 [무기억·빈은행·고정 은행의 자율 조회 파일럿](docs/development/tau2-autonomy-pilot.md)을 구현했다. 일반 연구 계획 실행과 정책 효과 실증은 [후속 작업](docs/development/roadmap.md)이다.

## 먼저 읽기

새 개발 세션은 [인수인계](docs/handoff/next-session.md)에서 현재 구현 상태·합의 사항·미정 값을 확인하고, [첫 구현 작업](docs/development/first-implementation.md)부터 이어간다.

첫 구현은 **SQLite 없이 JSON·JSONL 파일로 가볍게 시작한다.** 단일 원본 로그·요청 출처·혼합 호출의 [코어 계약](docs/design/core-contracts.md)과 v1.1 합성 검증을 보완했다. 제한된 연결/조회 파일럿과 일반 실증의 범위를 구분한다. 최근 탐색 질문은 [방향 논의 노트](docs/design/direction-notes.md)에 있다.

추가로 전달받은 실험팀 문서는 [검토·반영 내역](docs/handoff/experiment-handoff-review.md)과 [적용 조건 기억 파일럿 제안](docs/research/condition-sensitive-pilot.md)으로 정리했다. 기존 프로토콜·연구 범위를 유지하며, 제안을 실행 확정이나 성능 결과로 취급하지 않는다.

1. [연구 계획](docs/research/plan.md): 문제·연구 질문·기여 목표·기대 효과와 결정 사항.
2. [실험 프로토콜](docs/research/experiment-protocol.md): 두 회차·자기 경험과 혼합 은행·누적·교차·개입 비교.
3. [측정과 분석](docs/research/metrics.md): 성공·회복·회귀·진척·기억 품질·노출·비용의 정의와 해석.
4. [관련 연구](docs/research/related-work.md): 기존 벤치·정책 연구의 초점과 기여 비교.
5. [상세 설계](docs/design/architecture.md): 모듈·도구·은행·어댑터·상태·오류·재개 계약.
6. [보고서 네 장 구성](docs/handoff/report-outline.md): 연구 설명을 발표·보고서로 전개할 때 사용할 내용.

## 빠른 확인

Python 3.11 이상과 uv를 사용한다. `.python-version`은 초기 개발 환경인 Python 3.12 계열을 지정한다.

```bash
uv sync --frozen
uv run python scripts/validate_design.py
uv run python scripts/check_repository.py
uv run python scripts/verify_legacy_data.py
uv run python -m unittest discover -s tests
```

첫 명령은 잠금 파일의 개발 환경을 구성한다. 검증기는 로컬 파일·스키마·참조·해시를 검사하며 모델 API·벤치·GPU를 실행하지 않는다. 이전 연구 레포를 설치하거나 가중치를 받을 필요가 없다. unittest는 실제 오프라인 코어의 행동, v1.1 계약 음성 사례와 보관본 손상 탐지를 확인한다.

새 출력 디렉터리에서 가짜 두 회차 흐름을 실행하고 같은 원본을 재분석할 수 있다.

```bash
uv run python scripts/run_offline_demo.py --output runs/offline-example
uv run python scripts/run_offline_demo.py --output runs/offline-example --analyze-only
```

## 구조

```text
src/agent_memory_eval/   # 파일 코어·가짜 실행·원본 재계산과 후속 어댑터 위치
schemas/                # 설정·사건 계약
protocols/              # 초안과 향후 고정할 실험 계획
tests/fixtures/         # 작은 합성 계약 자료
scripts/                # 현재 제공하는 오프라인 검증기
docs/                   # 연구·설계·개발·보고서 설명
artifacts/              # 원본 위치·버전·해시 등 작은 manifest
archive/                # 과거 데이터·설계 근거의 고정 보관본
```

τ²·AppWorld·BFCL 의존성은 어댑터 구현 시 별도로 선택·고정한다. 기본 환경에 과거 SWE 실행·판독기 학습·GPU 라이브러리를 포함하지 않는다. 원시 실행·다운로드·가중치는 Git 밖의 `runs/`, `data/`, `outputs/`에 보존하고 공유 자료의 연결은 manifest로 관리한다.

## 출처와 작업 원칙

이 저장소는 기존 `ai-memory-lab`의 커밋 `d0044c6367e17c1488db4dae37f44e5b358342f9`에서 중요한 설계·계약을 선별해 재구성했다. [이관 내역](docs/migration.md)과 [출처 manifest](artifacts/legacy-source-manifest.json)에 원본 경로·해시·변환과 제외 이유를 기록한다.

후속 요청으로 기존 datasets 전체와 설계 자료·생성 코드·설명 문서도 [보관본](docs/data/legacy-datasets.md)에 복사했다. 1,268파일·약 202.26MiB이며, 미커밋 작업 트리의 원본 bytes·Git 상태는 [별도 manifest](artifacts/legacy-datasets-manifest.json)로 보존한다. 원본 실행·학습 출력·가중치는 기존 위치에 남겼다.

새 프레임워크의 설계·구현·프로토콜은 이 저장소에서 관리한다. 보관 자료의 라벨·정책·예산을 새 실험에 자동 적용하지 않는다. 기여 목표와 관측된 결과, 기억 내용의 정확성과 실제 효용을 구분한다. 작업 방법은 [협업 안내](CONTRIBUTING.md)를 따른다.
