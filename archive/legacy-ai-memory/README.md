# 기존 데이터 보관본

[20261114-v1](20261114-v1/)은 `/home/depa/ai-memory`의 작업 트리에서 복사한 고정 snapshot이다. 미커밋·미추적·원래 ignore된 데이터도 포함한다. 원본 저장소의 HEAD만으로 이 snapshot을 다시 만들 수는 없다.

데이터 34가족의 853파일, 설계 자료 180파일, 생성 코드 91파일, 설명 문서 144파일을 보관한다. 전체 1,268파일·212,081,023bytes이며 약 202.26MiB다. 원본은 이동·삭제·수정하지 않았다.

- [데이터 설명](20261114-v1/datasets/README.md)과 [과거 역할 목록](20261114-v1/datasets/catalog.json).
- [설계 자료](20261114-v1/docs/design-data/)와 [과거 생성 코드 설명](20261114-v1/data_pipeline/README.md).
- [새 저장소에서의 사용 안내](../../docs/data/legacy-datasets.md)와 [파일별 해시·출처 상태](../../artifacts/legacy-datasets-manifest.json).

과거 관계·선택·실제 득실·합성 설계 자료를 구분한다. 이 자료를 새 자율 생성 기억의 정답으로 자동 사용하지 않는다. 예전 BM25·2k 설정도 새 프레임워크의 기본 설정이 아니다.

`runs/`, `evaluation/results/`, `docs/reports/data/`, 학습 출력·가중치·환경은 이 snapshot 범위에 포함하지 않았다. 따라서 과거 문서의 일부 링크·명령은 원본 저장소나 별도 결과 묶음을 필요로 한다. 생성 코드도 당시의 참고본이며 독립 실행 환경을 제공하는 것은 아니다.

보관 파일은 원래 줄바꿈과 JSON·CSV·gzip bytes를 유지한다. 내부 `.gitignore`도 원본이므로 일부 데이터는 강제 staging으로 보존했다. 수정이 필요하면 보관본을 덮어쓰지 않고 새 자료를 만든다.
