# AI Memory Lab

[데이터 보강의 원인 분해](docs/reports/2026-11-12-augmentation-ablation.md): 같은 형식·고유 입력 수·정답 분포에서 부분 관측 관계를 함께 학습했을 때 필요한 기억 보존과 후속 복원이 개선됨. 형식 조건 65·63/96→관계 조건 88·87/96이며 불필요 제공·기존 자료 회귀도 남음. 기존 12출처에서의 원인 진단으로, [실제 가중치·CPU 재현](evaluation/results/memory-augmentation-ablation-20261112-v1/README.md)을 제공함.

[현재 연구 성과 요약](docs/reports/2026-11-12-memory-core-findings.md): **상황별 기억의 득실, 관계 대조 데이터의 학습 효과, 실제 효과를 분리해 측정하는 방법**에 집중함. 원 출력 재검산·핵심 비교표·대표 사례 5개와 [CPU 재현 자료](docs/reports/data/memory-core-findings-20261112-v1/README.md)를 정리함. 새 모델·벤치 확장은 보류하며, 아래 개별 기록의 긍정적 결과와 한계를 함께 보존함.

[STATE-Bench 첫 실제 업무 파일럿](docs/reports/2026-11-12-statebench-experience-pilot.md): 근거를 검토한 경험 20개로 개발 8과제×3정책을 실행함. 무기억 / 고정 Harrier 요청 / 요청+관측의 성공은 5 / 6 / 5임. 개선 1건과 관측 조건의 추가 악화 1건을 도구·DB 재실행으로 확인했으며, 새 학습이나 일반화 성과는 아님. [공유 결과·CPU 재현](evaluation/results/statebench-pilot-20261112-v1/README.md)을 제공함.

[STATE-Bench 적용 준비](docs/reports/2026-11-12-statebench-preparation.md): 과거 경험을 현재 업무에 적용하는 평가를 위해 공개 train 기록 300개와 관측 접두부 860개를 확보함. 출처 분할·입력 경계·전달 예산과 공식 런타임 연결을 검증했으며, 실제 득실 측정이나 새 학습 결과는 아님. [자료·재현 안내](datasets/statebench_preparation/20261112-v1/README.md)를 제공함.

[ConvoMem·AMA 각 100출처 외부 평가](docs/reports/2026-11-12-external-memory-benchmarks.md): 추가 학습 없이 고정 선택기를 비교함. ConvoMem은 이전 49·47→보완 50·51/100으로 비슷한 관측 정답 수에서 기억 토큰이 54–71% 줄었지만 기본 임베딩 83보다 낮음. AMA는 9·9→4·5/100임. 자동 채점의 표본 불일치와 손해 판정 오류를 함께 보존하며 범용 우위를 주장하지 않음. [공유본 CPU 검산](evaluation/results/memory-external-test-20261112-v1/README.md)을 제공함.

[11월 12일 복습 교체·예산별 효과](docs/reports/2026-11-12-balanced-replay-and-budget.md): 4회 추가 학습 후 BEAM 128토큰의 근거 있는 응답이 이전 33·31→38·39/52로 늘었음. 기본 임베딩 검색은 44/52이고 새 출처·기존 다른 자료의 회귀가 남아 기본 모델은 유지함. [공유 결과](evaluation/results/memory-balanced-replay-20261112-v1/README.md)에 관계 판독과 실제 개선·악화·비용을 분리해 보존함.

[동일 입력 회귀 검증](docs/reports/2026-11-11-memory-selector-regression.md): 기존 검색 순위는 대체로 유지됐으나 제공·보류와 표현 변화에 약점이 남음. 추가 학습 없이 같은 입력·후보에서 비교하고 실제 작업 결과까지 확인함.

[11월 11일 상태별 기억 선택](docs/reports/2026-11-11-memory-need-learning.md): 새 확인 6가족·36상태에서 고정 9B 작업자의 성공은 목표 학습 24→상태 학습 36으로 두 seed에서 같았음. 전달 기억 토큰은 2,802→1,904. 공개 상태 규칙도 36이므로 신경망 우위의 증거는 아님. [공유 검산·adapter](evaluation/results/memory-need-20261111-v1/README.md)에 데이터 처리와 실제 득실의 연결을 보존함.

에이전트가 **같은 과거 경험에서 무엇을 보존하고, 현재 조건에서 언제 사용해야 이득을 얻는지** 연구한다. 코드 작업에서 시작해 여행 계획의 관계 판독으로 범위를 넓혔다. 기억의 내용·사용 방법이 성공과 비용에 미치는 영향을 측정하고, 현재 상태와 과거 경험의 관계를 판단하는 작은 모델을 학습한다.

현재는 동료 검토를 위한 탐색 연구다. 관계 판독과 실제 작업자 효용을 분리해 측정한다. 작성한 API 과제의 일부 선호 학습은 무기억보다 좋아졌지만, 사전 주 모델과 개발에서 선택한 모델은 개선하지 못했으며 고정 기억을 넘어서는 안정적인 이득도 아직 확인하지 못했다.

11월 9일 전체 질문 검색에서는 고정 판독기에 전달할 후보를 학습해 Recall@10 68.6%→92.0%를 관측함. 같은 8개 평가 계획·단일 seed의 검색 결과이며, 실제 작업 성공·기억 득실의 개선과는 별도임.

**11월 11일 완료:** [제공 후 성공·순위 학습](docs/reports/2026-11-10-memory-direct-ranking.md)은 이진 50–51/96·순위 추가 46–51/96으로 긴 묶음 52·참조 규칙 55에 못 미침. [상태별 수집 파일럿](docs/reports/2026-11-11-memory-state-screen.md)은 대조 24상태에서 무기억 0·목표 규칙 12·필수 근거 24 성공, 이미 알고 있는 12상태에서 불필요한 기억으로 성공 4건의 회귀를 확인함. 기존 출처의 통제 진단이며 신경망 선택기의 우위는 아직 미확인임. 최신 판단은 [연구 상태](docs/current-status.md)에 모음.

## 먼저 보기

- [11월 10일 순위 학습·선별 수집 피드백](docs/reports/2026-11-10-ranking-and-collection-feedback.md): 무작위·길이·참조 기준선, 점수 변경, 선별에서 놓칠 수 있는 이득·손해를 검산함. 대량 수집 전에 제한된 학습 진단과 상태별 대조를 연결함.
- [11월 10일 작업자 기능 점검](docs/reports/2026-11-10-worker-contract-check.md): 144개 본 실행·2반복. 확인 자료의 정보 부족 정답은 0→6/24, 풀 수 있는 항목은 24→20/24로 채택 기준 미달. 새 선택기 학습·새 출처 일반화 결과는 아님.
- [11월 10일 학습 데이터 리뷰](docs/reports/2026-11-10-memory-training-data-review.md): 실제 단위 12가족·24요청·96상태, 기억을 읽지 않는 기준선 65.4%와 악화 라벨의 한계. 현재 자료의 추가 학습보다 선택의 개선 여지와 출처 다양성 확인이 우선임.
- [11월 9일 후보 검색용 LoRA](docs/reports/2026-11-09-candidate-search-learning.md): 같은 판독기에서 기존 검색 32/64후보와 학습 검색 32후보의 Recall@10은 68.6/86.9/92.0%. 전체 질문 219개·평가 계획 8개이며 확률 안정성·시간 측정의 제한을 함께 보고함.
- [11월 9일 전체 질문의 여러 기억 검색](docs/reports/2026-11-09-full-request-memory-retrieval.md): 고정 MLP 56.9%·LoRA 68.6%. 앞단 후보 누락을 확인한 이전 단계의 결과임.
- [11월 9일 여행 기억의 실제 득실](docs/reports/2026-11-09-travel-worker-utility.md): 구성한 식당 선택 과제. 9B 평가 16요청에서 과거 전용 정보는 무기억 1→학습 선택 10, 현재에도 보이면 11→9. 규칙과 학습 선택은 같은 결과이며 평가 계획은 8개뿐.
- [11월 9일 MemoryArena 여행 자료 수집·학습](docs/reports/2026-11-09-memoryarena-travel-references.md): 기본 계획 45개의 자체 분할. Test 참조 선택은 MLP 66.5%·LoRA 100%; 규칙도 100%. 실제 득실은 위 후속 파일럿에서 별도로 측정.
- [11월 8일 통합 리뷰: 학습·선택 기준·실제 효용·재현 점검](training/memory_selector/reviews/2026-11-08-integrated/README.md)
- [다음 단계: 데이터 수집·증강·분류의 통합 방향](docs/ai-memory-data-collection-and-taxonomy-2026-11-08.md) · [검토용 분류표](docs/design-data/memory-taxonomy-20261108-v1/README.md)
- [11월 8일 실제 개발 이슈의 기억 학습: 학습 적합과 새 이슈 선택 실패](docs/reports/2026-11-08-development-memory-leads.md)
- [11월 8일 초기화 정보 파일럿: 정보 복원 효과와 선택기의 남은 과제](docs/reports/2026-11-08-memory-unit-pilot.md)
- [11월 8일 낮 보고: 실제 기억 선택·비용·4B/9B 전이](docs/reports/2026-11-08-memory-selector-daytime.md)
- [11월 8일 아침 보고: 데이터 설계와 추가 학습](docs/reports/2026-11-08-memory-reader-overnight.md)
- [연구 방향](docs/ai-memory-direction-reassessment-2026-11-07.md) · [문서 읽는 순서](docs/README.md)
- [실험 결과와 한계](docs/reports/README.md) · [데이터 안내](datasets/README.md)
- [GPU 없는 결과 재현](docs/reproduction.md) · [공동연구 작업 안내](CONTRIBUTING.md)

## 현재 확인한 것

| 작업 | 관측과 범위 |
| --- | --- |
| 후보 검색 표현 학습 | 고정 판독기에서 기존 32/64후보와 학습 32후보의 R@10 68.6/86.9/92.0%. 같은 8계획·한 seed; 실제 작업자 효용과 별도 |
| 전체 질문의 복수 참조 판독 | 219평가 질문의 R@10 고정 MLP 56.9%·LoRA 68.6%. 관계 판독과 앞단 후보 누락을 분리 |
| 여행 기억의 실제 선택 효과 | 같은 과거 선택과 5정책을 4B·9B에 제공. 참조 정확성과 실제 득실이 다름을 관측; 단순 규칙 대비 학습 선택의 추가 이득은 미확인 |
| 여행의 명시적 참조 판독 | MemoryArena 기본 계획 30/7/8 분할, Test 1,153요청의 6후보 선택: MLP 66.5%·LoRA 100%. 깨끗한 참조 이력·한 seed; 실제 작업 성공률과 별도 |
| 기억 개입 측정 | 전달·표현·처방·과정 정보 복원의 소규모 실행. 과제·환경·검사 계약·제출 행동에 따른 한계를 함께 분석 |
| 관계 데이터 | 74행·10개 출처·6개 저장소의 발견 자료. 실제 기억의 유익·유해 정답은 없음 |
| 초기 통제 범위 학습 | 고정 MLP 36.1%, LoRA 66.7%, 대조 손실 추가 69.4%. 같은 개발 자료의 규칙 기준선은 100%; 개발 출처 2개 |
| 범위 추가 학습·외부 평가 | 현재·기억을 함께 읽은 LoRA 106/108·107/108, 별도 벡터 방식 68/108·82/108. 집합당 새 저장소 2개·구성한 36조건의 세 표현; 정확한 문자열 규칙은 100% |
| 실제 코드 관계 비교 | Harrier 고정 표현+학습 판독층 51.4%, Qwen3.5-9B 직접 판독 25.7%. Harrier 상태 단독도 51.4% |
| 작성한 API 과제의 실제 작업자 효용 | 확인 28상태: 4B 무기억 15·주 선택기 13·선호 17, 9B 무기억 18·주 선택기 16·선호 20·개발 고정 기억 21. 자연 SWE 일반화는 아님 |

각 수치의 분모·학습 방식·입력·제약은 [결과 색인](docs/reports/README.md)에서 확인한다. 서로 다른 과제의 점수를 직접 비교하거나 모델 크기의 일반적인 우열로 해석하지 않는다.

## 첫 실행: 저장 예측에서 결과표 재계산

저장소 루트, **Python 3.11 이상**에서 실행한다. 추가 패키지·모델 다운로드·API 키·Docker·GPU가 필요 없다.

```bash
python3 -m data_pipeline.relations check
python3 -m evaluation.reproduce_relations
```

74개 사례의 학습 파일·개발 분할을 확인하고, 두 모델의 세 입력 조건에서 저장한 **444개 예측**으로 원래 지표를 재계산한다. 이는 저장 예측의 검산이며 모델 순전파나 새 학습을 반복하는 것이 아니다. 코드 검사·새 학습·전체 근거 감사는 [재현 안내](docs/reproduction.md)를 따른다.

새 여행 판독 결과도 GPU 없이 검산할 수 있다:

```bash
python3 -m evaluation.travel_reference_exports verify \
  evaluation/results/memoryarena-travel-references-20261109-v1
python3 -m evaluation.export_travel_worker verify \
  evaluation/results/travel-worker-utility-4b-20261109-v1
python3 -m evaluation.export_travel_worker verify \
  evaluation/results/travel-worker-utility-9b-20261109-v1
```

전체 질문·후보 검색 실험은 공개 원문을 먼저 재구성해야 함. [재현 안내](docs/reproduction.md)의 두 검색 실험 명령과 [공유 결과](evaluation/results/travel-candidate-search-20261109-v1/README.md)를 따름. 원문 다운로드에는 네트워크가 필요하며 모델·GPU는 필요하지 않음.

## 작업 영역

```text
ai-memory-lab/
├── experiments/     # 에이전트 실행·기억 개입·채점·비용 측정
├── data_pipeline/   # 수집·생성 안내와 데이터 확인·학습 내보내기
├── datasets/        # 고정된 데이터·입력·라벨·출처·분할
├── training/        # 모델·판독층·LoRA·Colab 실행
├── evaluation/      # 공유 예측과 결과 재계산
├── docs/            # 연구 노트·결과·근거 자료의 색인
├── runs/            # 로컬 원시 실행; Git 제외
├── CONTRIBUTING.md
├── pyproject.toml
└── uv.lock
```

[측정](experiments/README.md), [데이터 파이프라인](data_pipeline/README.md), [학습](training/README.md), [평가](evaluation/README.md)의 안내에서 실제 코드로 들어간다. 기존 실험의 경로·해시·업로드 사본은 보존한다. 측정용 Python 환경과 Colab 학습 환경은 각각 고정한 의존성을 사용한다.

원시 `runs/`, 학습 `outputs/`, 생성된 학습 자료와 계정 설정은 Git 밖에 있다. 전체 가중치·원본 검산에 필요한 추가 묶음과 현재 배포 범위는 [재현 방법](docs/reproduction.md)에 명시했다.
