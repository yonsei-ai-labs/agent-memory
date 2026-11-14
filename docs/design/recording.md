# 기록과 재현 계약

2026-11-14 · 공통 프레임워크의 원본·분석 연결

모델 요청·응답, 기억 저장·반환·입력 포함, 업무 행동과 공식 결과를 연결한다. 전체 과제 묶음은 `experiment_id`, 한 과제·조건·반복 시도는 `run_id`로 식별한다. 분석을 다시 해도 원본을 덮어쓰지 않는다.

## 식별과 공개 범위

조건·순서·독립 반복·스트림·과제 원천·결과 짝·은행·snapshot·항목·버전·요청·연산·사건을 연결한다. 여러 호출·항목·파생 과제를 독립 표본처럼 합산하지 않는다.

작업자의 정본 대화에서 공개 업무 보기를 투영한다. 사용자 시뮬레이터 내부 지시·정답·평가 주석은 작업자 입력과 분리한다. 기억 도구 왕복은 사용자 대화를 자동 진행시키지 않는다.

## 원본 배치

```text
runs/<experiment_id>/
  experiment.json
  plan.requested.json
  plan.resolved.json
  schedules.jsonl
  pairs.jsonl
  task-groups.json
  state/memory.sqlite3
  events.jsonl
  snapshots/<snapshot_id>.json
  blobs/sha256/<digest>
  analysis/<analysis_revision>/

runs/<run_id>/
  manifest.json
  config.json
  effective-config.json
  task.json
  grader-instance.json
  api/
  trajectory.json
  memory-events.jsonl
  memory-summary.json
  official_report.json
  summary.json
  artifact-hashes.json
```

experiment의 manifest를 한 번의 과제 실행으로 집계하지 않는다. 공통 사건의 run별 보기에 같은 event ID가 나타나도 비용·시도 수는 한 번만 센다. 원본은 Git 밖에 두고 필요한 공유 묶음의 위치·해시·제공 범위를 `artifacts/`에 남긴다.

## 실제 입력과 기억 노출

실제 요청의 메시지·도구 스키마·모델·설정과 응답을 보존한다. 제목·목록·본문·가공본·이력 재전송을 구분하고 입력의 JSON pointer·문자 범위·해시를 기록한다. 문맥 생략·압축·redaction도 표시한다.

`returned`는 도구 반환, `input_included`는 보관한 요청의 내용 포함이다. 전송 전 중단·전송 여부 불명·정상 응답을 구분한다. 입력 포함을 주의·이해·효용의 증거로 단정하지 않는다. 인증 정보는 요청 원본과 함께 보관하지 않으며 저장용 redaction을 실제 입력 변형에 사용하지 않는다.

선택적인 [적용 가능성 필터](../research/condition-sensitive-pilot.md)를 구현하면 필터 전 후보·시점의 공개 관측·판정/미확정·통과 결과·원본 판정 요청·worker 전달을 연결한다. 독립 판정기 입력과 worker 입력의 노출 수신자를 구분하고, 제목이나 거절 설명에서 내용이 전달된 경우도 기록한다. 별도 판정 역할/필드가 필요하면 사건 계약·버전/호환 검증을 갱신한다. 현재 스키마가 그 기능 전체를 이미 구현했다고 해석하지 않는다.

## 사건과 장애

[사건 스키마](../../schemas/event.schema.json)에 envelope와 핵심 payload가 있다. 저장·갱신·head·연산 결과·사건은 같은 저장소 트랜잭션으로 확정한다. outbox에서 JSONL을 내보내고 재내보내기의 중복을 event ID로 탐지한다.

요청 시작을 응답보다 먼저 남긴다. 누락·중간 행 손상·해시 불일치를 검사하고 추적 근거가 불완전한 실행을 정상 완료로 표시하지 않는다. 복원할 수 없는 시도는 중단 기록을 유지하고 새 run으로 시작한다.

## 결과와 결측

| 값 | 계약 |
| --- | --- |
| 공식 성공 | 채점 완료 때 boolean. 미실행·오류·불완료는 null |
| 종료 사유 | 업무 종료·호출/시간 한도·작성 중단·인프라 오류·취소를 구분 |
| 과정 진단 | 공식 결과와 별도 필드. 판정 규칙·버전·미확정 보존 |
| 비용·사용량 | 보고·추정·미확인·해당 없음을 구분. 미확인을 0으로 채우지 않음 |
| 합성 자료 | `record_kind=illustrative`. 실제 결과·비용에 합치지 않음 |

한도 종료나 모델의 잘못된 도구 사용을 인프라 오류로 자동 제외하지 않는다. 실제 채점 가능 여부를 확인하고 배정·채점 완료·실패·결측 수를 보고한다.

## 비용과 분석 revision

작업자·기억 작성·조회 가공·사용자 시뮬레이터·채점의 호출을 구분한다. 한 요청에 업무·저장 판단이 섞이면 공유 결정 비용으로 남긴다. 캐시 세부 토큰과 기억 텍스트 추정치를 요청 총 사용량에 다시 더하지 않는다.

원천 수집·은행 생성 비용을 여러 평가에 중복 합산하지 않는다. 실제 지출과 조건별 배분 규칙, 운영 비용과 연구용 진단 비용을 함께 기록한다. 분석에는 코드·지표·주석 버전과 입력 원본 해시를 남긴다.

기존 레포의 특정 SWE 명령·패치·검색기·문자 상한은 이 계약의 기본값으로 가져오지 않았다. 이전 기록 규약에서 선별한 원칙의 출처는 [이관 내역](../migration.md)에 남긴다.
