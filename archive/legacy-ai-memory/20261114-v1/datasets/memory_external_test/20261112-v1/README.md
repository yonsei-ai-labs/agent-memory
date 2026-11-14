# ConvoMem과 AMA 외부 평가 자료

추가 학습 없이 기존 기억 선택기의 외부 전이를 확인하는 고정 200문항임. ConvoMem 합성 프로필 100개와 AMA 실행 이력 100개에서 각각 1문항을 선택함. 공식 전체 벤치 점수가 아닌, 동일한 후보와 전달 예산을 사용하는 부분 평가임.

- `cases.jsonl`: 원래 질문, 현재 관측 없음, 공통 후보 은행. 정답과 근거 표시는 포함하지 않음.
- `labels.jsonl`: 원래 정답/채점 기준과 ConvoMem의 제공 근거 위치. 선택기·작업자 입력과 분리함.
- `provenance.jsonl`: 원본 파일·질문·출처·해시와 대화 ID.
- `protocol.json`: 모델, seed, 예산, 후보 구성, 채점 및 실행 상한을 예측 전에 고정함.
- `intake-audit.json`: 실제 출처/질문 수, 범주, 원문·후보 길이, 근거 위치 대응 상태.
- `manifest.json`: 평가 입력과 checkpoint 무결성 검사용 해시.

ConvoMem은 원래 근거 대화 전체에 같은 프로필의 원래 배경 대화 2개를 섞음. 프로필별 지정 범주에서 가능한 가장 작은 근거 수의 원래 파일을 선택하고, 해시로 문항을 고정함. 원래 근거 대화 사이의 순서를 유지하며, 질문·답변은 다시 생성하지 않음. `legacy_benchmarks`의 LongMemEval/LoCoMo 자료는 사용하지 않음. 이 구성은 공식 pre-mixed context 길이별 평가와 다름.

AMA는 공식 test의 SOFTWARE 36개를 제외하고 EMBODIED_AI·Game·OPENWORLD_QA·TEXT2SQL·WEB에서 각 20개를 선택함. 회상·인과·상태 갱신·상태 추상화 질문은 각 25개임. 원래 행동/관측 이력 전체를 은행으로 사용함. 원래 과제의 성공 여부를 이번 기억 선택의 효과 라벨로 사용하지 않음.

원문은 작업자 토크나이저 기준 192토큰 창과 160토큰 간격으로 나눔. 질문만 사용하는 BM25 상위 64개가 모든 정책의 공통 후보이며 정답 근거를 강제로 넣지 않음. 이후 기억 전달 상한은 동일하게 1,024토큰·12조각임. 결과는 이 공통 후보 생성 단계에 조건부인 비교로 해석함.

ConvoMem 근거 주석은 원문에서 위치를 확인한 109/118구간임. 이 주석은 최소 충분 근거 집합이나 모든 비주석 기록의 불필요성을 보장하지 않음. AMA에는 공식 조각별 정답이 없어 관계 분류 정확도를 만들지 않음. 200출처에 고유 질문 문장은 195개이며, 200개의 독립 과제 유형이나 실제 사용자 자료를 의미하지 않음.

원본: [ConvoMem](https://huggingface.co/datasets/Salesforce/ConvoMem)과 [AMA-Bench](https://huggingface.co/datasets/AMA-bench/AMA-bench). ConvoMem 현재 데이터 카드의 라이선스는 CC-BY-NC-4.0이며, 오래된 `dataset_info.json`의 Apache 표기와 다르므로 현재 카드를 기준으로 함. AMA는 MIT임. 다운로드한 전체 원문은 `runs/memory-external-test-20261112-v1/raw`에 보존함.

로컬 검산:

```bash
.venv/bin/python -m data_pipeline.external_test_data audit
.venv/bin/python -m unittest tests.test_external_test -v
```

자료 구성에는 고정 Qwen3.5-4B 토크나이저, 모델 해시 확인에는 기존 두 seed의 partial-record augmented 및 balanced-replay checkpoint가 필요함. [공유 결과](../../../evaluation/results/memory-external-test-20261112-v1/README.md)의 압축에는 검산용 토크나이저·checkpoint·전체 선택 원문과 저장 특징을 포함하므로 기본 모델을 새로 내려받지 않고 CPU 검산할 수 있음. 원본 수집과 학습/예측은 분리되어 있으며 이 자료로 새 학습을 수행하지 않음.
