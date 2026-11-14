# 공개 기억 자료 30개 출처 검토

PersonaMem-v2와 Mem2ActBench에서 각각 15개 출처 그룹을 수집한 관계 주석 파일럿임. **30건 중 19건에서 좁은 관계 단서를 유지하고, 3건은 수정 필요, 8건은 모호하거나 근거 부족으로 보류함.** 19건은 완성된 선택 정답이나 실제 이득·손해 라벨이 아님. 단일 검토자의 지정 구간 주석이며, 나머지 기억은 미주석 상태임.

[결과와 확대 판단](../../../docs/reports/2026-11-11-memory-source-pilot.md)에 원본 정답의 문제, 살릴 관계, 다음 수집 기준을 정리함.

## 파일과 사용 범위

| 파일 | 내용 | 모델 입력 |
| --- | --- | --- |
| `inputs.jsonl` | 원래 질문, 공개 도구 스키마, 관측 대화에서 자른 기억 2,072조각 | 이 파일의 입력 필드만 사용 가능 |
| `annotations.jsonl` | 지정 관계·범위·39개 근거 위치, 미측정 효용 `null` | 제외 |
| `review-decisions.jsonl` · `review-table.csv` | 30건 모두의 유지·수정·보류 근거 | 제외 |
| `provenance.jsonl` | 기억 ID와 원본 파일·턴 번호 연결 | 학습 메타데이터로만 사용 |
| `upstream-supervision.jsonl` | 원본 정답·선호·출처 ID 등 선택된 행의 감독 메타데이터 | 제외 |
| `sources/` | 검산용 원문 대화와 원본 README·열 설명 | 직접 모델에 전달하지 않음 |
| `source-reservation.jsonl` | 200출처 그룹의 train 120·dev 40·test 40 사전 배정 | 분할 메타데이터 |
| `selection-freeze.json` · `selection-summary.json` | 선정 해시·구간·원천 집계 | 제외 |
| `upstream-files.json` · `manifest.json` · `audit.json` | 원천 URL·해시, 공유 파일 해시, 검산 결과 | 제외 |

현재 입력에는 에이전트 진행·실패 관측이 없어 각각 `null`로 둠. 과거 대화 속 도구 실패를 현재 환경의 실패로 복사하지 않음. PersonaMem의 생성 프로필이 든 system 메시지 15개를 입력에서 제외함. assistant의 관측 발화는 원문대로 보존하되 사용자 사실의 증명으로 자동 승격하지 않음.

기억 조각은 정답을 보지 않고 user 턴을 기준으로 자름. Mem2Act는 선택된 출처 그룹의 전체 세션을 포함하고, PersonaMem은 32k 대화 전체에서 system 메시지만 제외함. 정답 snippet만 골라 전달한 은행이 아님. 원본 출처 ID는 모델 기억에서 제거하고 중립적인 기억 ID로 연결함. 도구 스키마의 기본값은 관측 가능한 계약으로 유지함.

`training_approved=false`는 아직 완성된 학습 행으로 변환하지 않았음을 뜻함. 이 파일럿은 자동 학습 내보내기에 연결하지 않음. `minimal_sufficient_sets=null`이며, 미주석 조각을 음성으로 바꾸거나 관계 단서를 실제 득실로 바꾸지 않음. 재사용 철회는 선호의 사실적 반대가 아니며, 타인의 이야기는 현재 사용자에게 해당 사실이 없다는 증거가 아님.

## 선정과 분할

내용·모델 결과를 검토하기 전에 출처 그룹의 SHA 순서로 자료별 100개를 예약함. 각 자료에서 train 60·dev 20·test 20이며, 이번 15개씩은 train 안에서만 고름. 예약한 200개는 다운로드·주석 완료한 200개가 아님. 파일럿 30개를 포함해 train 120개이며, 남은 train 예약은 90개임.

PersonaMem은 사용자 단위로 묶음. 원본 train의 800명과 val의 735명 중 735명이 겹치므로 원래 두 파일을 독립 검증 분할로 사용하지 않음. Mem2Act는 질문 출처뿐 아니라 같은 혼합 세션의 배경 대화까지 전이적으로 묶어, 출처 ID가 있는 393문항을 334그룹으로 구성함. 출처 ID가 없는 7문항은 별도 기록하고 선정에서 제외함. 같은 생성기·주제·표현이 남을 수 있어 그룹 분리를 통계적 독립성의 보장으로 해석하지 않음.

Mem2Act 선정은 L4 5·L3 2·L2 5·L1 3건임. 사전 예약 train에 L3 출처가 2개뿐이어서 내용 검토 전에 예정 L3 3·L2 4를 조정했으며 분할은 바꾸지 않음. PersonaMem은 updated/self·others·stable/self 각 5건임. 관계별 층화 표본이므로 19/30을 전체 자료의 품질 추정치로 사용하지 않음.

## CPU 검산

저장소 루트, Python 3.11 이상에서 네트워크·GPU 없이 실행함.

```bash
python3 -m data_pipeline.review_relation_source_pilot check --output datasets/memory_source_pilot/20261111-v1
python3 -m unittest evaluation.tests.test_relation_source_pilot -v
```

30개 입력과 원문 대조, 근거 위치 39개, PersonaMem snippet 턴 50개, 원천 분할의 중복, 입력 필드 제한, 파일 해시를 검사함. 이 검산은 의미 주석의 독립 재심사를 대신하지 않음. 검토는 지정 근거와 질문·정답의 관계에 한정하며 2,072개 후보 전체의 관련성·충분성을 주석한 것이 아님.

원천을 다시 받아 새 경로에 재구성하려면 다음을 사용함. 약 175MB의 기본 자료와 선정한 사용자 대화를 내려받으며 모델 호출은 없음. 기존 동결 폴더를 출력 경로로 지정하지 않음.

```bash
python3 -m data_pipeline.collect_relation_source_pilot collect --output runs/relation-source-replay
python3 -m data_pipeline.collect_relation_source_pilot histories --output runs/relation-source-replay
python3 -m data_pipeline.review_relation_source_pilot prepare --intake runs/relation-source-replay --reviews datasets/memory_source_pilot/20261111-v1/review-decisions.jsonl --output runs/relation-source-rebuilt
```

다운로드가 중단되면 `collect --resume`을 사용함. 기존 manifest에 기록된 파일은 해시를 확인하고 재사용함. 로컬 준비 때의 재개 여부 같은 수집 로그는 재실행에 따라 달라질 수 있으나 원문 바이트와 입력·주석·출처 연결은 고정됨. 해시는 이 묶음의 동결 이후 변경을 검출하며 출처 전체의 사실성을 보증하지 않음.

## 출처와 이용 조건

- [PersonaMem-v2](https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2), revision `ed956dea41521fc4499acbc63f966e0fd3c053ba`. 원본 카드의 CC-BY-4.0 표기를 보존함. 생성된 persona·대화이며 실제 사용자의 자연 발생 경험으로 부르지 않음.
- [Mem2ActBench](https://github.com/Cantaloupe-M/Mem2ActBench/tree/c935c65be9aa19c930b60657c0c4b2b7ea13a34a), revision `c935c65be9aa19c930b60657c0c4b2b7ea13a34a`. 고정 README는 MIT License를 선언하며 해당 revision 루트에 별도 LICENSE 파일은 없음. 여러 공개 대화·도구 자료로 구성한 벤치마크임. 이번 조회는 실제 도구를 재실행하지 않음.

원본 저작자·자료명·URL·revision을 유지함. 이 저장소의 변환은 대화 분절, 모델 입력에서 system·출처 메타데이터 제외, 출처 그룹 분할, 한국어 검토 주석 추가임. 원본 대화 문장은 편집하지 않음.
