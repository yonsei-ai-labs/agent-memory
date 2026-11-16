# 팀 실행 원본 공유와 복원

2026-11-16 · 결정 지점 120요청 묶음 추가·업로드·재다운로드·복원 검증 완료

코드·문서·작은 관측 receipt는 비공개 Git 저장소에서 관리하고, 큰 실행 원본은 사용자가 지정한 Google Drive 팀 폴더에 공유한다. 실행 중 쓰기는 로컬에서 유지한다. 기존 실행이나 팀 폴더의 다른 자료를 덮어쓰지 않았고 공유 권한도 변경하지 않았다.

## 새 결정 지점 120요청 묶음

| 항목 | 값 |
| --- | --- |
| 공유 폴더 | [agent-memory-decision-replay-20261116-c51165b](https://drive.google.com/drive/folders/1hr7bEjZeAGegNeJiBouaaJOyYC05xk-O) |
| 압축 파일 | [agent-memory-decision-replay-20261116-c51165b.tar.gz](https://drive.google.com/file/d/19sXM2WEhxaqJx6QgNie5FIwwSQqEdLKF/view?usp=drivesdk) |
| 기준 코드 commit | `c51165b7f7ba51da8959591079b75bab4f7fec3c` |
| 압축 크기 | 3,023,043 bytes, 약 3.0MB |
| 포함 자료 | 새 raw run 하나와 코드·문서·작은 자료의 commit 사본, 1,074개 파일·17,634,291 bytes |
| 업로드·검증 기록 | [120요청 공유 registry](../../artifacts/data-releases/20261116-decision-replay.json) |

압축 SHA-256은 `d6d5b0f49574d1ef71209ee0dfcf05b39c8790d883f864de1a0265f9f21a4e3f`다. 120/120 정상 응답·보고 비용 $0.043359345이며 오류·생략·비용 미확인은 없다. 업무 도구·사용자 시뮬레이터를 실행하지 않은 한 응답 진단으로 [결과 해석](../reports/20261116-decision-replay-results.md)을 따른다. 아래 이전 보관본은 유지했다.

안내·해시·manifest를 포함한 7개 파일을 모두 재다운로드해 원본과 대조했다. 새 빈 폴더에 이 묶음만 복원하고 1,074개 포함 파일의 바이트·해시를 확인한 뒤, API 역할 키와 이전 baseline raw 없이 저장 결과·주석 재계산이 통과했다. API 키·`.git`·의존성 환경·다른 raw·`archive/`는 포함하지 않았다. 폴더 권한은 바꾸지 않았으며 별도 팀원 계정의 접근 시험은 하지 않았다.

이 묶음의 `RESTORE.md`로 먼저 압축·포함 파일을 검증한다. 복원한 `repository/`에서 다음을 실행한다.

```bash
uv sync --frozen --extra tau2
uv run --frozen --extra tau2 python scripts/run_decision_replay.py --audit-only --output runs/tau2-decision-replay-luna-120-01
uv run --frozen --extra tau2 python scripts/review_decision_replay_120.py
```

의존성 설치는 네트워크를 쓸 수 있지만 두 감사 명령은 모델을 호출하지 않는다. 별도 τ² checkout·이전 실행 원본이 없어도 동작한다. 단일 검토자의 의미 판정 자체를 독립 검증한 것은 아니다. 압축 안 코드는 위 commit 시점이며 이후 공유 registry·링크 추가는 Git의 후속 commit에 있다.

## 이전 무기억 측정 묶음

| 항목 | 값 |
| --- | --- |
| 공유 폴더 | [agent-memory-baseline-20261115-7243fef](https://drive.google.com/drive/folders/1ku0IRmXZYz34K1BlCuLjDAxX7Ly-f73l) |
| 압축 파일 | [agent-memory-baseline-20261115-7243fef.tar.gz](https://drive.google.com/file/d/1Ct7zVc6MqlSpxp2Ob9woH3S4l6PE51oC/view?usp=drivesdk) |
| 기준 코드 commit | `7243fef085351ac6700f836f695234ad2bd0a5d1` |
| 압축 크기 | 15,238,834 bytes, 약 15.2MB |
| 포함 자료 | 새 raw run 하나와 코드·문서·작은 자료의 commit 사본, 파일 6,828개·88,865,826 bytes |
| 업로드·검증 기록 | [새 공유 registry](../../artifacts/data-releases/20261115-baseline-screening.json) |

압축 SHA-256은 `c8fac8285ef97bcd416307e93f566f722e60be14def5c9823a519997a2702082`다. 원본 전체가 아닌 **증분 묶음**이며, 아래 이전 보관본은 그대로 유지했다. 새 측정은 계획 60회 중 55회 채점·44회 공식 성공이고, HTTP 503 비용 미확인으로 오류 1·미시작 4를 보존했다. [결과 해석](../reports/20261115-tau2-baseline-screening-results.md)을 따른다.

이 묶음의 `RESTORE.md`와 파일별 manifest를 확인하고 새 빈 폴더에 해제한다. `repository/`에 재검증용 코드와 새 `runs/`가 함께 있다. API 키·`.git`·의존성 환경·과거 raw run·`archive/` 보관본은 포함하지 않았다. 코드 저장소에 별도 접근할 수 없어도 이 사본으로 새 측정의 저장된 원본을 감사할 수 있다.

```bash
cd repository
uv sync --frozen --extra tau2
uv run --frozen --extra tau2 python scripts/run_tau2_baseline.py --audit-only --output runs/tau2-baseline-luna-deepseek1017-train-01
uv run --frozen --extra tau2 python scripts/review_tau2_baseline_screening.py
```

의존성 설치는 네트워크를 쓸 수 있지만 두 감사 명령에는 모델 호출·API 키·별도 native checkout이 필요 없다. 별도 DB 진단 재생은 `scripts/setup_tau2.py`로 고정 native 소스를 먼저 준비한다. 압축 안 코드 사본은 위 commit 시점이며 이후 업로드 registry와 이 안내의 갱신은 Git에서 관리한다.

## 이전 보관본의 공유 위치와 기준

| 항목 | 값 |
| --- | --- |
| 사용자 지정 팀 폴더 | [2026 최신딥러닝 15조](https://drive.google.com/drive/folders/1f7FawOEsVUxwtZm47W9fd2ajB3NNqNbn) |
| 이전 원본 묶음 | [agent-memory-runs-20261114-65a757f](https://drive.google.com/drive/folders/1W0VZxIgu_X1GYA8bWXs9wYE8Xq5xI9QX) |
| 압축 파일 | [agent-memory-runs-20261114-65a757f.tar.gz](https://drive.google.com/file/d/1wbrn4_Bz7NZR6Lb8X0RaLPl9zB5ebR5E/view?usp=drivesdk) |
| 기준 코드 commit | `65a757fcbda4e319f7b6748ea815f1ee998b95a3` |
| 압축 파일 크기 | 61,140,217 bytes, 약 61MB |
| 포함 자료 | 실행 폴더 15개와 관련 보고서·프로토콜·근거 등 파일 7,490개, 합계 874,525,357 bytes |
| 업로드·검증 기록 | [Git 공유 registry](../../artifacts/data-releases/20261114-team-runs.json) |

압축 파일의 SHA-256은 다음과 같다.

```text
2be07f90dffbeb7cb863fe34f71d5dd35dee8b85835cf67b0152510578de8ed2
```

이전 48회 비교뿐 아니라 당시 guided 연결·자율 조회·오류로 끝난 연결·준비 검사·합성 오프라인 예제를 보존했다. 이들을 하나의 실제 성능 표본으로 합산하지 않는다. 폴더별 상태·모델 요청 수·관측/합성 구분은 `EXPORT-MANIFEST.json`에 있다. 이 보관본의 연구 해석은 [48회 결과](../reports/20261114-tau2-delivery-development-results.md)를 따른다.

## 각 묶음에 업로드한 파일

| 파일 | 용도 |
| --- | --- |
| 묶음 이름의 `.tar.gz` | 원본과 관련 자료의 압축본 |
| `EXPORT-MANIFEST.json` | 파일별 경로·크기·SHA-256, 실행별 상태와 종류 |
| `SHA256SUMS` | 압축 파일과 외부 manifest의 해시 |
| `RESTORE.md` | 압축 해제·파일 검증·고정 코드에서의 재감사 절차 |
| `CODE_COMMIT.txt` | 자료를 검증할 기준 코드 commit |
| `EXPORT-RECEIPT.json` | 압축본 작성 당시의 크기·해시·검증 상태 |
| `UPLOAD-RECEIPT.json` | 업로드 파일 ID·링크·재다운로드 검증 결과 |

export receipt와 압축본 안의 manifest는 **묶음 생성 시점**의 업로드 전 상태를 보존한다. 이후 업로드 완료 상태는 별도 upload receipt와 Git registry에 기록했다. 새 upload receipt는 앞의 6개 파일을 다루고, 자기 자신의 업로드·재다운로드 검증은 Git registry에 별도로 기록한다. 압축본을 수정해 과거 해시를 바꾸지 않았다.

## 이전 보관본의 복원 순서

1. 공유 폴더에서 압축 파일과 `EXPORT-MANIFEST.json`, `SHA256SUMS`, `RESTORE.md`를 내려받는다.
2. 압축 해시를 대조하고 새 빈 폴더에 해제한다.
3. `RESTORE.md`의 Python 명령으로 모든 포함 파일의 크기·해시를 검증한다.
4. 코드 저장소의 위 기준 commit을 별도 checkout하고 검증한 `runs/`를 배치한다. 기존 동명의 실행 폴더는 덮어쓰지 않는다.
5. Python 3.12/3.13·uv와 고정 τ² checkout을 준비한 뒤 재감사한다.

```bash
uv sync --frozen --extra tau2
uv run --frozen --extra tau2 python scripts/setup_tau2.py
uv run --frozen --extra tau2 python scripts/review_tau2_delivery_development.py
```

마지막 명령은 저장 자료만 검사하며 모델 호출이나 API 키가 필요하지 않다. 설치·소스 준비에는 네트워크를 사용한다. 비공개 코드 저장소의 접근 권한은 Drive 접근과 별도로 필요하다. [연결 환경 안내](../development/tau2-connection.md)도 참고한다.

세 묶음 각각 연결 계정으로 7개 파일을 모두 다시 내려받아 로컬 원본과 비교했다. Drive 메타데이터의 ID·이름·크기·상위 폴더와 `shared` 상태도 대조했으며 다른 팀원 계정으로 별도 로그인한 시험은 하지 않았다. 로컬 `runs/`와 기존 실행·해시는 보존했다. 압축·업로드·재검증 과정의 모델 호출은 없다.
