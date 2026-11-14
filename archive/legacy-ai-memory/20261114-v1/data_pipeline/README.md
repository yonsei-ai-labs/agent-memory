# 데이터 수집·생성·학습 준비

[누적·분리 평가의 오프라인 준비 v2](../datasets/memory_context_session/20261113-offline-v2/README.md)는 `context_session_packet`으로 기존 1가족·4문항의 새 패킷을 생성함. 관계/득실/누적 효과, UNKNOWN, 조회·가상 추천의 계약을 분리하고 원문 해시와 부모 버전을 함께 기록함. 실제 직렬화 예산·누적 노출·국소 스냅샷은 `evaluation.context_session_inputs`로 CPU 검사하며 모델 실행 기능은 없음.

[현재 데이터 역할·누적 세션의 다음 계약](../docs/ai-memory-data-cleanup-and-context-stream-plan-2026-11-12.md)을 정리함. [CPU 전환 검사](../docs/design-data/memory-context-stream-20261112-v1/README.md)는 질문 답을 새 사실로 저장하지 않아도 기존 92정답이 유지되는지 확인하는 설계 검사이며, 실제 누적 작업자 실행기는 아직 없음. 새 긴 패킷은 이전 보완본과 부모 manifest가 달라 ID만으로 연결하지 않음.

현재는 실행 당시의 코드 위치를 보존하고 이 폴더에서 공용 진입점을 제공한다. 자료를 생성하는 코드와 고정된 자료의 역할을 구분하며, 동결 버전의 생성 스크립트는 그 버전의 근거로 남긴다.

[120출처 기억 선택 자료](../datasets/memory_source_scale/20261111-v1/README.md)는 `source_scale_data`로 고정 원천을 확보하고 `build_source_scale`로 검토한 발화·현재 관측 대조·출처 분할을 구성한다. 학습 2,352관계쌍과 별도 개발/시험을 보존하며 실제 득실 라벨과 구분한다. [30·60·120출처 학습 및 작업자 평가](../docs/reports/2026-11-11-memory-source-scale.md)까지 완료했다. 데이터 검사는 `python3 -m training.memory_selector.source_scale_protocol --data datasets/memory_source_scale/20261111-v1`로 실행한다.

[공개 기억 자료 파일럿](../datasets/memory_source_pilot/20261111-v1/README.md)은 `collect_relation_source_pilot`으로 고정 원천·출처 분할을 준비하고 `review_relation_source_pilot`으로 원문 입력·지정 관계 주석·품질 검산을 구성한다. 30출처 중 19개의 좁은 관계 단서를 유지했으며 실제 효용이나 완성된 선택 정답은 아니다. `python3 -m data_pipeline.review_relation_source_pilot check --output datasets/memory_source_pilot/20261111-v1`로 GPU·네트워크 없이 확인할 수 있다.

다음 자료의 기준은 [데이터 설계 노트](../docs/ai-memory-next-data-contract-2026-11-08.md)다. [역할 목록](../datasets/catalog.json)에서 기존 관계 감독·실제 효용·근거 후보를 구분하고, [검토 사례](../docs/design-data/memory-selection-next-20261108/review-queue.jsonl)에서 필요한 근거와 아직 없는 라벨을 확인한다.

최근 [근거·득실 분리 자료](../datasets/memory_decisions/20261109-evidence-v1/README.md)는 `memory_evidence_dataset`으로 기존 상태·묶음의 지정 기록 확보를 재현하고, `memory_evidence_review`로 원문 span과 임시 의미 주석을 검산한다. 지정 기록 확보를 의미적 충분성이나 실제 이득으로 바꾸지 않는다. [검토 규칙과 예시](../docs/design-data/memory-evidence-review-20261109-v1/README.md)에 현재 관측·후보·은행의 누락 구분과 새 출처 수집 기준이 있다.

후속 [현재 관측 입력 비교](../docs/reports/2026-11-09-memory-state-ablation.md)는 `prepare_memory_state_ablation`으로 기존 관측 237묶음의 네 결과 라벨을 준비하고, 같은 Harrier의 고정/LoRA 학습을 비교했다. 현재 관측으로 득실 예측은 개선됐으나 실제 선택 성공의 추가 이득은 없었다. 새 자료나 임시 충분성 주석을 학습 정답으로 추가하지 않았다. [공유 결과](../evaluation/results/memory-state-ablation-20261109-v1/README.md)의 예측 검산은 `export_memory_state_ablation check`로 GPU 없이 실행한다.

후속 [새 출처 사례 묶음](../docs/design-data/memory-evidence-discovery-20261108-v1/README.md)은 `collect_memory_evidence_discovery`로 공개 자료를 수집하고, `export_memory_evidence_discovery`로 출처 기억을 먼저 고정한 뒤, `review_memory_evidence_discovery`로 현재 코드·관계·준비 상태를 연결한다. `python3 -m data_pipeline.review_memory_evidence_discovery check`로 GPU 없이 공유 근거를 검산한다. 19개 관계 사례를 효용 학습 행으로 내보내지는 않는다.

[수집·증강·분류 통합안](../docs/ai-memory-data-collection-and-taxonomy-2026-11-08.md)은 다음 수집의 기준이다. 메타데이터 구간 확대 → 재사용 근거 연결 → 조건 변형의 코드·환경 확인 → 실제 제공 효과 측정 순서를 따른다. [분류표](../docs/design-data/memory-taxonomy-20261108-v1/README.md)는 수집 범위 검토용이며 학습 내보내기 대상이 아니다.

| 단계 | 실제 구현 | 입력·출력 |
| --- | --- | --- |
| 여행 작업자 효용 자료 | [travel_worker_pilot.py](travel_worker_pilot.py) | 고정 여행 참조 자료·식당 CSV → 결과를 보기 전에 고른 22요청·132과거 슬롯·두 현재 정보 조건; 실행·결과는 [후속 보고](../docs/reports/2026-11-09-travel-worker-utility.md) |
| 여행 참조 관계 수집·재현 | [memoryarena_travel.py](memoryarena_travel.py) · [reproduce_travel_references.py](reproduce_travel_references.py) | 고정 MemoryArena 여행 원문 → 기본 계획 45개 단위 분할, 명시적 참조 6,381개·후보 쌍 38,286개; 실제 효용 아님 |
| 실제 이슈·과거 경험 수집 | [collect_development_memory_sources.py](collect_development_memory_sources.py) | 고정 공개 parquet의 첫 row group·ID 기반 선별 → 77개 원본, 명령 실행 없음 |
| 진단 단서 구성·검토 | [development_memory_leads.py](development_memory_leads.py) · [review_development_memory_leads.py](review_development_memory_leads.py) · [자료 감사](audit_development_memory_leads.py) | 40이슈×16기억, 근거·주석·입력 시점·현재 저장소 분할; 실제 효과와 구분 |
| 경험 수집 | [run_pilot.py](../experiments/swe_memory/scripts/run_pilot.py) | 문제·설정 → `runs/<run_id>/`; 모델 호출·Docker 실행 |
| 기억 작성 | [build_memory_bank.py](../experiments/swe_memory/scripts/build_memory_bank.py) | 과거 실행 → 기억 은행; 모델 호출 |
| 관계 사례 구성 | [discovery-v1 생성 코드와 설명](../datasets/memory_relations/20261107-discovery-v1/README.md) | 출처 기억·공개 코드·국소 확인 → 주석·분할·근거. 기존 버전은 동결 |
| 배포 데이터 확인 | [relations.py](relations.py) `check` | 학습 파일 해시·ID·분할·대조를 읽기 전용 검사 |
| 학습 내보내기 | [relations.py](relations.py) `prepare` | 기존 내보내기 구현을 호출해 새 디렉터리에 학습 파일과 manifest 생성 |
| 근거 있는 범위 조합 | [scope_expansion.py](scope_expansion.py) · [collect_scope_sources.py](collect_scope_sources.py) | 검토한 실제 명령의 범위 식별자 → 통제한 요청·완료 관계. 생성에는 원본 로그·고정 parquet 필요 |
| 범위 자료 읽기 전용 감사 | [audit_scope_expansion.py](audit_scope_expansion.py) | 동결 해시·텍스트 라벨·출처 연결·분할·길이 규칙 검사 |
| 길이 통제 출처 선별 | [screen_length_challenge.py](screen_length_challenge.py) · [audit_length_challenge.py](audit_length_challenge.py) | 사전 고정한 shard 범위·선별 기준 → 새 두 저장소의 평가 전용 자료 |
| API 직접 재사용 진단 | [api_transfer_runtime.py](api_transfer_runtime.py) · [api_transfer.py](api_transfer.py) | pinned API 실행 → 과거 성공·현재 상태·네 절차의 관계 |
| API 버전 대조 | [api_version_challenge.py](api_version_challenge.py) | 별도 pandas 2.3.3/3.1.0 실행 → 실제 반환 계약 변화와 대조 |
| 고정 작업자 코드 판정 | [api_worker_judge.py](api_worker_judge.py) · [api_worker_interpreter.py](api_worker_interpreter.py) | 생성 코드를 제한 AST로 해석해 출력·원본 효과·호출 수 검사 |
| 효용 정책 고정 후 확인 판정 | [judge_api_worker_test.py](judge_api_worker_test.py) | 감사된 policy 선택 해시를 먼저 기록한 뒤 test worker 결과 판정 |
| 작업자 변경의 고정 선택 평가 | [judge_api_worker_transfer.py](judge_api_worker_transfer.py) | 9B 생성 전 동결한 4B 선택을 그대로 적용 |
| 실제 효용 데이터 공유 | [export_api_worker_utility.py](export_api_worker_utility.py) · [export_api_worker_transfer.py](export_api_worker_transfer.py) | 원본 코드·prompt·판정에서 가중치 없는 검토 묶음 생성 |
| 과제 실행 가능성 | [check_api_task_feasibility.py](check_api_task_feasibility.py) | 사후 인간 코드로 고정 fixture의 해답 존재 확인; 선택기 성능과 별도 |
| 실행 전제 출처 검토 | [고정 구간 검색](screen_memory_prerequisite_sources.py) · [선정 창 추출](extract_memory_prerequisite_review.py) | 공개 로그 36개 도구 호출의 포함·제외 검토; 학습 라벨이나 실제 기억 효용을 자동 생성하지 않음 |

## 공유 자료 확인

초기화 정보의 [파일럿 구성](memory_unit_pilot.py)과 [실행 판정](evaluate_memory_unit_pilot.py)은 실제 Django image의 프로세스 상태를 사용한다. [공개 후보 준비 상태 검토](review_memory_unit_eligibility.py)는 source 근거와 아직 없는 효용 라벨을 구분한다. [결과와 제약](../docs/reports/2026-11-08-memory-unit-pilot.md)을 함께 읽는다.

저장소 루트, Python 3.11 이상에서 실행한다. 추가 패키지나 GPU는 필요 없다.

```bash
python3 -m data_pipeline.relations check
python3 -m data_pipeline.audit_scope_expansion datasets/scope_relations/20261108-expansion-v1
python3 -m data_pipeline.audit_relation_coverage
python3 -m data_pipeline.audit_api_transfer datasets/api_transfer/20261108-v1
python3 -m data_pipeline.audit_api_version_challenge datasets/api_transfer/20261108-pandas-version-v1
python3 -m data_pipeline.verify_api_worker_utility datasets/api_worker_utility/20261108-v3
python3 -m evaluation.verify_api_worker_transfer datasets/api_worker_utility/20261108-9b-transfer-v1
python3 -m evaluation.verify_memory_units
```

## 새 학습 묶음 만들기

```bash
python3 -m data_pipeline.relations prepare --output training/memory_selector/data/relations-share-v1
```

원본 데이터의 154개 고정 파일을 검증한 뒤 학습 파일 5개와 manifest를 복사한다. 기존 출력 디렉터리가 있으면 중단한다. 원시 실행의 309개 참조까지 확인하려면 [전체 검산 절차](../docs/reproduction.md)가 별도로 필요하다.

내부적으로 [prepare_relations_benchmark.py](../training/memory_selector/prepare_relations_benchmark.py)를 호출한다. 기존 Colab 업로드 묶음과 과거 실행 코드의 연결을 유지하기 위한 호환 경로다. 새 수집·정규화·주석 기능은 이 폴더에 추가하고, 학습 코드는 데이터 버전과 manifest를 입력으로 받는다.

수치 근거 판독의 [prepare_data.py](../training/memory_selector/prepare_data.py)와 통제 범위 판독의 [prepare_scope_pilot.py](../training/memory_selector/prepare_scope_pilot.py)는 이전 개발 실험용이다. 원시 로그나 첫 학습의 저장 자료에 의존하므로 새 clone만으로 실행 가능한 관계 내보내기와 구분한다.

범위 자료의 포함·제외 이유는 [주 자료](../datasets/scope_relations/20261108-expansion-v1/README.md), 평가 전용 선별은 [길이 통제 자료](../datasets/scope_relations/20261108-length-challenge-v1/README.md)에 있다. tokenizer를 직접 재실행하는 길이 감사에는 `tokenizers`와 기록한 tokenizer 파일이 추가로 필요하다. 동결한 자료는 덮어쓰지 않고, 수집·생성을 다시 할 때 새 출력 경로를 사용한다.

실행 전제의 [검토 노트](../docs/design-data/memory-prerequisite-sources-20261108/README.md)는 성공한 설치, 설치해도 남는 오류, 소스 수정이 필요한 경우를 구분한다. 수집 개수를 학습 표본 수로 부르지 않으며, 원본 parquet를 읽는 선별·추출에는 `pyarrow`가 필요하다.
