# 고정 판독기와 후보 검색용 LoRA 비교

새 독립 데이터를 추가한 자료가 아니다. [전체 질문 자료](../../travel_retrieval/20261109-full-request-v1/README.md)의 813/189/219질문과 같은 30/7/8계획 분할을 재사용한다. 후보 폭 32/64와 검색용 임베딩 학습을 비교하기 위한 입력·모델 계약이다. 실제 득실 라벨은 없다.

2026-11-09 학습·평가 완료: [결과 보고서](../../../docs/reports/2026-11-09-candidate-search-learning.md), [원본 저장 예측과 GPU 없는 재현](../../../evaluation/results/travel-candidate-search-20261109-v1/README.md). 같은 고정 판독기에서 기존 검색 32/64와 학습 검색 32의 Recall@10은 68.57/86.87/92.00%였다. 새 독립 출처나 실제 작업 효용 검증은 아니다.

- `protocol.json`: 주 비교 세 가지, 보조 임베딩 비교, 기존 판독기 해시, dev 시간 측정 질문 21개.
- `manifest.json`: 실제 Colab에 보낸 데이터·고정 판독기·시간 측정용 캐시의 SHA256.
- `source-contract.json`: 원본 자료 버전, 준비 코드·설정 해시, 독립 출처 수.

기존 실행 `training/memory_selector/outputs/travel-full-request-retrieval-20261109-a100`의 실제 `upload.zip`, `results.zip`, 추출한 `artifacts/`, 완료·종료 상태와 검산 파일이 필요하다. 공개 예측 묶음만으로 새 GPU 학습을 실행할 수는 없다. 원래 고정 판독기의 adapter·head와 임베딩 캐시는 Git에 복사하지 않는다.

NumPy가 있는 환경에서 입력 준비:

```bash
python training/memory_selector/prepare_travel_candidate_search.py \
  --source training/memory_selector/data/travel-retrieval-v1 \
  --prior-run training/memory_selector/outputs/travel-full-request-retrieval-20261109-a100 \
  --config training/memory_selector/configs/travel-candidate-search-v1.json \
  --output training/memory_selector/data/travel-candidate-search-new
```

`--source`는 앞선 전체 질문 자료의 학습 내보내기다. test 라벨과 정답 제거 변형은 들어 있지 않다. 준비 코드는 고정 판독기와 기존 캐시가 실제 원본 results.zip에 일치하는지 검사한다. 실행 경로 메타데이터와 NumPy 버전에 따라 새 준비 파일의 바이트 해시가 달라질 수 있으므로 원래 실행을 검산할 때는 실제 upload.zip을 사용한다.

다음은 새 Colab A100을 할당해 학습한다. 완료·실패 모두 자기 세션을 종료한다.

```bash
python3 training/memory_selector/run_colab.py --profile travel-candidate-search \
  --data training/memory_selector/data/travel-candidate-search-new \
  --gpu A100 --transport console --max-runtime-seconds 3900 \
  --output training/memory_selector/outputs/travel-candidate-search-new
```

[학습 전 계획](../../../docs/ai-memory-candidate-search-training-2026-11-09.md)에 손실·선택 기준·후보 및 시간 측정 범위를 고정했다. 전체 질문 자료의 출처·CC BY 4.0 안내와 인용을 따른다.
