# 협업 안내

연구 범위는 [연구 계획](docs/research/plan.md), 비교와 통제는 [실험 프로토콜](docs/research/experiment-protocol.md), 구현 경계는 [상세 설계](docs/design/architecture.md)를 따른다.

새 세션은 [인수인계](docs/handoff/next-session.md)와 [첫 구현 작업](docs/development/first-implementation.md)에서 실제 상태와 다음 작업을 확인한다.

## 작업 영역

| 위치 | 역할 |
| --- | --- |
| `src/agent_memory_eval/` | 실행·기억·정책·모델·벤치 어댑터·기록·분석의 공통 코드 |
| `schemas/` | 설정·사건의 형식 계약과 버전 |
| `protocols/` | 모델·과제·조건·배정·중단·통계의 고정 계획 |
| `tests/` | 작은 합성 자료와 기능 구현 후의 행동 검증 |
| `scripts/` | 저장 파일만 읽는 검증과 개발 보조 |
| `docs/` | 연구 판단·설계·개발 순서·결과 해석 |
| `artifacts/` | 원본 위치·버전·해시·제공 범위의 작은 manifest |
| `archive/` | 원본 bytes·라벨·분할을 유지하는 과거 데이터 보관본 |

## 변경 확인

```bash
uv sync --frozen
uv run python scripts/validate_design.py
uv run python scripts/check_repository.py
uv run python scripts/verify_legacy_data.py
uv run python -m unittest discover -s tests -p 'test_legacy_data_archive.py'
```

현재 검증은 스키마·합성 사건·파일 참조·문서·이관 출처에 한정된다. 실행기 구현 이후에는 세션·은행 격리, 도구 정책, 실제 입력 노출, 채점·오류·비용, 재개와 집계를 검증한다. 과거 저장소의 학습·SWE 전체 테스트를 새 저장소의 기본 검증에 연결하지 않는다.

보관본 검사는 파일 목록·bytes·해시와 손상 탐지를 확인하며 과거 실험이나 프레임워크 행동을 검증하지 않는다. [재사용 안내](docs/data/legacy-datasets.md)에 따라 새 자료는 새 버전으로 만들고 원본 보관본을 고치지 않는다. 이관 당시 설계 manifest의 해시는 역사적 값이며 일반 문서 수정으로 달라진 현재 해시에 맞춰 갱신하지 않는다.

## 기록과 재현

실험마다 새 계획 revision·experiment ID·run ID를 정하고 모델·제공 경로·벤치 commit·과제·프롬프트·seed·중단 조건을 보존한다. 미래 관측과 정답을 모델 입력에 넣지 않는다. 같은 출처의 파생 과제·은행·호출을 독립 표본으로 합산하지 않는다.

성공·실패·채점 결측과 요청 오류를 구분한다. 비용·사용량의 미확인을 0으로 채우지 않는다. 원본과 분석 revision은 보존하고 합성 예시를 실제 성능 표에 포함하지 않는다. 세부 계약은 [기록 설계](docs/design/recording.md)를 따른다.

공통 설치는 가볍게 유지한다. 벤치 의존성과 모델 실행 환경은 실제 연결 시 선택·고정하고 공식 채점 의미를 검증한다. 계약의 의미를 변경하면 버전과 호환·이관 방식을 함께 갱신한다.
