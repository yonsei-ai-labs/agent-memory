# 실제 이슈와 과거 경험의 진단 단서: 20261108-v1

현재 이슈의 설명을 읽고, 고정된 과거 경험 16개 중 구체적인 점검 단서를 주는 기억을 구별하는 **중간 학습 과제**다. 현재 코드에서 수정법이 그대로 적용되는지, 기억 제공이 작업 성공·비용을 개선하는지는 측정하지 않는다.

## 규모와 출처

[SWE-rebench OpenHands 공개 실행 기록](https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories/tree/35455389ab51bf5e2306bfd436ef72d0f98bf882)의 고정 revision `35455389ab51bf5e2306bfd436ef72d0f98bf882`을 사용한다. `trajectories.parquet`의 첫 row group에서 저장소·이슈 ID로 30개 저장소와 77개 실행을 선정했다. 성공 여부나 공식 patch/evaluation 열로 선별하지 않았다.

그중 과거 경험 16개와 다른 현재 이슈 40개를 사람이 읽을 수 있는 근거에 연결했다. **640쌍 = 현재 이슈 40개 × 기억 16개**다. 독립된 경험 640개가 아니다. 현재 이슈는 24개 저장소에 걸쳐 있고, 원본 행동을 새로 실행하지 않았다.

| 분할 | 현재 이슈 | 현재 저장소 | 쌍 | 관련 단서 | 다른 원리 | 연결 미확인 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 학습 | 27 | 16 | 432 | 18 | 391 | 23 |
| 개발 | 7 | 4 | 112 | 9 | 102 | 1 |
| 평가 | 6 | 4 | 96 | 4 | 85 | 7 |

현재 이슈의 저장소를 나누되 **기억 은행은 세 분할이 공유**한다. 평가 저장소에서 나온 다른 과거 이슈가 기억 은행에 포함되기도 한다. 따라서 새 기억이나 완전히 새로운 저장소의 일반화 결과로 부르지 않는다. 역사적 발생 순서는 검증하지 않았으며, 시간순 배포 시나리오를 재현한 자료가 아니다.

## 입력과 주석

- 현재 입력은 에이전트가 행동하기 전의 사용자 이슈 설명이다. 8개는 2,200자 한도에서 잘렸으며, 정확한 위치와 hash를 남겼다. 잘려서 확인할 수 없는 내용은 상상해 채우지 않는다.
- 기억은 실제 수정 명령과 그 명령의 응답에 연결한 관측 요약, 진단에 쓰는 방법, 적용 경계로 구성된다. 사용 원리와 경계는 작성자의 해석이며 로그 원문이나 검증된 일반 법칙이 아니다. 수정 명령이 실행됐다는 근거와 전체 이슈 해결은 별개다. 되돌려진 시도도 그 사실을 표시했다.
- `shared_diagnostic_mechanism`: 현재 설명에 특정 점검 원리가 연결된다는 주석이다. 예를 들어 표현 변환 중 필드 소실을 점검한다.
- `different_mechanism`: 현재 설명과 그 기억의 구체적인 원리가 다르다는 주석이다.
- `insufficient_connection`: 관련 가능성은 있지만 필요한 전제가 입력에 없다.

주석자는 **한 assistant**이며 독립 검토나 실제 실행 효과가 정답을 보증하지 않는다. 추상적인 유사성의 경계에 이견이 있을 수 있다. `supervision.jsonl`의 이유를 같이 검토해야 한다. 모든 `observed_utility`는 `null`이다.

초기 네 후보 안은 어휘 검색이 관련 단서를 누락해 학습 전에 폐기했다. `pretraining-amendment.json`에 변경을 기록하고 모든 이슈에 16개를 대조했다. 평가 주석 작성자가 모델 비교를 설계했고 약한 어휘 기준선도 사전에 보았으므로, 독립적으로 봉인된 최종 benchmark가 아니다. 다만 **평가 라벨은 Colab 학습 묶음에서 제외**했다.

## 알려진 빈틈

학습에서 긍정 사례가 있는 기억은 16개 중 10개다. 개발 긍정 9쌍 중 1쌍, 평가 긍정 4쌍 중 1쌍은 같은 기억의 학습 긍정 사례가 없다. 개발 이슈 7개는 모두 관련 단서가 있으므로, 적절한 보류의 범위를 충분히 검증하지 못한다. 0.5 제공 문턱은 미리 고정했으며 보정된 성공 확률이 아니다.

모델은 저장소 이름이나 주석 이유를 입력받지 않는다. 단, 이슈 본문과 기억 내용 자체에 프로젝트·함수 이름이 있을 수 있다. 이슈와 기억을 각각 단독으로 읽는 비교를 함께 둔다. 긍정 사례가 31쌍뿐인 불균형 자료라서 전체 정확도만 해석하면 안 된다.

원인에 관한 추상적 유사성과 실제로 추가되는 정보가 다른 [일곱 검토 사례](../../../docs/reports/data/development-memory-leads-20261108/annotation-review-followup.json)를 남겼다. 같은 assistant의 재검토이며 독립 주석이 아니다. 학습을 시작한 v1의 정답을 사후에 바꾸지 않았다.

## 확인과 재현

공유 파일만으로 해시, 입력의 출처 연결, 라벨·분할·후보 수를 확인한다.

```bash
python3 -m data_pipeline.audit_development_memory_leads datasets/development_memory_leads/20261108-v1
python3 training/memory_selector/prepare_development_leads.py \
  --source datasets/development_memory_leads/20261108-v1 \
  --output training/memory_selector/data/development-leads-reproduction
```

새 디렉터리에 원본 77개를 다시 받으려면 `pyarrow`, `fsspec[http]`가 필요하다. 수집기는 고정된 첫 row group과 네 열만 읽고 원본의 명령을 실행하지 않는다.

```bash
python3 -m data_pipeline.collect_development_memory_sources \
  --output training/memory_selector/outputs/development-leads-source-reproduction
python3 -m data_pipeline.audit_development_memory_leads \
  datasets/development_memory_leads/20261108-v1 \
  --intake training/memory_selector/outputs/development-leads-source-reproduction
```

`artifact-index.json`에 든 파일은 동결했다. 원래 파일을 덮어쓰지 않는다. 공개 원본의 수정·응답 26쌍과 현재 설명 40개를 원시 자료에 대조한 결과는 [자료 감사](../../../docs/reports/data/development-memory-leads-20261108/data-audit.json), 비교 조건은 [학습 전 계획](../../../docs/reports/data/development-memory-leads-20261108/pre-training-plan.json)에 있다.
