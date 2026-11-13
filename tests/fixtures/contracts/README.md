# 합성 계약 예시

기존 설계의 기억 저장·snapshot·새 세션·목록 제목·본문 노출을 보여주는 부분 궤적을 이관했다. 모든 사건의 `record_kind`는 `illustrative`다. 모델 요청·응답도 합성이며 실제 API 호출·공식 채점·성공률·비용 관측을 포함하지 않는다.

| 파일 | 내용 |
| --- | --- |
| [example-events.jsonl](example-events.jsonl) | 합성 사건 13개 |
| [examples/collection-context.json](examples/collection-context.json) | 작성 시점에 공개된 경험 예시 |
| [examples/memory.json](examples/memory.json) | 저장된 기억의 제목·본문·버전 |
| [examples/bank-snapshot.json](examples/bank-snapshot.json) | 평가 시작 기억의 snapshot |
| [examples/list-result.json](examples/list-result.json) | 목록으로 반환한 제목과 메타데이터 |
| [examples/get-result.json](examples/get-result.json) | 선택한 항목의 본문 반환 |
| [examples/model-request-after-list.json](examples/model-request-after-list.json) | 목록 제목을 포함한 작업자 입력 |
| [examples/model-response-after-list.json](examples/model-response-after-list.json) | 본문 조회를 선택하는 합성 응답 |
| [examples/model-request-after-get.json](examples/model-request-after-get.json) | 목록 재노출과 본문이 포함된 입력 |

사건의 artifact 상대 경로는 이 fixture 디렉터리를 기준으로 한다. 파일 해시는 bytes 기준이며, `rendered_sha256`은 JSON pointer가 가리키는 문자열의 `char_start:char_end` 부분을 UTF-8로 인코딩한 해시다. 위치는 Python 문자 단위이며 provider 토큰 위치가 아니다.

반환·입력 포함·전송 상태·모델의 실제 활용을 구분한다. 합성 응답의 usage·cost는 null이고 마지막 요청은 부분 궤적으로 남는다. 완료된 모델 실행이나 비용 0의 관측으로 집계하지 않는다.
