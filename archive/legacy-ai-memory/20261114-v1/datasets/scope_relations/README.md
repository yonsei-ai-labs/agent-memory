# 범위 관계 진단 자료

- [출처 범위를 보강한 v1](20261108-expansion-v1/README.md): 동결된 학습·개발·첫 외부 자료.
- [길이를 통제한 새 출처 v1](20261108-length-challenge-v1/README.md): 외부 예측 전에 별도 동결한 진단.
- [데이터의 한계와 최신 정정](../../training/memory_selector/reports/2026-11-08-scope-data-quality.md).

**출처 단위 정정:** 첫 v1 설명의 “17개 원본 실행/episode”는 과제 ID 17개를 센 값이다. 실제 연결된 실행 기록은 로컬 run 24개와 공개 trajectory 8개, 총 32개이며 같은 과제의 반복을 포함한다. 7개 저장소·12개 연결 pool과도 구분한다. [식별자별 검산](../../docs/reports/data/scope-source-verification-20261108/source-unit-counts.json). 동결 폴더의 해시·입력·라벨·분할은 변경하지 않았다. 최신 `data_pipeline.audit_scope_expansion`은 이 단위를 분리해 출력한다.
