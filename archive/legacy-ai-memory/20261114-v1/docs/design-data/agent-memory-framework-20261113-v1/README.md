# 에이전트 메모리 프레임워크 설계 계약과 예시

2026-11-13 · [상세 설계](../../ai-memory-framework-detailed-design-2026-11-13.md)의 검토용 부속 자료

설정과 사건의 형식을 기계적으로 확인하기 위한 JSON Schema와 합성 자료다. 프레임워크 실행기·벤치 어댑터의 구현 또는 실제 모델·벤치 성능 자료를 뜻하지 않는다.

| 파일 | 역할 |
| --- | --- |
| [run-plan.schema.json](run-plan.schema.json) | 실험 설정의 타입·기억 조건·기본 실행 준비 제약 |
| [run-plan.example.json](run-plan.example.json) | Luna·τ²의 두 회차 실험 초안. 미정 항목은 null이며 상태는 draft |
| [event.schema.json](event.schema.json) | 공통 사건 envelope와 핵심 종류별 payload |
| [example-events.jsonl](example-events.jsonl) | 기억 저장·snapshot·새 세션·목록 제목 노출·본문 노출의 합성 부분 궤적 |
| [validate_examples.py](validate_examples.py) | 형식·사건 참조·파일 해시·입력 부분 문자열을 검사하는 읽기 전용 검증기 |
| `examples/` | 사건에서 참조하는 합성 공개 경험·기억·반환·요청·응답 |

초기 비교는 무기억·종료 후 작성·수행 중 작성·병행의 네 조건이다. 후보 풀 비교에는 원천 과제별로 작성한 동일 기억의 자기 경험 은행과 혼합 은행을 사용한다. 과제 간 은행을 누적 갱신하는 실행은 별도 프로토콜로 표현한다.

## 형식 검증과 실행 준비

JSON Schema는 Draft 2020-12다. `draft`에서 null을 허용하는 모델 파라미터·과제 manifest·반복·페이지·중단 설정이 있다. `ready`에서는 주요 필드를 채워야 하며 유한한 호출 또는 시간 중단 조건이 필요하다. status를 바꾸는 것만으로 실행 준비가 완료되지는 않는다.

추가 의미 검증은 구현할 `PlanCompiler`의 책임이다. 조건 ID 유일성, 원천 분할·짝짓기, prompt 참조와 해시, 실제 모델과 벤치 지원, 은행 권한·capability, 지출·주석·통계 계약을 검증해야 한다. 예시의 null을 임의의 실행 숫자로 대체하지 않는다.

`event.schema.json`은 envelope와 저장·반환·입력 포함·snapshot·모델 요청·시작/종료·결과 payload를 정의한다. 사건 간 외래 참조·단조 순서·버전 계보·본문 해시·입력 문자열 위치는 별도 교차 검증 대상이다. 업무 전이·환경별 오류의 상세 필드는 어댑터 계약으로 추가한다.

## 합성 자료의 해석

모든 예시 사건은 `record_kind=illustrative`다. 과제·ID·본문과 모델 요청·응답은 계약 예시이며 생성 API를 호출하지 않았다. 원천 경험에서 항목 하나를 저장하고, 이를 새 세션의 목록과 본문으로 읽는 부분 궤적이다. 실험 완료·공식 채점·성공률·비용 관측은 포함하지 않는다.

본문과 제목은 모델 요청의 문자열 안에서 확인할 수 있다. 사건의 `pointer`, `char_start`, `char_end`는 JSON을 해석한 뒤 해당 문자열의 Python 문자 위치다. 실제 provider의 토큰 위치나 byte 위치로 해석하지 않는다. `rendered_sha256`은 해당 부분 문자열의 UTF-8 해시다.

`artifact_ref.path`는 이 부속 자료 디렉터리를 기준으로 한 상대 경로다. 실제 실행에서는 실험 루트의 내용 해시 저장소로 연결할 예정이다. artifact 해시는 파일 bytes 기준이며, 기억 본문과 입력 부분 문자열의 해시는 각각 별도 필드다.

예시의 모델 응답에도 사용량·비용은 null이다. 이 예시를 관측된 성공·실패 또는 비용 0의 실행으로 집계하지 않는다.

## 로컬 검증

Python 3.11 이상과 `jsonschema`가 있는 환경에서 저장소 루트에서 실행한다. 검증기는 파일을 읽고 결과만 출력하며 모델 API·벤치·GPU를 실행하지 않는다.

```bash
python3 docs/design-data/agent-memory-framework-20261113-v1/validate_examples.py
```

미정 초안의 ready 승격, 기억 없음 조건의 조회, 0회 반복, 유한 중단 조건 누락, 채점 완료 결과의 성공값 누락이 형식 검사에서 거절되는지도 확인한다. 내부에서 만드는 가짜 ready 형태는 스키마가 모순 없이 채워질 수 있는지 확인하는 용도이며 실행 가능한 계획이 아니다.
