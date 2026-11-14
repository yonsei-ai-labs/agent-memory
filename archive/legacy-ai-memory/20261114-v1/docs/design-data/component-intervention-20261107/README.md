# 메모리 조각 개입 설계 자료

[설계 노트](../../ai-memory-intervention-study-plan-2026-11-07.md)를 위한 검토 자료다. 새로운 대상 실행이나 확인 실험 결과가 아니다. 모델 요청·실행 은행 수정은 0회다.

- `proposed-study.json`: 주 대비 M−S, N/R/S/M 조건, 후속 삭제·교정·표현·적용성·검증 비용 질문, 실행 전 미확정 사항. **실행 배정이나 충분한 표본 수를 확정한 계획은 아니다.**
- `ablation-example.json`: 기존 source 요약 S와 source 도구 관측 O를 이용한 M=S+O, 전체 O 삭제, 개별 O 삭제, 정확한 복원 예시 9개. 원문·근거 ID·SHA256을 포함한다. 이미 대상 결과를 본 뒤 만든 탐색용 구성이다.
- `numeric-design-examples.json`: 성공률 40/70/35/60%의 **합성 예시**와 고정 사례 집합의 Hoeffding 설계 반폭 계산. 관측 성과·확인 구간·검출력 추정으로 사용하지 않는다.
- `paper-experiment-structures.json`: 이번에 주요 실험 구조를 재확인한 논문 5편의 버전·원문 링크·기존 다운로드 해시와 해석 한계. 논문 결과와 프로젝트의 응용 설계는 노트에서 구분한다.
- `tool-error-outcome-crosscheck.json`: 기존 48회의 오류 관측을 delivered_success에 다시 연결한 사후 기술 집계. 성공15회 중14회, 비성공33회 중31회에 오류가 있었다. 실패 원인이나 회피의 인과 효과를 추정한 것이 아니다.
- `prepare-examples.py`: 위 네 JSON을 원본 근거에서 생성하는 코드. 프로젝트 루트에서 `python3 docs/design-data/component-intervention-20261107/prepare-examples.py`로 실행한다. 기존의 문헌 자료와 source cases 파일을 사용한다.
- `verify-design.py`: 삭제·복원의 원문 일치, source 근거 연결, 합성 계산, 로컬 링크, 기존 완료 자료와 전역 집계 불변을 검사한다. 프로젝트 루트에서 `python3 docs/design-data/component-intervention-20261107/verify-design.py`로 실행한다.
- `validation.json`, `artifact-index.json`: 검사 결과와 이번 설계 노트·자료의 파일 해시. 인덱스는 자기 자신을 포함하지 않는다.

정확한 복원 예시의 해시가 M과 같은 것은 구현 검사다. 새 처치가 되거나 효과 재현을 입증한 것이 아니다. O 추가는 정보·길이·구조를 함께 바꾼다. 주 비교는 이 패키지의 조건부 효과이며 순수한 말투·신경 메커니즘 실험이 아니다.

기존 완료 자료 7묶음의 1,715개 인덱스 항목을 검사한다. 278행 전역 기록과 비용 집계는 그대로 둔다. 사용자가 전달한 외부 리뷰의 18,390개 원본 해시·60개 테스트 검사는 이번 설계 작업에서 재실행하지 않았다.
