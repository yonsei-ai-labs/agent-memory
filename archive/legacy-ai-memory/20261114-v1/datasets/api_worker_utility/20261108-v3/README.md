# 고정 작업자에서 측정한 기억 제공의 득실

Qwen3.5-4B의 고정 revision/BF16/greedy와 v3 출력 계약으로 96상태×10행동을 실행했다. 추가 4개는 동일 prompt 재현 검사다. 작업자는 기억의 절차를 바꿔 사용할 수 있다. 학습 50·개발 18·확인 28상태이며, 확인의 실행 판정은 효용 head와 관계 정책을 고정한 뒤 했다.

`jobs.jsonl`은 실제 업로드한 정답 없는 prompt, `model-outputs.jsonl`은 생성한 코드와 토큰 수·지연, `judgments.jsonl`은 두 fixture와 가능한 현재 구현에서 얻은 실행 판정이다. 생성 코드의 임의 Python 실행 대신 공개된 제한 AST 실행기를 사용했다. 미지원 코드는 별도로 표시하며 엄격한 성공에는 포함하지 않는다.

```bash
python3 -m data_pipeline.verify_api_worker_utility datasets/api_worker_utility/20261108-v3
```

추가 패키지 없이 prompt/출처 해시, 코드 연결, 기록된 반환값·원본 변화·통과 판정을 검산한다. 이는 모델을 다시 생성하거나 코드를 새로 실행하는 검사가 아니다. 새 실행에는 원래 API 자료의 pinned runtime 환경이 필요하다. 모델 가중치·저장 hidden state·원래 token ID 배열은 Git에 포함하지 않았으며 전체 감사를 위해 원본 upload.zip/results.zip이 필요하다.

하나의 API 구현에서 파생된 상태·네 후보·제공 방식·두 fixture를 독립 경험으로 세지 않는다. 이 자료는 작은 저작 API 작업의 실제 worker 득실이며 자연 SWE 해결률을 대신하지 않는다. 세 번의 학습 pilot 중 형식 계약이 고정된 v3 전체 실행만 배포 데이터로 사용했다.
