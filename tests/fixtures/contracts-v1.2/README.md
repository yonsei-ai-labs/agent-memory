# Snapshot 자동 제공 계약 예시

v1.2 journal의 닫힌 합성 기록이다. 기존 v1.1 사건과 새 `memory.preloaded`·snapshot 출처의 `memory.input_included`를 함께 담는다. 두 기억의 한글·따옴표·줄바꿈을 그대로 입력에 제공하며, 제목·본문 네 구간을 실제 요청과 연결한다.

`log.jsonl`과 해시로 연결한 `blobs/sha256/`는 파일 코어에서 만든 illustrative 자료다. 실제 Luna·사용자 모델·벤치 실행이나 성공 결과가 아니다. `scripts/validate_design.py`는 파일 무결성·세션·snapshot·요청 출처·노출 완전성을 검사한다. 변조·다른 은행·늦은 제공·할당량 초과의 행동 검사는 [preload 테스트](../../test_preload.py)에 있다. v1.0/v1.1 fixture는 유지한다.
