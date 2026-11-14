# 실행 전제 기억의 출처 검토 — 2026-11-08

**도구 응답 36개·출처 가족 19개를 검토해, 전제와 후속 진행이 연결되는 근거 후보 8개를 남겼다.** 그 8개도 독립 출처는 네 가족뿐이다. 새 학습 행·관계 정답·실제 기억 득실 라벨은 모두 0개다. 이 자료를 곧바로 학습 데이터 증가분으로 세면 안 된다.

## 어떻게 골랐나

[처음 계획](selection-plan.json)에 따라 이미 받은 [SWE-smith trajectories](https://huggingface.co/datasets/SWE-bench/SWE-smith-trajectories/tree/08e109b4a59eaeebf80e4675cd125d42e7ac99a4)의 고정 shard에서 1,024–2,047행만 검사했다. 과제 최종 결과·공식 수정안은 읽지 않고, 명령과 연결된 중간 오류 응답만 사용했다. 로그의 명령은 실행하지 않았다.

이 구간에서는 227개 trajectory·48개 연결 가족에서 461개의 후보 도구 호출이 잡혔다. 이 숫자는 유용한 기억이나 독립 경험의 수가 아니다. 첫 20개를 순서대로 검토했더니 세 가족에 집중됐고, 남길 수 있는 다섯 전제 에피소드는 모두 같은 Pydantic trajectory였다. 따라서 [두 번째 계획](diversity-review-plan.json)을 먼저 저장하고, 아직 검토하지 않은 가족마다 가장 이른 후보 하나씩, 16개 가족을 추가했다. 제외된 후보를 성공 사례로 교체하지 않았다.

가족은 `repository@upstream_revision`으로 묶는다. 같은 revision의 다른 결함·PR ID를 독립 분할로 흩뜨리지 않는다. 이들은 공개 benchmark의 에이전트 실행 기록이며 실제 저장소를 사용하지만, `pr_` ID가 있다는 이유만으로 자연 발생 사용자 문제나 독립 경험이라고 보증하지 않는다. 원본 데이터 카드의 선언 라이선스는 MIT이며, 저장소별 코드·로그의 출처도 각 항목에 보존했다.

## 남긴 근거와 경계

| 출처 | 관측한 변화 | 아직 주장할 수 없는 것 |
| --- | --- | --- |
| Pydantic: jsonschema | conftest import 실패 → 설치 → 다른 내부 symbol import 실패까지 진행 | 전체 검사 성공, 설치 하나만의 인과 효과 |
| Pydantic: benchmark plugin | 세 번의 옵션 거부 → plugin 설치 → 옵션을 인식하고 다음 수집 오류로 진행 | 좁힌 검사로 전체 과제를 검증했다는 주장 |
| Pydantic: dirty-equals | 수집 실패 → 설치 → 같은 명령에서 186 passed, 5 skipped | 전체 suite나 과제 성공, 기억 덕분의 개선 |
| Pydantic: pytz/pytest-examples | 세 수집 오류 → 설치 → 6,247개 수집 후 실행 시작 | 잘린 후속 로그의 전체 성공 |
| Pydantic: faker | 여덟 fixture 오류 → 설치 → 후속 전체 검사에서 그 여덟 항목 통과 | 전체 로그의 성공·미래 환경의 동일 조건 |
| pipdeptree | 내부 version module 없음 → editable 설치 → import를 지나 metadata TypeError | 어떤 build 단계가 원인인지, 전체 재현 성공 |
| Dask | 없는 검사 파일 → 실제 파일을 지정해 18 pass·1 fail | 그 범위가 충분한지, Graphviz `dot` 부재를 해결했는지 |
| gTTS | six import 실패 → 설치 → 같은 재현 스크립트의 여러 분기 실행 | 출력 문구만으로 모든 동작이 검증됐다는 주장 |

추가로 세 종류의 경계를 보존했다. Starlette의 uvicorn 부재는 후속 설치·검증이 없어 **미해결 관측**이다. pandas는 여러 설치 뒤에도 `/testbed/pandas`의 compiled module import가 실패해 **실패 시도 근거**만 남겼다. typeguard는 editable 설치가 성공해도 내부 `CallMemo` import 실패가 반복되어, pipdeptree의 설치 경험을 무조건 전이하면 안 되는 **제외 대조**다. 모두 기억 제공의 손해 라벨은 아니다.

36개 전체의 처리 결과는 근거 후보 8, 중복 에피소드 2, 일반 제외 23, 실패한 설치를 포함한 제외 대조 1, 미해결 관측 1, 실패 시도 근거 1이다. 검토는 assistant 한 명이 한 것으로 독립 사람 주석이 아니다.

## 데이터 설계에서 얻은 구체적 교훈

1. **설치 명령의 성공과 필요한 전제의 충족은 다르다.** Pydantic에서는 `pip install -e ".[dev]"`가 성공했지만 “dev extra를 제공하지 않는다”는 경고가 있었다. 관측한 `pyproject.toml`에는 dev dependency group이 있었다. 이후 여러 검사 의존성이 계속 빠져 있었다. 성공 코드만 남기는 요약은 이 경계를 잃는다.
2. **같은 오류 이름이 같은 처방을 뜻하지 않는다.** 내부 version module은 설치 뒤 진행되기도 했지만, 다른 내부 symbol은 설치 후에도 실패했고 소스 수정이 필요했다. `ModuleNotFoundError`·`ImportError`를 단순히 설치 양성 라벨로 바꾸지 않는다.
3. **실패한 대안과 검사 범위를 같이 보존한다.** PYTHONPATH 변경이나 좁은 검사 선택이 benchmark plugin 부재를 해결하지 못한 기록은 조건 판단에 중요하다. Dask의 실제 검사 파일을 찾았어도 Graphviz의 시스템 실행 파일이 없어서 한 항목은 실패했다.
4. **진행과 성공, 출처 근거와 현재 효용을 분리한다.** 다음 오류까지 도달했다는 사실을 전체 해결로 바꾸지 않는다. 과거 로그에서 도구가 없었던 것과 현재 대상 환경에도 없는 것은 다른 사실이다.

## 다음 실험으로 옮기는 조건

이 후보를 바로 현재 확인 자료에 추가해 모델을 고르지 않는다. 다음 별도 자료에서는 먼저 source/target 연결 그래프와 학습·개발·확인 가족을 고정하고, target의 판단 시점을 정해야 한다. 첫 실행 전 선택과 이미 오류를 관측한 뒤 선택은 입력 정보가 다르므로 별도로 다룬다.

각 과거 기억에 대해 현재 전제가 빠진 상태, 이미 충족된 상태, 비슷한 오류지만 다른 원인인 상태, 필요한 관측이 없는 상태를 확보한다. 조작한 상태라면 자연 발생 로그라고 쓰지 않고 재구성 조건으로 표시한다. 선택기에 주는 것은 그 시점까지의 관측이며, 다음 실패·성공·공식 검사 결과·기억을 읽고 만든 명령은 입력에서 제외한다.

그 뒤 고정 작업자로 무기억 / 출처 근거 / 요약 / 과정 정보를 복원한 요약을 비교한다. 최초 유효 검사까지의 명령 수·시간, 잘못된 설치나 불필요한 재검사, 최종 수정·제출을 따로 기록한다. 일반적인 전제 확인 규칙과 항상 제공 기준선도 포함한다. 이 실제 제공/보류 결과가 있어야 효용 학습 대상으로 승격할 수 있다.

행 수를 복제해서 늘리기보다, 독립 가족과 실패 원인의 폭을 늘리는 것이 우선이다. 현재 8개 후보만으로 neural selector의 학습·일반화 성적을 보고하지 않는다.

## 파일과 재현

- [첫 20개 수동 판정](manual-review.json), [근거 카드·원문 offset·SHA](initial-review.json)
- [추가 16개 수동 판정](diversity-manual-review.json), [근거와 제외 대조](diversity-review.json)
- [고정 구간 검색 코드](../../../data_pipeline/screen_memory_prerequisite_sources.py), [선정 창 추출](../../../data_pipeline/extract_memory_prerequisite_review.py), [첫 검토 내보내기](../../../data_pipeline/export_memory_prerequisite_review.py)

전체 원문 창은 무시된 로컬 `training/memory_selector/outputs/daytime-prerequisite-*-20261108-v1/`에 보존했다. 공유 JSON에는 명령과 필요한 정확한 발췌, 원문 문자 offset·digest·clipping 여부를 넣었다. 원본 checkpoint와 기존 동결 데이터는 변경하지 않았다.
