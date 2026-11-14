# 관계 자료 재검토·출처 보완 v2

원본 `development_memory_leads/20261108-v1`과 기존 재검토 v1을 보존한 별도 묶음임. 원본 판단·성적을 소급해서 교체하지 않음.

- `review-batch/reviewer.zip`: 검토자에게 전달할 91개 사례·출처·빈 양식·안내문만 포함함. 기존 정답은 포함하지 않음.
- `review-batch/maintainer/`: 기존 정답·선정 집단·표집 확률·원본 ID를 관리함. 맹검 검토자에게 전달하지 않음.
- `source-issue-contexts.json`: 원본 raw record 해시를 대조하고 복원한 과거 이슈 16개. 이슈 보고와 실제 실행 관측은 별개임.
- `source-card-corrections.json`: 실제 수정 대상이 잘못 요약된 Redis 카드, 과거 이슈 문맥과 수정 범위를 구분해야 하는 Python 카드의 정정 문안임. 카드가 바뀌면 각각 연결된 기존 40쌍을 다시 검토해야 함. 기존 라벨의 자동 승계는 허용하지 않음.
- `first-pass-nonblind.jsonl`: 기존 양성 18개, 빈약한 현재 관측의 음성 1개, Redis 출처 문제 관련 4개의 근거 연결 초안임. AI가 기존 정답을 본 뒤 작성한 비맹검 판정이며 학습 정답이 아님.
- `first-pass-summary.json`: 23개 1차 판정과 남은 68개를 집계함. 독립 판정 완료는 0개임.

91개는 기존 진단 표본 64개와 query별 남은 음성 중 균등 추출한 27개로 구성됨. 전체 640쌍의 단순 무작위 표본이 아님. 진단 표본과 추가 표본을 합친 변경 비율로 전체 오류율을 추정하지 않음. 현재 관측이 잘린 21행은 원문 범위·잘림 여부를 표시했으며 임의로 내용을 채우지 않음.

양성 18개에 대한 1차 의견은 12개 유지, 5개 미확인, 1개 다른 점검 목표임. 여기서 양성은 구체적인 진단 행동의 연결이지 같은 원인·같은 수정법·실제 이득을 뜻하지 않음. 12개 중 일부는 현재 요청에 이미 있는 정보라서 추가 효용은 별도 검증 대상임.

```bash
python3 -m data_pipeline.memory_relation_review --output /tmp/memory-review-v2-new
```

위 명령은 커밋한 출처 보충 자료로 묶음을 재생성함. 원자료가 로컬에 있는 경우 `extract_source_contexts(intake)`로 raw record 해시까지 다시 대조할 수 있음. 배포 묶음에서 임의의 원자료 다운로드나 모델 호출은 하지 않음. [검토 안내](reviewer-guide.md), [보완 보고서](../../reports/2026-11-09-data-remediation-implementation.md)를 함께 참조함.
