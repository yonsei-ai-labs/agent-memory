# 데이터

[누적·분리 평가의 오프라인 준비 v2](memory_context_session/20261113-offline-v2/README.md): 기존 1가족·4문항의 평가 계약, 버전과 본문 해시를 묶는 기록 연결, 실제 직렬화 예산 검사를 고정함. 대본 답의 17기록을 제외하고 모의 답변 2종의 32입력·제공/보류 16쌍을 CPU로 검사함. 실제 모델 호출·득실 측정은 없으며, 원본 552사례의 수정본이나 독립 평가로 합산하지 않음.

[Luna 누적/분리 세팅용 4문항](memory_context_session/20261112-luna-smoke-v1/README.md): 기존 1가족의 42관측·79청크와 4질문을 고정함. 대본 정답의 예약 기록 17개를 제외했으며 원문·정답·실제 결과를 분리함. 학습용 자료나 독립 평가가 아니며, v1의 누적 노출·조회 지시 한계를 보존함.

[현재 자료 역할·누적 실험안](../docs/ai-memory-data-cleanup-and-context-stream-plan-2026-11-12.md): 학습 증강 / 이전 부모의 고정 상태 진단 / 새 긴 세션 패킷을 구분함. [역할 목록](catalog.json)의 `current_focus`에 manifest·이전 부모·실행 전 보완 사항을 연결함. 행·표현·길이 변형을 새 독립 경험으로 합산하지 않음.

[새 숨은 함정 세션](hidden_memory_traps/pilot-20261112-v1/README.md): 4설계 세션·92판단·552사례·3,468고유 입력. 긴 기억 은행과 고정 과거의 관계 정답을 제공함. 실제 득실·사람 주석은 미측정이며, 예산 계산과 작은 선택기의 입력을 보완해야 함. 아래 보완본과 부모 버전이 다르므로 기억 ID만으로 결합하지 않음.

[현재 관측 대조 보완](memory_state_supplement/20261112-v3/README.md): 기존 파일럿 4출처에서 검토 입력·근거 누락·마스킹을 보완하고, 목표·은행·대상 기억을 고정한 96쌍·192상태를 구성함. 같은 두 관측을 두 목표에 교차해 관측만 보는 지름길을 점검함. 실제 득실·새 학습은 아직 없으며 설계용 dev 자료임.

[현재 상태·관측 관계 증강](memory_state_augmentation/20261112-v1/README.md): 기존 학습 11가족의 원 기록 477개를 보존하고 1,056상태·고유 판독 입력 4,224쌍을 구성함. 분기 변경·관측 추가·동등 표현의 대조와 정답을 분리한 검토 24사례를 포함함. 새 독립 출처·실제 득실 라벨·학습 결과는 없음.

[데이터 보강의 원인 분해](memory_augmentation_ablation/20261112-v1/README.md): 같은 train 출처를 이용한 repeat / format / relation 조건임. 주 비교 format/relation은 각 400항목·고유 입력 395개·후보 716쌍·양성 310쌍과 손실 가중치를 맞추고, 복합 기억의 부분 관측 80행을 달리함. 평가 12출처는 이전 결과를 본 원인 진단 자료임.

[STATE-Bench 검토 경험 은행](statebench_pilot/20261112-v1/README.md): 원문 20기록·근거 80곳을 연결한 조건부 경험 20개와 결과를 보기 전 선정한 개발 과제 8개임. 실제 24실행의 성적은 별도 결과에 보존하며, 개별 기억의 적용성·득실 정답은 아직 없음.

[STATE-Bench 학습 기록 준비](statebench_preparation/20261112-v1/README.md): 공개 train 300기록·관측 접두부 860개·도구 관측 후보 2,842개. 출처 분할·원문 해시·지정 구간 내용 검토를 포함하며 선택 정답·실제 득실 라벨은 아직 없음.

[ConvoMem·AMA 외부 평가 자료](memory_external_test/20261112-v1/README.md): 추가 학습에 사용하지 않은 200출처·200문항과 공통 후보 9,399개를 고정함. 원래 질문·답변·이력과 출처를 보존하며 새 학습용 관계 라벨로 합치지 않음. 공식 전체 벤치 점수와 통제 부분 평가를 구분함.

[복습 대조와 새 출처](memory_balanced_replay/20261112-v1/README.md): 동일 핵심 320상태와 복습 80상태의 교체, 미사용 예약 12출처의 240상태, 기존 BEAM 원문 은행의 예산 진단을 구성함. 불확실한 6개 학습 라벨을 가리고 관계 주석과 실제 작업 결과를 분리함. 상태 수를 독립 출처 수로 세지 않음.

버전이 고정된 자료와 출처·입력·라벨·분할 계약을 보관한다. 수집·생성과 학습 내보내기는 [data_pipeline](../data_pipeline/README.md)을 따른다.

[공개 기억 자료 120출처 학습](memory_source_scale/20261111-v1/README.md)은 학습 120출처·2,352관계쌍, 별도 개발 30출처·시험 31출처를 보관한다. 좁은 사실 감독과 현재 관측의 통제 대조이며 PersonaMem의 다른 후보는 미주석이다. [학습·고정 작업자 결과](../docs/reports/2026-11-11-memory-source-scale.md)와 실제 출력 검산 묶음을 함께 공개한다.

[공개 기억 자료 30출처 파일럿](memory_source_pilot/20261111-v1/README.md)은 PersonaMem-v2·Mem2ActBench의 원문 후보와 지정 구간 검토를 보관한다. 19개의 좁은 관계 단서와 보류 11건을 구분하며, 실제 득실 라벨과 완성된 선택 학습 행은 아직 없다. 200출처의 분할 예약을 200개 학습 자료 확보로 세지 않는다.

다음 실험에 사용할 역할은 [데이터 목록](catalog.json)과 [판단 시점·라벨·행동의 설계 노트](../docs/ai-memory-next-data-contract-2026-11-08.md)에 정리했다. 목록은 기존 자료를 합친 학습 데이터가 아니며, 이미 확인한 test를 새 독립 평가로 바꾸지 않는다.

[새 출처의 조건 대조](../docs/design-data/memory-evidence-discovery-20261108-v1/README.md)는 과거 기억 12개·현재 문제 18개·관계 검토 19쌍이다. 현재는 실행 준비를 위한 근거 묶음이며 실제 효과는 모두 미측정이고 학습 내보내기는 허용하지 않는다.

[수집·증강·분류 통합안](../docs/ai-memory-data-collection-and-taxonomy-2026-11-08.md)은 다음 자료의 확대·주석·평가 기준을 정한다. [기존 자료의 분류표](../docs/design-data/memory-taxonomy-20261108-v1/README.md)는 원본을 수정하지 않은 검토용 연결이며 새 데이터셋이나 효용 라벨로 합산하지 않는다.

| 버전 | 규모 | 용도 |
| --- | --- | --- |
| [memory_source_scale / 20261111-v1](memory_source_scale/20261111-v1/README.md) | 학습 120출처·127질문·2,352관계쌍; 개발 30·시험 31출처 | Mem2Act 사실 공급·PersonaMem 부분 감독, 관측 없음/기확보/무관 대조; 실제 득실과 별도 |
| [memory_decisions / 20261110-refresh-v1](memory_decisions/20261110-refresh-v1/README.md) | 기존 12가족·96상태·504배정, 비어 있지 않은 후보 408개 | 원래/추가 기억의 득실을 같은 작업자 환경에서 전부 재측정; 새 독립 출처·선택기 학습 결과 아님 |
| [travel_candidate_search / 20261109-v1](travel_candidate_search/20261109-v1/README.md) | 기존 전체 질문 1,221개·45계획 재사용 | 고정 판독기에서 후보 폭 32/64와 검색용 LoRA 비교; 새 독립 자료 아님 |
| [travel_retrieval / 20261109-full-request-v1](travel_retrieval/20261109-full-request-v1/README.md) | 같은 기본 계획 45개·전체 질문 1,221개·질문당 과거 기록 26–336개 | 전체 요청의 여러 명시적 참조 검색; 이전 test 노출, 실제 득실과 별도 |
| [travel_worker_utility / 20261109-pilot-v1](travel_worker_utility/20261109-pilot-v1/README.md) | 11계획의 22요청·과거 슬롯 132개 | 구성한 과거 선택을 실제 생성하고 5정책의 실제 득실 측정; 두 현재 정보 조건·4B/9B는 같은 출처의 반복이며 새 독립 표본이 아님 |
| [memoryarena_travel / 20261109-reference-v1](memoryarena_travel/20261109-reference-v1/README.md) | 기본 계획 45개, 명시적 참조 요청 6,381개×6후보 | 공개 참조 이력의 사람·날짜·항목 판독; 자체 train/dev/test 30/7/8계획, 공식 전체 점수·실제 효용과 별도 |
| [development_memory_leads / 20261108-v1](development_memory_leads/20261108-v1/README.md) | 실제 현재 이슈 40개×과거 기억 16개, 640쌍 | 진단 단서의 관련성 주석; 공유 기억 은행, 현재 저장소 분리; 실제 효용 아님 |
| [memory_units / Django startup pilot](memory_units/20261108-django-startup-pilot-v1/README.md) | 1 source 가족·2 실제 버전·8 재구성 상태, 40개 비교+4재생성 | 초기화 관측과 수정 절차의 실제 제공 효과 진단; 학습/독립 SWE 평가 아님 |
| [memory_relations / 20261107-discovery-v1](memory_relations/20261107-discovery-v1/README.md) | 관계 74행, 출처 경험 10개, 연결 가족·저장소 6개 | 현재 코드·관측과 과거 경험 사이의 제한된 관계 판독 |
| [scope_relations / 20261108-expansion-v1](scope_relations/README.md) | 최대 학습 1,316행, 17과제 ID·32실행 기록·7저장소; 별도 외부 108행 | 실제 선택 범위 식별자와 통제한 현재 요청·완료 기록의 두 관계 |
| [scope_relations / 20261108-length-challenge-v1](scope_relations/20261108-length-challenge-v1/README.md) | 새 2저장소·3실행·6명령, 108행 | 파일명과 전체 입력 길이의 지름길을 막은 평가 전용 자료 |
| [api_transfer / 20261108-v1](api_transfer/20261108-v1/README.md) | 34구현·7라이브러리, 96상태×4후보, 544번 실행 | 저작한 API 절차의 반환/원본 변경 계약을 실제 실행으로 확인 |
| [api_transfer / pandas-version-v1](api_transfer/20261108-pandas-version-v1/README.md) | 72행, 실제 정답 변화 2쌍 | pandas 2.3.3/3.1.0 반환 계약의 같은 가족 버전 스트레스 검사 |
| [api_worker_utility / 4B v3](api_worker_utility/20261108-v3/README.md) | 같은 96상태×10행동, 960개 기본 생성+4반복 | 직접 적용 라벨과 별개로 측정한 실제 작업자 출력·실행·비용 |
| [api_worker_utility / 9B 전이](api_worker_utility/20261108-9b-transfer-v1/README.md) | 같은 확인 28상태×10행동, 280개 기본 생성+4반복 | 4B에서 고정한 28개 정책의 다른 작업자 전이; 새 독립 자료는 아님 |

74행은 독립된 74개 경험이 아니다. 모든 출처가 이미 검토된 발견용 자료이며 최종 독립 test는 없다. 라벨 `supported / contradicted / not_established`는 질문한 관계의 근거 상태다. 실제 기억 제공의 이익·손해나 자동 제공·보류 정답으로 바꾸지 않는다.

범위 자료의 1,316행도 독립 경험 수가 아니다. 과거 명령의 전체 범위 선택은 관측이고, 현재 요청·완료 기록은 통제해서 구성했다. 범위 자료는 두 3분류 출력이며 자연 관계 74행의 단일 3분류와 다른 과제다. [출처 검토와 데이터 설계의 교훈](../training/memory_selector/reports/2026-11-08-scope-data-quality.md)을 함께 읽는다.

API 자료는 실제 라이브러리 실행을 사용하는 **저작한 진단**이며 자연 에이전트 경험을 수집한 자료가 아니다. 과거 절차의 직접 재사용 가능성 라벨과, 그 기억을 받은 LLM 작업자의 실제 득실을 구분한다. 544회 실행·384행·96상태를 서로 독립된 경험 수로 바꾸지 않는다.

작업자 효용 자료의 4B 학습·개발·확인은 50/18/28상태다. 확인 결과 채점 전에 head·정책 선택을 동결했다. 9B에는 동일한 선택을 옮겼다. 공개된 코드 출력과 실행 trace에서 숫자를 검산할 수 있으나, 가중치와 원래 token 배열을 포함한 전체 감사에는 원본 upload/results 묶음이 추가로 필요하다.

## 자료를 읽는 순서

1. `task-contract.json`: 무엇을 판단하며 어떤 입력을 허용하는가.
2. `learner-inputs.jsonl`: `sample_id`와 두 예측 문자열.
3. `supervision.jsonl`: ID로 결합하는 정답·주석 메타데이터. 모델 입력에는 넣지 않는다.
4. `group-splits.json`, `paired-contrasts.json`: 출처·파생 사례를 묶은 개발 분할과 조건 대조.
5. `relations.jsonl`, `case-table.csv`: 근거·관측 시점·사례 설명.
6. `artifact-index.json`: 고정 파일의 경로·크기·해시.

## 배포 자료만 확인하기

```bash
python3 -m data_pipeline.relations check
python3 -m data_pipeline.audit_scope_expansion datasets/scope_relations/20261108-expansion-v1
```

표준 라이브러리로 학습 파일 5개의 해시, ID 결합, 개발 분할, 대조 그룹을 확인한다. 원시 `runs/`나 Docker·GPU가 필요하지 않다. 이 검사는 출처 로그 전체를 다시 검산한 것이 아니다.

전체 근거 검산에는 기존 원시 실행과 추가 환경이 필요하다. [버전별 안내](memory_relations/20261107-discovery-v1/README.md)와 [재현 범위](../docs/reproduction.md)를 따른다. 동결 후 `build_*`, `audit_dataset.py`, `freeze_dataset.py`를 원본 폴더에서 다시 실행하지 않는다. 수정 자료는 새로운 버전으로 만든다.
