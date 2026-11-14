# 공동연구 작업 안내

현재 연구 질문과 결과는 [README](README.md), 문서의 읽는 순서는 [docs](docs/README.md), 실행 요구 사항은 [재현 안내](docs/reproduction.md)를 따른다.

## 작업 영역

| 영역 | 맡을 일 |
| --- | --- |
| `experiments/` | 고정 에이전트·기억 정책·배정·채점·비용 |
| `data_pipeline/` | 새 수집·정규화·사례 생성·주석·분할 코드 |
| `datasets/` | 버전이 고정된 데이터와 입력·라벨·출처 계약 |
| `training/` | 모델·출력층·손실·학습 설정·Colab 실행 |
| `evaluation/` | 저장 예측 평가와 공통 검증 진입점 |
| `docs/` | 연구 판단·실험 설계·결과와 읽는 순서 |

기존 실행 코드의 경로는 고정 계획·업로드 코드·검산과 연결돼 있다. 새 기능은 역할에 맞는 영역에 추가하고, 공통화할 때 기존 진입점과 실행 당시 사본의 연결을 유지한다. 측정 환경과 GPU 학습 환경은 각자의 고정 의존성을 사용한다.

## 새 실험과 데이터

- 새 프로토콜·데이터·실행 ID를 정하고 입력, 분할, 모델 revision, seed, 예산과 중단 기준을 기록한다.
- 같은 출처·과제·파생 변형은 같은 분할 그룹에 둔다. 이미 본 자료의 재검토는 개발·진단으로 표시한다.
- 정답·주석·미래 결과를 예측 입력과 분리한다. 판단 시점 이후에 얻은 관측을 시작 시 선택기의 특징으로 쓰지 않는다.
- 관계 라벨, 실제 행동 결과, 제공·보류의 평균 효과는 다른 필드·평가로 유지한다. 미확정 값과 관측 실패를 구별한다.
- 데이터 변경은 새 버전 디렉터리에 만든다. `artifact-index.json`이 있는 자료와 그 자료가 참조하는 원본은 덮어쓰지 않는다.

## 문서와 결과

새 보고서는 질문·사용 데이터·고정한 비교·결과·한계·재현 경로를 함께 적고 [결과 색인](docs/reports/README.md)에 연결한다. 다음 계획은 실행 결과와 구분한다. 과거 노트의 숫자를 최신 결론으로 바꾸기보다 새 보고서와 변경 이유로 연결한다.

보고서마다 원본 경로·해시, 실제 실행 코드, 데이터와 모델 버전, 관측 누락을 보존한다. 중단·실패 실행도 실행 목록에 남기고, 반복·파생 행을 독립된 표본처럼 합산하지 않는다.

## 변경 후 확인

```bash
python3 -m data_pipeline.relations check
python3 -m evaluation.reproduce_relations
python3 -m unittest discover -s evaluation/tests -v
python3 -m unittest discover -s training/memory_selector/tests -v
```

에이전트 코드를 바꿨다면 `uv sync --frozen` 후 `uv run python -m unittest discover -s experiments/swe_memory/tests -v`도 실행한다. GPU·모델 API를 쓰는 실행은 명시된 별도 명령으로 시작하며, 종료 뒤 결과 회수와 자기 Colab 세션 종료 여부를 확인한다.

Git에는 코드·설정·고정된 작은 자료·보고서와 공유 예측을 둔다. `runs/`, 학습 `outputs/`, 생성된 학습 `data/`, 계정 설정은 별도 로컬 자료다. 추가 원본이 필요한 검산은 [재현 수준과 전달 묶음](docs/reproduction.md)에 요구 사항을 적는다.
