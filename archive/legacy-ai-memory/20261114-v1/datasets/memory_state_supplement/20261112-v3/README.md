# 현재 관측과 기억 적용성의 대조 보완 자료

기존 숨은 함정 파일럿 4출처를 이용해 검토 입력, 여러 기록에 걸친 판단 근거, 같은 목표에서의 상태 대조를 보완함. **실행 전 설계 자료이며 실제 득실·새 모델 성적은 아직 없음.** 모든 출처는 `dev`이고 학습 사용은 승인되지 않은 상태임.

[보완 결과와 예시](../../../docs/reports/2026-11-12-memory-state-supplement.md) · [생성·검사 코드](../../../data_pipeline/memory_state_supplement.py)

## 규모와 단위

| 항목 | 규모와 의미 |
| --- | --- |
| 출처 | 기존 파일럿 4가족. 모두 이전 stage1 노출 이력이 있음 |
| 기존 판단 입력 | 84시점 × 명시/간접 표현 = 168사례. 긴 입력 단계는 주 학습 뷰에서 제외함 |
| 기존 후보 주석 | 5,472행. 모호한 1판단의 18행은 마스킹함 |
| 알려진 근거 누락 진단 | 20개 원 주석 × 2표현 = 40행. 단독 입력은 20개가 중복되어 독립 사례 40개가 아님 |
| 새 고정 조건 대조 | 원 사건 24개 × 2표현 × 2목표 = 96쌍·192상태 |
| 인수 검사 | 399사례·5,686입력, 고유 입력 5,666개·근거 구간 17,003개 |
| 작업자 조건 배정 | 192상태 × 6정책 = 1,152개. 실제 메시지는 402개로 중복 제거됨 |
| 작업자 실행·독립 사람 검토·새 학습 | 모두 0 |

후보별 주석, 표현 변형, 시점, 출처, 고유 입력을 독립 경험 수로 합산하지 않음. 자료 안의 정답 구성 규칙과 공개 규칙이 잘 맞는 것은 신경망 선택기의 필요성을 증명하지 않음.

## 구성

| 파일 | 내용 |
| --- | --- |
| `sources.jsonl` | 사용한 4출처의 기존 사용 이력·dev 역할 |
| `source-sessions.jsonl`, `source-events.jsonl`, `source-records.jsonl` | 원 세션·사건과 공개된 기록의 정규화 사본 |
| `current-inputs.jsonl` | 원 파일럿의 무기억 조건 실제 메시지. 카탈로그·현재 지침·요청 포함 |
| `relation-cases.jsonl`, `relation-targets.jsonl` | 모든 후보에 같은 공개 은행을 주는 판독 계약과 원 역할 주석 |
| `missing-premise-probes.jsonl` | 근거가 빠진 단독 입력과 완전 입력의 연결. 단독 입력은 `insufficient` |
| `unreviewed-candidate-masks.jsonl` | M/L의 미주석 후보 5,726출현을 `null`, `loss_mask=0`으로 보존 |
| `controlled-cases.jsonl`, `controlled-labels.jsonl`, `paired-controls.jsonl` | 목표·은행·대상 기억을 유지하고 관측을 바꾸는 대조 |
| `eligibility.jsonl` | 원문 근거와 현재 유효 예약의 존재에 따른 포함 규칙. 작업자 결과로 거르지 않음 |
| `review-packet.jsonl`, `review-key.jsonl`, `review-triage.jsonl` | 원 58항목, 16판단의 공개 입력 보완, 별도 정답과 의미 검토 우선순위 |
| `controlled-review-packet.jsonl`, `controlled-review-key.jsonl` | 새 192상태의 별도 검토표와 정답. 검토 완료 자료는 아님 |
| `worker-assignments.jsonl`, `worker-inputs.jsonl.gz` | 6정책 배정과 실제 메시지. 결과는 `null` |
| `protocol.json`, `audit.json`, `parent-manifest.json`, `manifest.json` | 범위·집계·부모 버전·고정 해시 |

## 입력과 정답의 경계

판독 입력의 전체 은행은 **선택기가 볼 후보 저장소**임. 작업자가 현재 이미 아는 관측으로 취급하지 않음. 모든 후보에 같은 은행을 제공하고, 정답의 `judge_with`만 골라서 입력에 추가하지 않음. 정답 근거 ID·역할·phase는 실제 입력에 넣지 않음.

새 대조의 같은 두 관측은 서로 다른 목표에 다시 사용됨. 예를 들어 Stefan의 예약을 쓰지 않는다는 관측은 Stefan의 옛 기록을 보류할 근거지만, Lena의 예약을 묻는 상황에서는 Lena 기록을 대신하지 못함. 관측 문장만 보면 현재 표에서 최대 50%이고, 관측을 뺀 목표·대상 기억만 봐도 최대 50%임. 이는 유한한 구성 표의 진단으로 모델 정확도가 아님.

`needs_record`와 `observation_sufficient`는 필요 정보의 대비임. 특히 `observation_coverage` 쌍은 시간순 before/after를 뜻하지 않음. 일부 current snapshot은 기존 공개 기록을 현재 확인한 것으로 구성한 통제 상태이며 실제 에이전트 실행 궤적이 아님.

## 평가 사용

동일 작업자·모델 revision·디코딩을 별도로 고정한 뒤 무기억, 대상 기록, 전체 기록, 목표 BM25 2k, 공개 문자열 규칙 2k, 필요 기록만 제공하는 진단을 비교할 수 있음. 현재 파일은 입력 명세이며 기존 Colab 실행기의 설정 파일로 바로 전달하는 형식은 아님. 모델·반복·실행 상한을 정하기 전에는 자동 실행하지 않음.

기억 예산은 실제 `Historical records` 블록의 머리말과 줄바꿈을 포함한 2,048토큰임. 전체 제공은 초과 여부를 별도로 표시한 진단 조건임. 같은 입력 해시의 정책은 작업자 출력을 공유함. 필요 기록 진단은 정답을 사용하는 조건이며 성능 상한이 아님.

`grade_control()`은 형식, 원래 과제 정답, UNKNOWN 출력, 대상 기억 제공, 현재 관측의 해소 여부를 구분함. 정답 값을 전달받지 못했을 때의 정상적인 보류를 작업 완료나 근거 없는 행동으로 혼동하지 않음. 주석의 필요/불필요와 관측 이득/손해도 구분함.

기존 후보 주석은 음성이 많으므로 전체 정확도만 쓰지 않음. 출처별 balanced accuracy, 근거 누락, 무효 기록 제공, 보류를 보고하고, 192상태의 두 상태 동시 정답을 별도 집계하는 것이 적절함. 40행의 불충분 진단은 이진 필요성 학습에 그대로 섞지 않음.

## 검사

정규화 자료의 검사는 원 작업 트리·토크나이저·GPU 없이 표준 라이브러리로 실행 가능함.

```bash
python3 -B -m data_pipeline.memory_state_supplement audit
python3 -B -m unittest tests.test_memory_state_supplement
```

새 폴더에 원문부터 재생성하려면 부모 패킷과 두 고정 tokenizer가 필요함. 기존 폴더는 덮어쓰지 않음.

```bash
.venv/bin/python -B -m data_pipeline.memory_state_supplement build \
  --source /path/to/pilot-20261112-v1 \
  --selector-tokenizer /path/to/harrier/tokenizer.json \
  --worker-tokenizer /path/to/source-worker/tokenizer.json \
  --data /tmp/memory-state-supplement-new
```

현재 관측과 전체 후보를 함께 읽는 실제 입력은 고정 Harrier tokenizer 기준 1,208–3,582토큰임. 원래 짧은 pair보다 읽기 비용이 커지므로 선택기를 포함한 순비용 개선은 별도 측정이 필요함. 사람의 의미 판정, 새 독립 출처, 작업자의 실제 득실은 아직 확보되지 않음.
