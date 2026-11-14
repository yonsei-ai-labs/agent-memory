# 선택한 명령별 확인

라벨을 생성하기 전 명령과 연결된 도구 응답을 읽고 다음 여섯 항목을 확인했다. 자동 선별은 pytest의 선택 필터·shell 복합 실행·파일 생성 여부를 보수적으로 검사했다.

| 원본 행 / 메시지 | 선택한 실제 범위 | 검토 |
| --- | --- | --- |
| 545 / 18 | boltons `tests/test_timeutils.py` | `python -m pytest`의 전체 파일 선택. 같은 tool call ID의 pytest 응답이 있고, 만든 파일 목록에 없음. |
| 545 / 28 | 같은 범위 | 동일 실행의 반복 명령. 추가 독립 경험으로 세지 않음. |
| 588 / 28 | boltons `tests/test_mathutils.py` | 전체 파일 선택과 연결 응답. 다른 변형 실행이지만 upstream revision이 같음. |
| 588 / 38 | 같은 범위 | 동일 실행의 반복. 입력 중복을 늘리지 않고 추가 근거로만 연결. |
| 1,011 / 20 | click `tests/test_chain.py` | 전체 파일 선택과 연결 응답. created paths가 비어 있음. |
| 1,011 / 22 | click `tests/test_basic.py` | 같은 실행의 다른 전체 파일 선택. created paths가 비어 있음. |

응답에는 검사 결과가 있지만 이것으로 포함 여부나 현재 상태를 정하지 않았다. 현재 요청·완료 상태는 명시한 가상 조건이며, 원본의 통과 수나 총 과제 해결로 바꾸지 않는다. 네 파일 모두 정확한 기준 commit에 존재했다. source-review의 upstream 검증 링크와 evidence의 원본 명령·응답을 함께 확인한다.
