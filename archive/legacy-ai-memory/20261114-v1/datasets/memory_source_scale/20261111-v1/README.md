# 120출처 그룹의 기억 선택 학습 자료

Mem2ActBench와 PersonaMem-v2의 관측된 사용자 발화에서 현재 질문에 참고할 정보를 선정하고, 그 정보가 현재 관측에 없는 상태와 이미 있는 상태를 구성함. 학습용 **120출처 그룹·127원본 질문·2,352관계쌍**이며, 개발 30그룹과 시험 31그룹을 별도로 둠. 실제 이득·손해 라벨은 모든 관계 행에서 `null`임.

[학습·작업자 평가 보고서](../../../docs/reports/2026-11-11-memory-source-scale.md)와 [출력·가중치 검산 묶음](../../../evaluation/results/memory-source-scale-20261111-v1/README.md)을 함께 제공함.

| 분할 | 출처 그룹 | 원본 질문 | 통제 상태 | 감독 관계쌍 |
| --- | ---: | ---: | ---: | ---: |
| 학습 30 | 30 | 32 | 96 | 555 |
| 학습 60 | 60 | 63 | 189 | 1,080 |
| 학습 120 | 120 | 127 | 381 | 2,352 |
| 개발 | 30 | 31 | 93 | 498 |
| 시험 | 31 | 31 | 93 | 498 |

학습 120그룹은 Mem2Act 66그룹과 PersonaMem 54사용자임. 30·60그룹은 그 안에 포함되며 각각 15+15, 30+30임. 후보 기억을 제공하는 출처도 각 학습 크기 안으로 제한함. 30그룹 학습의 방해 기억에 나머지 90그룹을 섞지 않음.

## 정답의 의미

Mem2Act는 검토한 원문에서 식별자·대상·설정 등의 관측 단서를 정하고, 그 단서를 공급하는 기억을 양성으로 둠. 같은 분할의 다른 기억 중 해당 단서의 승인된 표현이 없는 기억을 후보 음성으로 구성함. **음성은 좁은 목표 사실의 표현을 배제한 감독이며, 모든 의미적 도움을 독립 검토한 정답은 아님.** 시험 후보 150개도 읽어 좁은 단서 주석을 확인했지만, 다른 주제의 음성이 많아 어려운 동일 대상·조건 충돌 평가로 해석하지 않음.

PersonaMem은 사용자 발화에서 확인한 개인 맥락 한 조각만 감독함. 예를 들어 원문에 헤드폰만 있으면 소음 제거 기능을 사실로 추가하지 않고, 일회성 경험을 지속적인 선호로 승격하지 않음. 다른 후보 기억은 미주석으로 남기며 음성으로 계산하지 않음. 따라서 이 부분의 분류 점수는 **관련 맥락의 새로움과 중복 판독**이며, 전체 기억의 관련성 순위 성적이 아님. 추천 답변의 정오를 작업 성공 라벨로 사용하지 않음.

PersonaMem의 후보와 무관한 관측에는 같은 분할의 다른 사용자 발화도 포함됨. 단일 사용자의 실제 기억 저장소를 재현한 구성이 아닌 보조 판독 과제임. 발화 중 작성 요청·초안에 담긴 내용도 해당 문맥의 관측으로만 사용하며, 실제 인물의 지속적인 특성으로 해석하지 않음.

두 자료 모두 질문·관측·기억 원문만 모델에 제공함. 원본 정답, 생성된 persona 프로필, 잠재 선호, 근거 ID, 감독 사실 이름은 모델 입력에서 제외함. 단일 에이전트의 주석이므로 독립된 사람의 합의 주석과 구분함.

## 현재 관측의 대조

원래 질문마다 세 상태를 구성함.

1. `missing`: 추가 관측이 없음.
2. `known`: 승인한 단서를 공급하는 원문을 현재 관측에 이미 제공함.
3. `unrelated`: 필요한 단서를 담지 않은 다른 원문을 현재 관측으로 제공함. 가능한 한 `known` 관측과 길이가 가까운 조각을 선택함.

이는 자연 실행에서 수집한 진행·실패 이력이 아닌 통제 상태임. 같은 질문의 세 상태를 독립 경험 세 개로 세지 않음. 현재 관측에 원문이 그대로 있는 대조는 문자열 중복 제거 규칙으로도 풀 수 있어 그 규칙을 비교 대상으로 유지함.

문항마다 후보 10개를 구성하며 정답 근거가 후보에 있도록 보장함. 원래 긴 대화 전체에서의 검색 재현이 아님. 기본 전달 정책은 최대 두 기억·512토큰이며, 개수 제한의 효과를 확인하기 위해 512토큰 안에서 더 많은 기억을 전달하는 별도 진단을 둠.

## 출처 분할과 검토 기록

[이전 30출처 검토](../../memory_source_pilot/20261111-v1/README.md)의 200그룹 예약을 유지함. PersonaMem 사용자와 Mem2Act의 혼합 세션 배경 출처까지 전이적으로 묶어 분리함. 예약된 train을 검토하고, 채택하지 않은 그룹은 예약되지 않은 Mem2Act 그룹으로 보충함. 기존 dev/test를 train으로 이동하지 않음. 검토 후 사용하지 않은 예약 그룹은 원래 분할에 남음.

후보의 출처가 분리돼도 같은 생성 방식·표현·주제는 남을 수 있음. 이 자료는 공개 합성 대화 벤치마크에서 만든 파생 자료이며 181개의 자연 발생 독립 에이전트 경험을 뜻하지 않음.

| 파일 | 내용 |
| --- | --- |
| `train-*-cases.jsonl`, `dev-cases.jsonl`, `test-cases.jsonl` | 질문·현재 관측·고정 후보 은행 |
| 같은 접두어의 `*-labels.jsonl` | 부분 감독, 필요한 사실, 대체 근거, 진단용 정답 선택 |
| `records.jsonl`, `provenance.jsonl` | 후보 원문 1,699개와 원문 내 정확한 구간 |
| `train-reviews.json`, `heldout-reviews.json` | 채택 단서와 해석 범위 |
| `review-ledger.jsonl` | 채택·미채택·예비 후보 기록. 미채택을 전부 원본 오류로 세지 않음 |
| `tasks-supervision-only.jsonl` | 원본 도구 인수·스키마와 주석. 모델 입력으로 직접 사용하지 않음 |
| `source-order.json`, `source-reservation.json` | 중첩 학습 순서와 분할 |
| `manifest.json` | 동결한 데이터 파일 20개의 해시·집계·한계 |

## 재현

저장소 루트에서 데이터·출처 구간·분할을 네트워크와 GPU 없이 검사함.

```bash
python3 -m training.memory_selector.source_scale_protocol --data datasets/memory_source_scale/20261111-v1
```

학습 입력의 토큰 길이까지 확인하려면 Harrier의 고정 tokenizer와 `transformers`가 있는 환경에서 `--tokenizer-check`를 추가함. 이번 실행의 최대 입력 길이는 591토큰이며 잘림이 없음. 공개 원천과 검토 파일로 다시 생성한 20개 데이터 파일이 원본과 바이트 단위로 일치함.

원천 재구성은 이전 수집기의 고정 revision 다운로드와 다음 단계로 수행함. 원천 파일의 로컬 기본 경로는 `runs/memory-relation-intake-20261111-v1`임. 다운로드·검토 이력은 결과 묶음의 `quality/source-download-manifest.json`에 보존함.

```bash
python3 -m data_pipeline.source_scale_data histories
python3 -m data_pipeline.source_scale_data histories --heldout
python3 -m data_pipeline.build_source_scale --reviews datasets/memory_source_scale/20261111-v1 --output runs/source-scale-rebuilt
```

재구성에는 `tokenizers`와 고정 Qwen3.5-4B tokenizer revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`가 필요함. 모델 가중치 실행은 필요하지 않음. 기존 동결 폴더를 출력 경로로 지정하지 않음.

## 원천과 변경

- [PersonaMem-v2](https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2), revision `ed956dea41521fc4499acbc63f966e0fd3c053ba`, 원본 카드 CC-BY-4.0. 생성된 사용자와 대화임.
- [Mem2ActBench](https://github.com/Cantaloupe-M/Mem2ActBench/tree/c935c65be9aa19c930b60657c0c4b2b7ea13a34a), revision `c935c65be9aa19c930b60657c0c4b2b7ea13a34a`. 원본 README가 MIT License를 선언하며 해당 루트에 별도 LICENSE 파일은 없음.

변경 내용은 사용자 원문의 구간 선택, 생성 프로필 제외, 출처 단위 분할, 제한된 후보 은행 구성, 현재 관측의 통제 대조, 좁은 단서 주석임. 원문 문장을 바꿔 사실을 추가하지 않음. 도구 호출·예약·결제 등 원문 속 요청은 실행하지 않음.
