# 고정 commit으로 과거 실험 재검증

2026-11-16 · P0 실행기 · 과거 결과의 오프라인 재현

[재현 registry](../../protocols/reproductions/historical-v1.json)는 실행 구현과 분석 재현 commit을 구분한다. [이력 대조](../../artifacts/analyses/20261116-reproduction-commit-candidates.json)로 확인한 전달 비교 `65a757f`, 무기억 `7243fef`, 결정 재전송 `c51165b`와 원인 진단 `c31a9d6`을 사용한다. 과거 lock·원본·분석을 수정하지 않고, 현재 작업 트리의 코드 해시가 과거와 같을 것을 요구하지 않는다.

## 실행

준비 검사는 Git blob의 동결 참조·분석 코드·잠금 파일과 원본 입력 해시·활성 writer를 확인한다. 작업 폴더나 환경을 만들지 않으며 모델·catalog를 호출하지 않는다.

```bash
uv run --frozen python scripts/reproduce_experiment.py --experiment decision_replay
```

실제 재현은 새 출력 폴더와 `--run`을 지정한다. `--experiment`는 `delivery`, `baseline`, `decision_replay`, `baseline_diagnosis` 중 하나다.

```bash
uv run --frozen python scripts/reproduce_experiment.py --experiment delivery --run --output outputs/reproductions/delivery-01
uv run --frozen python scripts/reproduce_experiment.py --experiment baseline --run --output outputs/reproductions/baseline-01
uv run --frozen python scripts/reproduce_experiment.py --experiment decision_replay --run --output outputs/reproductions/decision-replay-01
uv run --frozen python scripts/reproduce_experiment.py --experiment baseline_diagnosis --run --output outputs/reproductions/baseline-diagnosis-01
```

`--inputs-root`는 원본을 복원한 저장소 형태의 폴더로 바꿀 때 사용한다. 기존 출력 폴더는 덮어쓰지 않는다. `--live`, 원본 재시도·재개, 주석 재작성 모드는 제공하지 않는다. 프로세스 timeout 기본 1800초는 이 오프라인 재현 실행기의 한도이며 연구 프로토콜의 실행 숫자가 아니다.

## 격리와 저장량

1. 지정한 commit의 detached worktree를 만든다. Git branch나 현재 미커밋 변경을 checkout하지 않는다.
2. 원본 run의 `writer.lock`에 공유 잠금을 잡고 입력 manifest를 만든다. 독립 파일 사본을 worktree에 넣는다. raw symlink·원본 hardlink·추적 파일 덮어쓰기를 거부한다. 진단의 native 소스는 로컬 Git clone으로 독립 준비하고 원래 checkout 안을 가리키는 추적 symlink만 보존한다. checkout 밖으로 나가는 링크는 거부한다.
3. 그 commit의 `uv.lock`으로 worktree 전용 `.venv`를 `uv sync --frozen --offline --no-python-downloads`로 준비한다. 결과 검토 세 개는 기본 의존성만 설치하고, 로컬 도구 재생을 하는 원인 진단은 고정 `tau2` extra를 설치한다. 로컬 uv cache가 부족하면 실패를 기록하고 자동으로 네트워크 설치하지 않는다.
4. 전용 Python `-I -B`에서 과거 review 스크립트의 기본 검증 모드만 실행한다. 실제 패키지 import 경로·Python/설치 패키지·assert 활성 상태를 기록하고 API 역할 키를 전달하지 않는다.
5. Python 감사 hook으로 등록된 검토 스크립트의 네트워크·중첩 프로세스·worktree 쓰기를 차단한다. 원인 진단만 등록한 native clone에 대한 정확한 `git rev-parse HEAD`·`git status --porcelain`을 허용하고 기록한다. Git 선택적 인덱스 갱신은 끈다. 시도를 잡아먹고 계속한 스크립트도 실패로 기록한다. 이는 신뢰하는 과거 스크립트의 우발적 I/O를 막는 장치이며 적대적 코드를 격리하는 OS 보안 sandbox가 아니다.
6. 원본·복사 입력·동결 코드·저장 결과를 다시 대조한다. 기본으로 worktree·복사한 raw·`.venv`를 제거하고 receipt·manifest·로그·child 환경 검증만 남긴다. 점검이 필요하면 `--keep-worktree`를 명시한다.

전달 비교의 기존 분석은 guided 기억 원천 raw와 고정 τ² 데이터 파일도 필요하다. registry에 둘 다 기록했다. baseline·재전송은 원본 묶음 안에 감사 자료가 있어 별도 native 설치나 과거 baseline 원본이 각각 추가로 필요하지 않는다. 세 실행의 입력 요구를 하나로 가정하지 않는다.

원인 진단은 고정 τ² 소스·설치 버전이 필요하며, 내부 도구와 DB/채점 재생은 메모리 내 사본에서 수행한다. 모델 호출·새 성능 실험·원래 DB/파일의 수정과 구분한다. 과거 스크립트끼리의 import는 그 worktree의 `scripts/`만 추가해 해결한다.

## 결과와 검증

출력 `receipt.json`은 실행/분석 commit·registry/코드/입력 해시·환경·명령·판정·원본 보존·정리 결과를 남긴다. `child-report.json`에는 실제 import 경로와 네트워크/쓰기 시도 수가 있다. 실패와 timeout도 로그·receipt를 보존한다. 완료된 실험의 원본 journal에 가짜 run이나 기억 사건을 추가하지 않는다.

[행동 테스트](../../tests/test_historical_reproduction.py)는 실제 작은 Git 저장소·worktree·격리 Python으로 main 변경, 잘못된 import, 원본/해시/입력 오류, 활성 writer, 네트워크·쓰기·프로세스 시도, timeout·정리를 확인한다. 합성 테스트의 의존성 설치는 표준 라이브러리 fixture이며 실제 세 과거 명령의 uv 환경 검증과 구분한다.

```bash
uv run --frozen python -m unittest discover -s tests -p 'test_historical_reproduction.py'
```

과거 결과 재현과 최신 코드의 구버전 로그 호환 검사는 서로 다른 책임이다. 현재 코드를 고칠 때는 기존 raw에 대한 최신 감사도 확인하되, 분석 버전·판정 기준을 바꾼 결과를 과거 receipt에 덮어쓰지 않는다. P1a의 성능 수정 뒤에도 여기의 고정 commit 재현은 그대로 사용한다.
