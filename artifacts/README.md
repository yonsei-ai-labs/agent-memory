# 자료 출처와 연결

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
