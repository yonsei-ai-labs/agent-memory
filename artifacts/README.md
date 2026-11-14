# 자료 출처와 연결

[팀 원본 공유 registry](data-releases/20261114-team-runs.json)는 Google Drive에 업로드한 압축본·manifest·복원 안내의 파일 ID/링크와 기준 코드 commit을 보존한다. 7개 파일의 재다운로드·바이트/해시 대조, 폴더 위치·파일 크기 확인 결과를 담는다. 큰 압축본과 전체 inventory는 Git 밖에 두며 [다운로드·복원 안내](../docs/data/team-run-sharing.md)를 따른다.

작은 manifest로 원본 위치·버전·해시·역할을 관리한다. 큰 데이터·원시 실행·가중치는 여기에 복사하지 않는다.

[legacy-source-manifest.json](legacy-source-manifest.json)은 기존 레포의 고정 커밋에서 무엇을 가져왔는지 기록한다. 각 원본의 SHA-256, 새 경로, 복사·경로 수정·내용 통합·참조만 유지 등의 변환과 이관 당시 새 파일의 해시를 포함한다.

이관 후 수정할 설계 문서의 현재 해시가 이관 당시와 달라지는 것은 정상이다. 출처의 commit·원본 해시와 이관 당시 해시는 이력으로 보존한다. 의미를 그대로 유지하는 고정 계약 예시는 버전과 원본 연결을 관리한다. [이관 내역](../docs/migration.md)을 함께 읽는다.

기본 검사에서는 이관 이후 변경된 파일을 보고하며 정상적인 후속 개발을 막지 않는다. 이관 직후 상태를 엄격히 확인할 때는 `--check-import-hashes`를 추가한다.

기본 검증은 이전 저장소 없이 가능하다. 원본 checkout을 가진 경우에는 출처 해시도 다시 확인할 수 있다.

```bash
uv run python scripts/check_repository.py
uv run python scripts/check_repository.py --verify-source-snapshot /path/to/ai-memory
```

[legacy-datasets-manifest.json](legacy-datasets-manifest.json)은 이후 요청받은 데이터 보관의 별도 기록이다. 고정 commit만 가져온 최초 설계 이관과 달리, 기존 작업 트리의 미커밋·미추적·ignore된 파일도 포함한다. 파일별 SHA-256·크기·역할·출처 Git 상태와 HEAD blob ID, 전체 선택·제외 범위를 기록한다.

데이터 본체는 `archive/`에 있으며 [보관 안내](../docs/data/legacy-datasets.md)를 따른다. `uv run python scripts/verify_legacy_data.py`로 원본 checkout 없이 모든 보관 파일의 무결성을 확인한다. 당시의 라벨 의미·실제 성능을 재검증하는 명령은 아니다.

[experiment-handoff-import.json](experiment-handoff-import.json)은 다운로드 폴더의 실험팀 문서를 추가 반영한 기록이다. 원문 보관본의 bytes·SHA-256, 파생 검토/제안 문서, 확인한 공식 소스 commit·해시와 미열람 Library 연결을 남긴다. 기본 저장소 검사는 원문 보관본의 해시를 확인하며, 온라인 원문을 다시 조회하거나 실제 실험을 수행하지 않는다.

[행동 보조 분석](analyses/20261114-tau2-action-comparison.json)은 기존 자율 조회 파일럿을 audit한 뒤 원본과 별도 버전으로 계산한 사후 기술 분석이다. 입력 로그·요약 해시와 이관 요약 인수만 제외하는 규칙을 담으며, 원래 `actions_changed`나 실행 원본을 바꾸지 않는다. [재계산 방법과 해석](../docs/development/tau2-autonomy-pilot.md#보고서용-행동-보조-분석)을 따른다.

[업무 사례 주석](analyses/20261114-tau2-case-review.json)은 같은 9회 실행의 공개 메시지를 근거로 한 단일 에이전트의 사후 검토다. 사용자 거절 설명·이관 근거·대안 제안을 구분하고 사건 번호·원문 blob 해시를 남긴다. [해석과 판정 범위](../docs/reports/20261114-tau2-case-review.md)를 따른다. 원본 점수와 별도이며 독립 재판정은 아직 없다.

[다음 과제 후보](analyses/20261114-tau2-task-candidates.json)는 공식 train 30개의 구조 목록, 상세 검토 7개와 개발 후보 6개, 고정 소스 해시와 오프라인 native DB 검사를 담는다. 모델 실험 결과나 실행 프로토콜이 아니다. 두 자료는 분석 전용이며 worker·writer 입력에 넣지 않는다. 원본 사건의 재대조에는 기록된 `runs/` 원본과 고정 τ² checkout이 필요하다. [과제 선정과 주석 기준](../docs/research/tau2-task-selection-and-rubric.md)을 함께 읽는다.

[전달 비교의 실행 전 동결 기록](pilots/20261114-tau2-delivery-development-protocol.json)은 프로토콜·주석 기준·구현 해시와 비용 상한을 보존한다. [실제 48회 receipt](pilots/20261114-tau2-delivery-development-results.json)는 원본 audit·배정·채점·노출·비용·모델/제공자에 연결한다. [공개 근거와 주석](analyses/20261114-tau2-delivery-behavior-review.json)은 실패·설명·시나리오 제시와 파일 상태 사본의 보관량을 별도로 남긴다. 원본·동결 기록은 유지하며, 단일 검토자의 의미 판정은 독립 재판정 전이다. [결과 보고서와 재검증 명령](../docs/reports/20261114-tau2-delivery-development-results.md)을 따른다.
