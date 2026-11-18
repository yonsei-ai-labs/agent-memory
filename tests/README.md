# 검증 자료와 테스트 배치

2026-11-18 [네 조건 배치의 행동 검사](test_written_replay_batch.py) 13개를 포함해 **전체 307개가 504.859초에 통과**했다. 실제 파일 기록자와 모의 HTTP로 빈 작성·공유 입력·반복·중단·비용·정상 응답을 확인한다. 원본 경로를 옮긴 뒤의 재감사, 미완료 journal과 빈 은행 출처 교환의 거부도 검사한다. 별도로 보존된 실제 4개 결정에서 160개 대응 행과 48개 입력을 재구성했다. [검증 기록](../artifacts/analyses/20261118-written-replay-batch-validation.json)

2026-11-18 [고정 기억 한 응답 실행](../docs/development/snapshot-replay-runtime.md)의 새 [행동 검사](test_snapshot_replay.py) 21개를 포함해 **전체 294개가 469.205초에 통과**했다. 실제 기록자·기억 서비스와 모의 HTTP로 입력 동일성·노출·오류/비용·은행 격리·원본 없는 감사·공개 판정 연결을 확인한다. 기본 검사 3종도 통과했다. 전체 회귀 결과와 재검증 근거는 [검증 기록](../artifacts/analyses/20261118-snapshot-replay-runtime-validation.json)에 남겼다. 새 실제 모델 효과 측정은 아니다.

2026-11-17 [작성 기억 비교의 타당성 보완](../docs/development/written-comparison-validity.md) 후 native extra와 고정 checkout이 있는 환경에서 **전체 273개가 443.869초에 통과**했다. [새 행동 검사](test_written_comparison_validity.py) 13개는 결정 선정·공개 근거·완전한 작성 receipt·준비물 재구성·판정의 미확정을 확인한다. 실제 원본의 준비 입력 48개와 대응 행 160개도 기존 renderer와 같았다. [검증 기록](../artifacts/analyses/20261117-written-comparison-validity-validation.json)을 따르며, HTTP는 mock이고 새 모델 효과를 측정한 결과가 아니다. 아래 수치는 이전 검증 이력이다.

2026-11-16 착수한 추가 보완에서 native extra와 고정 checkout이 있는 환경의 **전체 215개가 414.294초에 통과**했다. 모든 모델 경로는 mock HTTP다. 직전 197개·417.496초, 182개·488.026초, 172개·373.041초와 아래 수치는 이전 검증 이력이며 서로 성능 비교에 쓰지 않는다.

[추가 실행 보완](../docs/development/runtime-hardening.md)은 18개 검사를 더했다. writer 추론 필드 재입력·추가 문맥·비용 불일치·중복 ID의 회귀 4개를 이전 `1e59371` worktree에서도 다시 재현했다. 역할별/입력 한도, worker 중단 뒤의 DB 진단과 user 한도의 불완전 상태, 잘못된 provider 응답·usage, 비유한 비용 설정, 실제 Git의 clean/dirty 동결 검사도 확인한다. [근거](../artifacts/analyses/20261116-runtime-hardening-validation.json)를 따른다.

새 [blob 캐시 검사](test_blob_verification_cache.py) 8개, [기억 작성 검사](test_memory_writing.py) 5개, [과정 trace·native 전달 통합](test_experience_transfer.py) 2개를 더했다. 손상·교체·삭제·하위 참조·재시도, 동일 공개 경험의 세 피드백 격리·정답/비공개 입력 차단·빈 기억·오류 보존, writer 작성→새 세션·DB 변경·공식 채점을 확인한다. 실제 모델 효과는 아니다. [검증 자료](../artifacts/analyses/20261116-worker-memory-validation.json)와 [구현 안내](../docs/development/worker-written-memory.md)를 따른다.

이번 [실행 경계 버그 수정](../docs/development/runtime-edge-fixes.md)은 새 테스트 10개를 추가했다. OpenRouter 12개·결정 재전송 22개·native 연결 3개·baseline 13개, 관련 50개도 별도로 통과했다. null 사용량의 원응답·미확인 비용과 다음 전송 차단, `+1` 시각·기존 야간편·잘못된 날짜, 마지막 정상 배정의 완료 사유와 실제 오류 보존을 확인한다. 원본 세 실험의 최신 코드 호환 감사와 고정 commit의 재현 결과는 [검증 자료](../artifacts/analyses/20261116-runtime-edge-fixes-validation.json)에 연결한다.

[schema 캐시 검사](test_validator_cache.py) 3개는 반복 컴파일 제거와 잘못된 기록의 거부, mapping 변경 격리·다른 schema root의 규칙 격리를 확인하며 통과했다. 성능은 실제 저장 결과의 별도 [전후 측정](../artifacts/analyses/20261116-schema-validation-performance.json)이며 모델 효과와 구분한다.

[과거 재현 행동 검사](test_historical_reproduction.py) 18개는 실제 작은 Git worktree와 격리 Python으로 현재 main 변경·키 제거·잘못된 import·입력 변조/누락·활성 writer·네트워크/쓰기/프로세스 시도·timeout·사본 정리를 확인한다. 로컬 native clone·허용된 Git 읽기/변경 거부·내부/외부 symlink·과거 이웃 스크립트 import도 검사한다. 표준 라이브러리 fixture 환경을 사용하며 실제 과거 결과의 재현과 구분한다. [실행 안내](../docs/development/historical-reproduction.md)를 따른다.

[진단 검토 packet 검사](test_decision_review_packet.py) 3개는 안정적인 가림 ID와 공개 필드, 주석의 완전성·중복·범주·근거 일치를 확인하며 별도로 통과했다. 2026-11-16 유료 실행 전 기존 재전송 행동 테스트 16개도 다시 통과했다. 전체 151개를 재실행한 것은 아니다. [실제 120요청 결과](../docs/reports/20261116-decision-replay-results.md)는 이 오프라인 테스트와 별도다.

[결정 지점 재전송](test_decision_replay.py) 16개는 원본 입력 동일성·medium/메모 변경 범위·조건 블록·원본 seed 유지·3개 호출 검사와 5개 수동 지점·추가 조회/보류 구분·mock HTTP 수명주기·원본 보존·외부 원본 없는 감사·예산 거절·503/미확인 비용 중단을 검증한다. 실제 모델 효과와 구분하며 [구현 안내](../docs/development/decision-replay.md)를 따른다.

2026-11-15에 새 16개를 포함한 **전체 148개가 통과**했다. 아래 128개·별도 4개 등의 설명은 이전 단계 이력이며 실제 모델 실험의 결과를 뜻하지 않는다.

[무기억 공개 분석](test_baseline_review.py)의 순수 테스트 4개도 별도로 통과했다. WRITE 도구 표기, 공개 메시지에서 비공개 필드 제외, 호출 반복·이관·오류 계수를 확인한다. 아래 전체 128개 실행 이후의 추가 검증이며 전체 132개를 다시 실행했다고 보고하지 않는다. 실제 측정의 원본 audit와 분석 재현 명령은 [결과 보고서](../docs/reports/20261115-tau2-baseline-screening-results.md)를 따른다.

[무기억 탐색](test_tau2_baseline.py)의 새 12개를 포함해 2026-11-15에 전체 128개가 통과했다. 준비되지 않은 비용·비유한 숫자·변경된 소스·시험 분할·기존 출력 폴더를 거절하고, native 모의 HTTP에서 새 세션/DB·반복별 seed와 사용자 wire seed 생략·개별/전체 요청 중단·미확인 비용·native 단계 한도·출처 보존·원본 재계산·중단 기록·요약 변조를 검증한다. 별도 guard 검사는 두 범위의 요청/금액/시간 거절에서 HTTP 횟수가 부풀지 않는지 확인한다. 기본 검사 3개와 새 스키마 검사도 통과했다. 실제 Luna 실험이나 기억 효용 결과가 아니다. 아래 80·88·109·116개는 이전 단계의 검증 이력이다.

[기억 도구 노출 진단](test_memory_visibility.py) 7개를 추가했다. 텍스트에 적힌 이름과 실제 schema 제공, `none`으로 막힌 선택, 원응답 호출과 텍스트 언급, 다른 run/ID/연산의 반환·누락/중복 반환, 기억 schema/auto/raw call의 HTTP 전송 보존, 저장 JSON의 구조 재현을 확인한다. 이 7개와 기존 native 라우팅·기억 오류·전송 관련 3개를 실행해 통과했다. 직전 전체 109개에 더한 현재 116개 전체는 이번에 다시 실행하지 않았다. 기존 기록 재검사는 [진단 보고서](../docs/reports/20261114-memory-retrieval-diagnosis.md)를 따른다.

현재는 파일 코어·입력 구성·혼합 도구·가짜 두 회차의 [실제 오프라인 행동 테스트](test_offline_core.py), [v1.1 합성 계약 fixture](fixtures/contracts-v1.1/README.md)의 음성 사례, 보관본 손상 탐지를 제공한다. [기존 v1.0 fixture](fixtures/contracts/README.md)도 보존한다. 실제 모델·벤치 능력이나 정책 효과의 실증은 아니다.

```bash
uv run python scripts/validate_design.py
uv run python scripts/check_repository.py
uv run python scripts/verify_legacy_data.py
uv run python -m unittest discover -s tests
```

오프라인 코어 26개·계약 26개·보관본 8개·OpenRouter 전송/프로필 mock 10개를 실행한다. τ² extra와 고정 checkout이 있으면 native 무기억 통합 2개와 [기억 통합](test_tau2_memory.py) 8개를 더해 총 80개다. 기억 통합은 같은 native 환경/채점에서 내부 왕복·혼합 순서·종료 batch·사후 작성 실패·은행 분리·요청 한도·출처/audit를 확인한다. 새 기능/벤치 연결에는 관련 행동 테스트를 추가한다. 현재 지원 범위는 [기억 연결 안내](../docs/development/tau2-memory.md)와 [오프라인 코어 안내](../docs/development/offline-core.md), 전체 목표는 [개발 계획](../docs/development/roadmap.md)을 따른다.

[자율 조회 파일럿](test_tau2_autonomy.py)의 순수 준비 검사 3개와 native mock 5개를 더해 현재 전체는 88개다. 블록/위치 배정·해시 변경 거절·무기억/빈은행/기억은행의 wire 대조·미확인 비용 중단·빈 source의 생략·동일 버전 가져오기·예전 대화 차단·원본 폴더 없이 audit·요약 변조 거절을 확인한다. 모든 unittest는 모의 HTTP만 사용한다.

[행동 보조 분석](test_action_comparison.py) 5개를 더한 93개에, [snapshot 제공](test_preload.py) 8개와 [과제/조건/반복 실행](test_tau2_delivery.py) 8개를 추가했다. extra와 고정 checkout이 있는 전체는 109개다. 한글·따옴표의 구간, v1.1 보존·v1.2 출처, 전송 전 한도·역할/은행 차단, 새 native 세션·DB 격리, 조회 도구 없는 제공, 두 과제의 공식 결과 차이·비용 미확인 생략·보관된 원본 재계산을 검사한다. [v1.2 닫힌 합성 fixture](fixtures/contracts-v1.2/README.md)는 기본 설계 검사에서도 확인한다. 실제 모델 효과 결과는 아니다.

[v1.1 테스트](test_contract_v11.py) 26개는 journal tail/손상/누락, 재전송 중복, 세션 ID·은행/고정 보기, 요청 출처/역할/시점/완전성, 자기 작성 인수, 공개 경험 접두부, 목록 메타데이터, 합성 혼합 호출 순서와 ready 설정 형식을 확인한다. 매 테스트는 작은 임시 fixture를 만든다. 실제 기록자·라우터·provider 요청을 생성하거나 실행하지 않는다.

[보관본 테스트](test_legacy_data_archive.py)는 작은 임시 파일에서 bytes 변경·누락·미등록 추가·경로 이탈·symlink·현재 출처 변경을 탐지한다. 큰 과거 데이터를 테스트 fixture로 복제하거나 모델을 호출하지 않는다. 첫 런타임 작업의 검증은 [구현 명세](../docs/development/first-implementation.md)를 따른다.

실제 네트워크 연결 확인은 [별도 명령](../docs/development/tau2-connection.md)에서 `--live`로 실행한다. `uv run --extra tau2 python -m unittest discover -s tests`는 native 도구·채점을 포함해 모의 HTTP만 사용한다.

[실제 48회 전달 결과](../docs/reports/20261114-tau2-delivery-development-results.md)는 위 unittest와 별도다. 고정 구현·τ² checkout·실행 원본이 있는 환경에서 `uv run --frozen --extra tau2 python scripts/review_tau2_delivery_development.py`로 공식 audit·259개 공개 근거·주석 연결과 receipt를 새 모델 호출 없이 재검증한다. 단일 검토자의 의미 판정 자체를 자동 입증하는 검사는 아니다.
