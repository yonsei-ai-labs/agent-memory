# 재현 방법과 필요한 자료

[STATE-Bench 실제 업무 파일럿의 CPU 재현](../evaluation/results/statebench-pilot-20261112-v1/README.md): 공유 압축만으로 선택 65회·작업자 단계 209개·도구 재실행 167개와 성공 5/8·6/8·5/8을 검산함. Python 3.12와 명시된 의존성이 필요하며 API·GPU·기본 모델 캐시는 필요 없음. 새 모델 추론이나 독립 언어 채점은 아님.

공유 저장소의 첫 실행은 **Python 3.11 이상만으로 저장 예측에서 결과표를 다시 계산**하는 것이다. 모델 학습과 전체 원본 감사는 추가 자료·환경이 필요한 별도 단계다. 아래 명령은 저장소 루트에서 실행한다.

[통합 리뷰](../training/memory_selector/reviews/2026-11-08-integrated/README.md)에서 GitHub 새 clone의 31개 검사 명령과 별도 CPU 가중치·작업자 코드 재실행을 확인했다. 재검산 범위별 근거를 구분한다.

과거 색인은 모두 현재 경로의 불변성을 보장하지 않는다. 11월 5일 작업 공간 색인의 가변 경로 5개는 이후 버전과 다르다. 당시 누적 목록 두 개와 집계 코드는 동일 바이트 사본을 찾았지만, 당시 데이터 안내 README와 집계 테스트 두 파일의 동일 버전은 Git 현재 파일·기록에서 찾지 못했다. [사본 연결과 미확인 항목](../training/memory_selector/reviews/2026-11-08-integrated/historical-index-reconciliation.json)을 따른다. 이 두 과거 버전의 완전 복원과 현재의 결과표 재현은 별개이며, 원래 색인의 해시를 현재 값으로 고쳐 통과시키지 않는다.

## 1. GPU 없이 결과표 재계산

전체 질문·후보 검색 학습의 최신 두 결과는 먼저 고정된 공개 원문을 재구성함. Python 표준 라이브러리만 사용하며 원문 다운로드에는 네트워크가 필요함. 아래 세 출력 경로는 기존 디렉터리와 겹치지 않는 새 경로를 사용함.

```bash
python3 -m data_pipeline.reproduce_travel_retrieval \
  --output /tmp/ai-memory-travel-source
python3 -m evaluation.reproduce_travel_retrieval \
  --source /tmp/ai-memory-travel-source \
  --output /tmp/ai-memory-full-request-evaluation
python3 -m evaluation.reproduce_travel_candidate_search \
  --source /tmp/ai-memory-travel-source \
  --output /tmp/ai-memory-candidate-evaluation
```

입력·예측 압축본·원본 멤버 해시와 후보·최종 순위를 검사하고 결과를 새 출력 폴더에 저장함. [전체 질문 검색](../evaluation/results/travel-full-request-retrieval-20261109-v1/README.md)과 [후보 검색 학습](../evaluation/results/travel-candidate-search-20261109-v1/README.md)의 저장 결과 검산이며, 새 모델 추론은 수행하지 않음. 후보 검색의 float 지표는 Python 버전 차이 10⁻¹² 이내만 허용하며 개수·선택은 유지해야 함. 실제 가중치 감사에는 각 안내의 upload.zip/results.zip과 CPU Torch 환경이 추가로 필요함.

여행 기억의 실제 득실 파일럿은 다음 두 명령으로 과거 출력·5정책·실제 결정·조건별 성공과 비용을 재계산한다. 추가 패키지가 필요 없다. 4B 과거 132회와 두 작업자의 현재 230회씩, 총 새 생성 592회를 구분하며 9B에 저장된 과거 행은 재사용이다.

```bash
python3 -m evaluation.export_travel_worker verify evaluation/results/travel-worker-utility-4b-20261109-v1
python3 -m evaluation.export_travel_worker verify evaluation/results/travel-worker-utility-9b-20261109-v1
```

[공유 파일 안내](../evaluation/results/travel-worker-utility-9b-20261109-v1/README.md)의 `compare_travel_workers`로 두 작업자의 과거 기록 바이트·230개 현재 입력·선택 정책 동일성을 검산한다. `audit_travel_worker_constraints`는 같은 주석을 SQLite/Decimal로 다시 평가한다. CPU tokenizer 검사에는 별도 `tokenizers` 패키지와 원본 results ZIP이 필요하다. 이 단계들을 실제 가중치의 독립 순전파와 구분한다. [결과 보고](reports/2026-11-09-travel-worker-utility.md)는 현재 정보에 따른 득실과 명시적 참조 과제의 한계를 함께 정리한다.

MemoryArena 여행 판독은 `python3 -m evaluation.travel_reference_exports verify evaluation/results/memoryarena-travel-references-20261109-v1`로 6모델의 저장 예측 229,716개를 검산한다. [공유 파일 안내](../evaluation/results/memoryarena-travel-references-20261109-v1/README.md)에 범위와 실제 가중치 감사의 차이를, [데이터 안내](../datasets/memoryarena_travel/20261109-reference-v1/README.md)에 고정 공개 원본을 내려받아 학습 자료를 재구성하는 명령을 정리했다. 자체 분할의 관계 선택이며 공식 MemoryArena 전체 성적은 아니다.

실제 개발 이슈의 새 자료는 `python3 -m data_pipeline.audit_development_memory_leads datasets/development_memory_leads/20261108-v1`, 저장 예측은 `python3 -m evaluation.verify_development_leads evaluation/results/development-memory-leads-20261108-v1`로 검산한다. [보고서](reports/2026-11-08-development-memory-leads.md)의 새 학습·CPU 가중치 감사와 구분한다.

초기화 정보의 새 파일럿은 `python3 -B -m evaluation.verify_memory_units`로 검산한다. 44개 생성과 판정의 산술·입력 연결 검사이며 새 Docker 실행이나 모델 추론은 아니다. [범위와 실행 재현](../datasets/memory_units/20261108-django-startup-pilot-v1/README.md)을 따른다.

```bash
python3 -m data_pipeline.relations check
python3 -m evaluation.reproduce_relations
python3 -m unittest discover -s evaluation/tests -v
```

첫 명령은 배포한 학습 파일 5개의 해시, 74개 입력·정답 ID, 6개 개발 fold와 26개 조건 대조 그룹을 확인한다. 둘째는 Harrier·Qwen의 세 입력 조건에서 저장한 444개 예측을 원래 전체 지표와 비교한다. 원시 `runs/`, 모델 다운로드, Torch, API 키, Docker, GPU가 필요 없다.

이 단계는 저장 예측의 산술·연결 검증이다. 모델 순전파를 새로 실행하거나 현재 관계 라벨을 실제 메모리 효용으로 검증하는 것은 아니다. [작은 예측 묶음](../evaluation/results/natural-relations-20261107/README.md)은 원래 결과·설정·환경과 파일별 출처·해시를 포함한다.

11월 8일 범위 진단도 추가 패키지 없이 검사할 수 있다.

```bash
python3 -m data_pipeline.audit_scope_expansion datasets/scope_relations/20261108-expansion-v1
python3 -m data_pipeline.audit_relation_coverage
python3 -m evaluation.verify_scope_predictions evaluation/results/scope-external-20261108-best
python3 -m evaluation.verify_scope_predictions evaluation/results/scope-external-20261108-ridge
```

첫 감사는 입력·정답·분할·연결한 원본 근거를 검사하며 과제 ID와 실제 실행 기록을 구분한다. 두 외부 평가의 예측 재계산은 모델을 실행하지 않는다. 마지막 checkpoint의 자료·구조·짧은 반복 결과까지 확인하는 다섯 명령은 [평가 안내](../evaluation/README.md)에 있다. 수집 때 사용한 거대한 공개 shard나 로컬 실행 전체가 없어도 공유된 근거와 해시를 확인할 수 있으나, 상위 원본 전체를 다시 검토하는 범위는 별도다.

길이 통제 자료의 tokenizer를 실제 다시 실행하는 추가 감사는 `tokenizers` 패키지와 첫 확대 실행의 tokenizer 파일이 필요하다. [자료의 검산 안내](../datasets/scope_relations/20261108-length-challenge-v1/README.md)를 따른다. 위의 공유 예측 검사는 이 파일 없이도 해당 자료의 해시·라벨·지표를 확인한다.

같은 날 추가한 API 판독과 실제 작업자 효용도 표준 라이브러리만으로 검산할 수 있다.

```bash
python3 -m data_pipeline.audit_api_transfer datasets/api_transfer/20261108-v1
python3 -m data_pipeline.audit_api_version_challenge datasets/api_transfer/20261108-pandas-version-v1
python3 -m evaluation.verify_api_predictions evaluation/results/api-transfer-neural-20261108
python3 -m evaluation.verify_api_predictions evaluation/results/api-transfer-cpu-20261108
python3 -m data_pipeline.verify_api_worker_utility datasets/api_worker_utility/20261108-v3
python3 -m evaluation.verify_api_utility evaluation/results/api-worker-utility-20261108
python3 -m evaluation.verify_api_worker_transfer datasets/api_worker_utility/20261108-9b-transfer-v1
```

앞의 API 예측 6,656행은 과거 절차를 그대로 재사용하는 관계 판독이다. 뒤의 4B·9B 묶음은 실제로 생성한 코드·관측된 반환값/원본 변경·선택 정책의 연결을 확인한다. 기록된 판정 산술을 재계산하며, 모델이나 생성 코드를 새로 실행하는 검사는 아니다. 새 코드 실행에는 고정 API runtime 패키지들이 필요하고 실제 head 재검산에는 원본 checkpoint·encoder 캐시와 CPU Torch가 필요하다. [낮 종합 보고](reports/2026-11-08-memory-selector-daytime.md)가 결과와 검산 수준을 구분한다.

## 2. 코드와 데이터 준비 확인

```bash
python3 -m unittest discover -s training/memory_selector/tests -v
python3 -m data_pipeline.relations prepare --output training/memory_selector/data/relations-share-v1
```

내보내기는 원본 154개 고정 파일을 확인하고 새로운 출력 폴더에 학습 파일과 manifest를 만든다. 기존 경로가 있으면 중단하므로 다음 준비에는 새 이름을 사용한다. 이전 수치·범위 실험의 생성 자료가 없으면 해당 조건부 검사는 skip될 수 있다. 이는 새 관계 예측 444개를 확인하는 위 재현 검사와 구분한다.

Colab 실패·회수·종료 검사는 외부 실행을 모의하고 임시 입력 파일을 사용하므로 원본 학습 자료 없이 실행된다. 관계 분할·대조 검사는 저장소에 포함된 고정 자료를 직접 사용한다.

SWE 실행 코드의 로컬 검사는 프로젝트 환경을 사용한다.

```bash
uv sync --frozen
uv run python experiments/swe_memory/scripts/run_pilot.py --config experiments/swe_memory/configs/smoke-luna.toml --validate-only
uv run python -m unittest discover -s experiments/swe_memory/tests -v
```

`--validate-only`는 설정 구조만 확인한다. 실제 실행에는 Docker 이미지·API 키가 추가로 필요하다.

## 3. 새 모델 학습·판독 실행

먼저 Colab CLI와 본인 계정의 인증을 준비하고 2단계의 학습 자료를 만든다. 아래 명령은 **유료 Colab L4를 할당**해 Harrier 판독층 학습과 9B 직접 판독을 새로 실행한다.

```bash
python3 -u training/memory_selector/run_colab.py \
  --profile relations --gpu L4 \
  --data training/memory_selector/data/relations-share-v1 \
  --session-state "$HOME/.config/colab-cli/ai-memory/sessions.json" \
  --max-runtime-seconds 1200
```

`--session-state`에는 본인의 CLI 상태 파일을 지정한다. 기본값도 현재 사용자의 홈 아래다. 계정 설정은 저장소나 업로드 묶음에 포함하지 않는다. 실행기는 자기 세션에 허용된 파일을 올리고 결과를 회수한 뒤 종료하며, 실패 시에도 종료를 시도한다. `run-status.json`의 완료·회수 상태와 `stopped`를 확인한다.

시간 상한은 할당 시작부터 연결 확인·업로드·학습까지 적용된다. 예산을 소진하면 다음 단계를 시작하지 않고 회수·종료로 넘어간다. 회수·노트북 저장·종료에는 별도 제한 시간이 있어 제어기의 총 경과 시간은 이 상한보다 길 수 있다. 회수 중 중단·불완전 다운로드·잘못된 결과 JSON도 종료 시도와 상태 저장을 거친다. 이 예외 경로는 외부 명령을 모의한 테스트로 확인했으며, 공유 준비 중 새 GPU 실행은 하지 않았다.

실행별 원본은 `training/memory_selector/outputs/<UTC시각>/`에 생긴다. 학습 의존성은 profile별 requirements와 실행 당시 환경 기록을 사용한다. 수치·범위 실험은 [학습 안내](../training/memory_selector/README.md)의 별도 입력·config를 따른다.

큰 checkpoint 묶음은 작은 파일로 나누어 올리고, 원격에서 조각별·전체 SHA가 맞아야 추론을 시작한다. 서버 5xx에 대한 제한된 업로드 재시도도 전체 runtime 상한에 포함된다. 초기값·데이터 선택이나 학습 횟수를 바꾸는 재실행과 전송 재시도를 구분해 기록한다.

## 4. 전체 가중치·원본 근거 검산

이 단계에는 Git 밖의 자료가 필요하다. 원래 상대 경로를 유지해 회수한다.

과거 보고서의 **별도 원본** 링크는 이 안내로 연결된다. 함께 적힌 `runs/...` 또는 `training/memory_selector/outputs/...` 경로는 원본의 위치를 보존한 것이며, 해당 파일·폴더가 Git에 포함됐다는 뜻은 아니다.

| 검산 | 추가 자료 | 진입점 |
| --- | --- | --- |
| 관계 사례의 전체 출처 | 참조된 기존 `runs/`와 보존 자료 | 버전 폴더의 `verify_frozen.py`, `audit_preservation.py --check-only` |
| 첫 학습 | `outputs/20261107T074128Z/`의 실제 코드·데이터와 가중치·임베딩 | [독립 리뷰](../training/memory_selector/reviews/2026-11-07-independent/README.md) |
| 범위 판독 학습 | `outputs/20261107T104953Z/`의 전체 실행 묶음 | `probes/audit_scope_pilot.py` |
| 자연 관계 비교 | `outputs/20261107T112302Z/`의 전체 실행 묶음 | `probes/audit_relations_benchmark.py` |
| 선택지 순서 진단 | `outputs/20261107T113403Z/`의 전체 실행 묶음 | `probes/audit_reference_probe.py` |
| 에이전트 효과·비용 | 해당 `runs/`, 환경 이미지·배정·채점 기록 | 각 [측정 보고서](reports/README.md)의 검산 절차 |

Colab 묶음은 **`upload.zip`과 `results.zip`을 함께** 전달하고, `upload-manifest.json`, `run-status.json`, 파일 해시와 검산 코드를 포함한 실행 폴더를 보존한다. `results.zip`만으로 당시 실제 코드와 입력을 모두 확인할 수는 없다. 기반 모델 가중치는 포함하지 않으므로 config에 고정한 revision을 별도로 내려받아야 한다.

일부 검산 스크립트는 감사 결과·해시 목록을 출력 폴더에 쓴다. 공유받은 실행 묶음의 작업용 복사본에서 실행하고, 이미 고정한 원본은 보존한다. 첫 관계 예측 재계산은 표준 출력만 사용하지만, 자료 재구성과 전체 질문·후보 검색 재계산은 지정한 새 디렉터리에 파일을 저장함.

전체 원시 실행·이미지·가중치를 자동으로 내려받는 공개 배포 경로는 아직 없다. 따라서 첫 공유에서 바로 재현 가능한 범위는 1단계와 포함된 코드·데이터 검사이며, 전체 실행 재현에는 위 원본 묶음을 추가로 전달해야 한다.

## 11월 11일: 직접 성공 학습과 상태별 수집 파일럿

```bash
python -m evaluation.export_memory_direct_ranking check \
  --path evaluation/results/memory-direct-ranking-20261110-v1
python -m evaluation.export_memory_state_screen check \
  --path evaluation/results/memory-state-screen-20261111-v2
```

Python 3.11 이상, `numpy==2.4.6`, `tokenizers==0.23.3`, `Jinja2==3.1.6`에서 확인함. [학습 자료와 공유 범위](../evaluation/results/memory-direct-ranking-20261110-v1/README.md), [실제 작업자 입력·출력](../evaluation/results/memory-state-screen-20261111-v2/README.md)을 따름. 두 명령은 공유 파일만 사용하며 GPU·모델 호출을 하지 않음. 전자는 저장 특징과 head의 재계산이며, 실제 LoRA와 optimizer 전체는 로컬 원본 `runs/memory-direct-ranking-20261110-v1/training`에 있음. 후자는 고정 9B 작업자의 120본 호출·2반복을 다시 채점함.

이 회차 평가 테스트 161개와 학습 테스트 194개를 통과함. 학습 테스트는 Torch가 있는 별도 학습 환경에서 수행함. 기존 74행 검사·444저장 예측 검산도 통과했음. 재현 명령 통과를 새 출처 일반화나 실제 가중치의 재학습과 구분함.

## 11월 11일: 상태별 필요 기억과 실제 작업 효과

```bash
python3 -m evaluation.export_memory_need check --path evaluation/results/memory-need-20261111-v1
python3 -m evaluation.memory_need_data_quality --source datasets/memory_need_sources/20261111-v1 --data datasets/memory_need/20261111-v1 --output /tmp/memory-need-quality.json
```

[공유 범위·초기 모델 복구·학습](../evaluation/results/memory-need-20261111-v1/README.md)을 따름. 위 명령은 GPU 없이 저장 점수·확인 특징·실제 작업자 입력과 출력을 재계산함. 로컬 전체 특징 25,056개 검산과 공유 확인 특징 6,552개 검산을 구분함. 실제 base/adapter 순전파 4,368예측의 검산은 이미 완료했으며 optimizer 전체는 로컬 원본에 있음. 대화 중단의 부분 시도와 토큰 목록 재구성도 보존함.

## 11월 11일: 동일 입력의 선택기 회귀

```bash
python3 -m evaluation.export_memory_selector_regression check --path evaluation/results/memory-selector-regression-20261111-v1
python3 -m unittest evaluation.tests.test_memory_selector_regression -v
```

[공유 범위](../evaluation/results/memory-selector-regression-20261111-v1/README.md)를 따름. 모델 추론·학습 없이 전체 저장 점수와 작업 결과를 재계산함. 기존/새 모델의 같은 입력 비교이며, 표현 변형을 독립 출처로 세지 않음.
