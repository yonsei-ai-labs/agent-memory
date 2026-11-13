# 기존 연구에서 새 프로젝트로의 이관

2026-11-14 · 선별과 재구성 내역

새 프레임워크의 연구·설계·구현은 이 저장소에서 관리한다. 원본은 [ai-memory-lab의 고정 커밋](https://github.com/yonsei-ai-labs/ai-memory-lab/tree/d0044c6367e17c1488db4dae37f44e5b358342f9)에서 읽었으며, 원래 작업 폴더의 미커밋 변경과 큰 실험 자료는 복사하지 않았다.

## 가져온 내용

| 원본 | 새 역할과 위치 |
| --- | --- |
| 프레임워크 상세 설계 | [상세 설계](design/architecture.md), [기록](design/recording.md), [개발 계획](development/roadmap.md)으로 새 패키지 경계에 맞게 정리 |
| 설계 검토안·보고서 핵심 정리·저장/조회 논의 | [연구 계획](research/plan.md)과 [실험 프로토콜](research/experiment-protocol.md)로 통합 |
| 벤치 지표·노벨티 비교 | [측정과 분석](research/metrics.md), [관련 연구](research/related-work.md)로 분리 |
| 기록 규약 | SWE 전용 필드를 제거하고 [공통 기록 계약](design/recording.md)으로 재작성 |
| 설정·사건 스키마 | [schemas](../schemas/README.md)에 원본 형식과 ID·버전을 유지해 복사 |
| 설정 초안 | [τ² 두 회차 초안](../protocols/drafts/tau2-two-pass.json)으로 이동하고 초안 ID 정리 |
| 합성 사건·참조 JSON | [계약 fixture](../tests/fixtures/contracts/README.md)로 원본 bytes 이동 |
| 계약 검증기 | `scripts/validate_design.py`로 이동하고 새 경로·날짜 형식 검증에 맞게 수정 |
| 초기 문헌 CSV·JSON | [문헌 자료](references/README.md)로 복사하고 조사 범위 명시 |

대화에서 정리한 네 장의 설명은 [보고서 구성](handoff/report-outline.md)에 반영했다. 이전 초안을 여러 개의 현재 기준 문서로 복제하지 않고 목적별 기준 문서로 통합했다.

## 경계 변경

기존 설계의 `experiments/agent_memory/`와 `evaluation/agent_memory/`는 현재 저장소 안에 배치하는 안이었다. 새 저장소는 실행·기억·어댑터·기록·분석을 `src/agent_memory_eval/`의 공통 패키지에 배치한다. 기존 저장소의 Python 모듈을 실행 의존성으로 import하지 않는다.

큰 자료는 기존 위치에 보존한다. 과거 고정 기억·관계 라벨·학습 결과는 새 자율 생성 기억의 정답이나 성능으로 자동 편입하지 않는다. 필요한 사례는 출처·역할·유효성·분할을 검토한 뒤 명시적으로 도입한다.

해시로 연결된 JSON과 원본 문헌 CSV는 checkout의 줄바꿈 설정으로 bytes가 바뀌지 않게 관리한다. CSV의 BOM·CRLF도 보존하며 `.gitattributes`에 새 경로의 규칙을 정의했다.

기존 `experiments/swe_memory/scripts/records.py`의 해시·원자적 기록·미확인 비용 처리는 향후 재사용 검토 대상으로만 기록했다. 현재 코드로 복사하지 않았으며, 새 요청·노출 계약에 맞는지 검증한 뒤 필요한 동작만 재사용한다.

## 출처 manifest

[legacy-source-manifest.json](../artifacts/legacy-source-manifest.json)에 다음을 기록한다.

- 원본 저장소와 고정 commit, 원본 파일 경로와 SHA-256.
- 복사·수정·통합·참조만 유지 등의 역할과 새 경로.
- 이관 당시 대상 파일의 해시. 이후 문서 수정은 이 이력과 구분.
- 새로 작성한 구조·협업 안내·문서·검증 도구와 제외한 자료의 범위.

기본 검증은 원본 저장소 없이 동작한다. 원본 checkout이 있으면 고정 commit의 bytes를 별도로 확인할 수 있다. 기본 확인은 `uv run python scripts/check_repository.py`, 원본 확인은 같은 명령의 `--verify-source-snapshot /path/to/ai-memory` 옵션이다.

## 이관 시점의 상태

실행기·벤치 연결·실제 모델 실험은 구현·실행 전이다. 현재 제공하는 것은 연구·설계, 패키지 골격, JSON Schema, 합성 예시와 그 검증이다. 벤치 원문에 관한 설명은 기존 조사 시점의 근거를 이어받았으며, 이관 자체를 새 문헌 검증이나 실험 재현으로 표시하지 않는다.
