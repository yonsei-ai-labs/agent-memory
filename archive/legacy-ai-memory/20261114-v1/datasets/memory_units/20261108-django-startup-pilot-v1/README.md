# Django 과정 정보의 제공 효과 진단 자료

한 과거 경험의 초기화 관측과 기존 수정 절차를 두 실제 Django 버전의 네 프로세스 상태에 각각 제공한다. **8상태·1연결 가족이며 모두 diagnostic**이다. source와 target 버전은 이미 설계에 사용됐다. 자연 SWE 과제의 독립 평가나 selector 학습용 분할이 아니다.

- `source-units.json`: 과거 agent 명령과 실제 응답, 기존 procedure 본문 및 SHA. source의 최종 미해결 상태 유지.
- `states.jsonl`: 고정 Docker image ID, 연구자가 만든 prelude, 현재 작업자에게 보인 정보.
- `design.json`: 생성 전 비교 조건·지표·한계·학습 확대 조건.
- `artifact-index.json`: 위 세 파일의 고정 hash.
- `worker-input/`: GPU에 보낸 결과 없는 44개 prompt와 별도 manifest. 40개 기본 비교와 4개 재생성 검사.

주 대비는 절차+초기화 정보와 절차만의 차이다. 무기억, 초기화 정보만, 절차+일반 주의 안내를 함께 비교한다. 초기화 정보는 실행 관측과 코드 예시를 포함하므로 순수한 서술/지시 표현 비교가 아니다. 현재 prompt에도 모듈명·경로가 있어 정보의 최초 획득과 실행 예시의 역할이 함께 고려돼야 한다.

공유 자료만으로 prompt를 다시 준비할 수 있다. 새 출력 폴더를 사용한다.

```sh
python3 -B training/memory_selector/prepare_memory_unit_worker.py \
  --source datasets/memory_units/20261108-django-startup-pilot-v1 \
  --output /tmp/memory-unit-worker-prepared
python3 -B -m evaluation.verify_memory_units
```

`data_pipeline.memory_unit_pilot`의 최초 source 추출에는 기존 로컬 source 실행 로그와 두 복원 Docker image가 필요하다. 이미 공유된 이 동결 자료를 사용하는 데 해당 로그는 필요하지 않다. `states.jsonl`의 image ID가 로컬에 있어야 아래 실제 실행을 재현할 수 있다. image는 Docker Hub에서 임의로 현재 tag를 다시 받으면 같은 상태라고 보장되지 않는다.

```sh
python3 -B -m data_pipeline.evaluate_memory_unit_pilot \
  --data datasets/memory_units/20261108-django-startup-pilot-v1 \
  --bundle datasets/memory_units/20261108-django-startup-pilot-v1/worker-input \
  --predictions evaluation/results/memory-unit-pilot-20261108/model-outputs.jsonl \
  --output /tmp/memory-unit-docker-replay
```

새 모델 생성은 Colab L4를 할당해 사용량이 발생한다. 기존 결과 검산에는 필요하지 않다.

```sh
python3 -B training/memory_selector/run_colab.py \
  --profile memory-unit-worker \
  --data datasets/memory_units/20261108-django-startup-pilot-v1/worker-input \
  --max-runtime-seconds 1800 \
  --output training/memory_selector/outputs/my-new-memory-unit-run
```

[결과와 해석](../../../docs/reports/2026-11-08-memory-unit-pilot.md), [생성·실행 기록](../../../evaluation/results/memory-unit-pilot-20261108/README.md).
