# 새 출처의 기억–현재 문제 사례 검토

2026-11-08. [다음 데이터 계약](../../ai-memory-next-data-contract-2026-11-08.md)에 따라 만든 첫 발견용 묶음이다. **과거 경험 12개의 기억 초안과 현재 문제 18개를 연결한 19개 사례를 만들었다. 아직 학습 데이터나 기억 효용 벤치는 아니다.**

바로 볼 자료는 [사례 표 CSV](case-table.csv), [관계 검토 원문](case-review.json), [근거가 연결된 JSONL](cases.jsonl)이다. 같은 현재 문제에 기억 두 개를 비교한 한 쌍이 있으므로 19행을 19개 독립 문제로 세지 않는다.

## 무엇이 달라졌나

이전처럼 같은 저장소라는 이유만으로 관련성을 붙이지 않고, **수정 전 코드에서 현재 조건과 과거 절차의 조건을 비교했다.** 같은 파일이나 함수 계열 안에서도 필요한 정보가 다르고, 과거 처방이 이미 반영된 경우도 확인했다.

| 사례 | 원문과 코드에서 확인한 차이 | 현재 해석 |
| --- | --- | --- |
| structlog 문맥 복사 → None 길이 제한 오류 | executor 전에 문맥을 복사하는 코드가 이미 있다. 현재 오류는 `None < 0` 비교 경로다. | 같은 저장소의 과거 처방을 다시 적용할 이유가 약함 |
| 같은 structlog 문제 ← WebOb의 None 이름 처리 | 저장소와 연산은 다르지만 None이 유효한 값이라는 조건을 보존해야 한다. | 원리를 옮겨볼 후보. 다만 현재 본문도 None 처리를 직접 요청하므로 추가 효용은 불명 |
| fsspec URL scheme 경험 → 콜론이 있는 로컬 파일 | 과거에는 실제 scheme이 인식되지 않았고, 현재는 로컬 파일명이 scheme으로 과잉 인식된다. | 같은 URL 처리라도 입력 계약이 반대. registry 이름 변경을 그대로 재사용하면 안 됨 |
| numpydoc 새 섹션 추가 → See Also 설명 소실 | 과거에는 인식·파싱·렌더러·템플릿을 함께 바꿨다. 현재에는 이미 있는 섹션의 여러 항목과 설명이 사라진다. | 단계별 보존 여부를 확인하는 진단으로 옮겨볼 후보 |
| tabulate 새 표 형식 요청 | 수정 전 코드에 요청과 비슷한 `fancy_outline`이 이미 있으나, 본문은 이름을 정하지 않았다. | 채점 계약부터 확인. 임의로 해결/미해결을 정하지 않음 |

위 판단은 **한 에이전트의 잠정 관계 검토**다. 직접 재사용하기 어렵다는 판단은 기억이 반드시 해롭다는 뜻이 아니고, 진단으로 옮길 만하다는 판단도 실제 도움이 된다는 뜻이 아니다.

## 수집·검토 단위

| 단위 | 개수 | 의미 |
| --- | ---: | --- |
| 새 저장소 묶음 | 12 | 아래에 명시한 기존 로컬 목록에서 겹치지 않는 저장소 |
| 과거 실행 후보 | 24 | 저장소별 앞선 문제 두 개. 모두를 상세 감사한 것은 아님 |
| 출처만 보고 고정한 기억 초안 | 12 | [절차 P와 조건·관측 E](source-cards.jsonl) |
| 연결을 검산한 과거 명령–응답 | 87 | 실제 명령과 연결된 도구 응답; 여기서 다시 실행하지 않음 |
| 처음 선정한 현재 문제 | 12 | 저장소별 더 늦은 문제 하나 |
| 추가 본문 후보 | 42 | 제목 42개를 선별 검토하고, 본문 7개·수정 전 코드 6개를 더 검토 |
| 최종 현재 문제 / 관계 사례 | 18 / 19 | 현재 문제 하나에 기억 두 개를 연결한 대조 포함 |
| 코드 파일 / 공유 코드 구간 | 23 / 42 | Git blob과 전체 내용 SHA를 확인한 수정 전 코드의 발췌 |
| 새 학습 행 / 실제 효용 라벨 / 복원 가능한 작업자 snapshot | 0 / 0 / 0 | GPU 할당·유료 모델 호출·추가 학습 없음 |

직접 재사용 검토는 **조건이 맞지 않음 10쌍, 관련 처리가 이미 다른 경로에 있음 4쌍, 수정해 활용할 후보 5쌍**이다. 이는 성능 측정이나 제공/보류 정답이 아니다. 모든 실제 효과는 `null`로 남겼다.

새 저장소 판정은 기준 커밋 `49e5ec0`의 `datasets`, `docs/design-data`, 학습 `reviews` 아래 JSON/JSONL/CSV와 기존 30개 저장소 intake를 대조한 결과다. 전 세계 데이터 오염이나 모든 과거 노출의 부재를 보증하는 검사는 아니다. 원래 데이터·분할·예측은 수정하지 않았다.

## 수집 순서와 한계

1. [첫 계획](intake-plan.json)을 작성하고 파일·출력·검사 도구 등 12개 Python 저장소를 정했다. 모집단 무작위 표본이 아니라 실행 가능성을 고려한 발견용 표본이다.
2. 공개 trajectory의 ID와 task metadata를 연결했다. 문제 생성 시각으로 앞선 두 문제를 출처 후보, 가장 늦은 문제를 대상으로 정했다. 이 순서는 실제 역사에서 해당 에이전트 경험이 이용 가능했다는 증거는 아니다.
3. 출처 12개에서 P/E를 쓰고, [출처 동결 기록](source-freeze.json)을 남긴 뒤 대상 문제 본문을 열었다. 동결 시점은 이번 검토의 작업 순서 기록이며 독립 관찰자의 검증이 아니다.
4. 처음 12쌍에 다른 원인의 문제가 많아 [발견 단계 변경 계획](target-expansion-plan.json)을 썼다. **기억 본문은 유지**하고, 나머지 문제 설명에서 같은 조건이나 가까운 경쟁 조건을 찾았다. [42개 후보와 검토 깊이](expanded-target-screening.json)를 모두 남겼다. 제목만 읽은 후보를 부정 라벨로 바꾸지 않았다.
5. 실제로 읽은 현재 정보는 문제 본문과 `base_commit` 코드다. 대상 trajectory·미래 명령·공식 패치·공식 검사 결과는 내보내거나 검토하지 않았다. 일부 문제 본문의 제안 diff는 처음부터 제공되는 정보이며, 추가 가치 해석에 별도로 표시했다.

입력 코드를 읽은 것은 **연구자의 정적 검토**다. 고정 작업자가 공통 초기 조사를 마쳤다는 기록이나, 그 시점에서 모든 행동을 독립 실행할 수 있는 환경 snapshot이 아니다. 현재 코드 전체의 해시와 공유 발췌는 검산했지만 새 문제를 실제로 실행하지는 않았다.

출처의 주장도 좁게 보존했다. PyFilesystem 재현 스크립트는 오류를 출력하면서 종료코드가 0인 경우가 있었고, responses는 callback 검사 통과 후 전체 파일 검사에서 실패가 남았다. jsonargparse에서는 추가한 local-class 검사가 실패한 뒤 제거돼 기존 검사만 통과했다. `test_receives_official.py`는 출처 에이전트가 만든 파일이었다. 이런 기록을 전체 해결이나 공식 검사 통과로 요약하지 않았다.

## 다음 실행에서 먼저 확인할 것

**이 19행을 바로 LoRA 학습에 넣지 않는다.** 이번 묶음으로 확인한 것은 출처 수 증가와 선택할 가치가 있는 상황 증가가 서로 다르다는 점이다. 같은 저장소의 과거 버그와 나중 버그를 연결하는 것만으로는 유익한 재사용 사례가 충분히 생기지 않는다.

실행 준비의 첫 후보는 다음 네 현재 문제다. 이 우선순위는 코드상 비교 조건을 만들 수 있는지에 따른 것이며, 효과를 측정해 좋은 성적만 골라낸 것이 아니다.

- fsspec `1452`와 `1699`: 같은 scheme 기억을 로컬 경로의 경계 문제와 다른 비동기 위임 문제에 비교한다.
- numpydoc `172`와 `283`: 같은 단계별 보존 경험을 설명 소실과 이미 원인·수정 제안이 제공된 문제에 비교한다. `172`와 기억 출처 `145`는 **같은 base commit**이므로 독립 코드 가족으로 세지 않는다.

먼저 별도 환경에서 원문 재현을 확인하고, 원문 오타·불명확한 요구는 연구자 보정과 채점 계약으로 구분한다. 그 뒤 고정 작업자의 초기 조사·완전한 snapshot·성공/회귀 검사·제공 위치·N/P/P+E/P+G 문구와 토큰 차이·반복/비용/중단 규칙을 고정한다. 지금은 작업자·tokenizer·실행 예산을 새로 정하지 않았고 이 네 문제도 실행 가능하다고 확인된 상태가 아니다.

이 작은 실행에서 모두 P+E를 주는 고정 정책으로 충분하거나 실제 효과 차이가 없으면 그 결과를 유지한다. 이를 확인하기 전에 관계 주석만 늘려 선택기 학습을 재개하지 않는다. 학습 재개에는 여러 출처의 실제 효과와 학습 적합·개발 정책 선택 점검이 함께 필요하다. 이 발견 묶음을 보고 방법을 고른 뒤 같은 자료를 독립 확인 시험으로 부르지 않는다.

## 검산과 재수집

[원본 비교 기록](verification.json)에는 87개 도구 응답과 23개 전체 코드 파일의 검산 결과를 남겼다. ID 목록 4,096행과 추가 task metadata 42개도 공개 원본에서 다시 읽어 같은 해시를 확인했다. 이 검사는 주석의 독립 합의나 실제 기억 효과 검증과 구분한다.

저장소 루트에서 공유 파일의 해시·원문 연결·현재 코드 구간·효과 미측정 상태를 검사한다. Python 표준 라이브러리만 필요하다.

```bash
python3 -m data_pipeline.review_memory_evidence_discovery check
```

기존 로컬 원본과 내려받은 전체 코드를 함께 검산할 때:

```bash
python3 -m data_pipeline.review_memory_evidence_discovery check \
  --intake training/memory_selector/outputs/memory-evidence-discovery-20261108-v1
```

새 clone에서 원본을 재수집하려면 `fsspec`, `pyarrow`, 네트워크가 필요하다. 첫 수집은 큰 parquet의 첫 row group에서 선택된 열을 읽는다. source 명령은 실행하지 않는다. 아래 출력 경로가 이미 있으면 원본을 덮어쓰지 말고 새 경로를 사용한다.

```bash
python3 -m data_pipeline.review_memory_evidence_discovery inventory \
  --output training/memory_selector/outputs/discovery-reproduction-inventory.json
python3 -m data_pipeline.collect_memory_evidence_discovery \
  --plan docs/design-data/memory-evidence-discovery-20261108-v1/intake-plan.json \
  --inventory training/memory_selector/outputs/discovery-reproduction-inventory.json \
  --previous-selection docs/design-data/memory-evidence-discovery-20261108-v1/previous-intake-selection.json \
  --output training/memory_selector/outputs/memory-evidence-discovery-20261108-v1
python3 -m data_pipeline.review_memory_evidence_discovery expand \
  --intake training/memory_selector/outputs/memory-evidence-discovery-20261108-v1 \
  --inventory training/memory_selector/outputs/discovery-reproduction-inventory.json \
  --output training/memory_selector/outputs/memory-evidence-discovery-20261108-v1
python3 -m data_pipeline.review_memory_evidence_discovery fetch-code \
  --intake training/memory_selector/outputs/memory-evidence-discovery-20261108-v1
python3 -m data_pipeline.review_memory_evidence_discovery check \
  --intake training/memory_selector/outputs/memory-evidence-discovery-20261108-v1
```

`export_memory_evidence_discovery`와 `build`는 검토 명세에서 **새 경로로** 근거 파일을 생성하는 개발 도구다. 동결된 공유 파일 위에서 다시 실행하지 않는다. `index`는 검토를 마친 새 묶음의 색인을 만드는 명령이며 변조된 자료를 통과시키기 위한 복구 명령이 아니다.

출처는 Nebius의 [고정 trajectory 자료](https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories/tree/35455389ab51bf5e2306bfd436ef72d0f98bf882)와 [고정 SWE-rebench task 자료](https://huggingface.co/datasets/nebius/SWE-rebench/tree/89cdfbab4ab1bd8f5a658bb212d1b63624f4f881)다. 데이터 카드의 CC BY 4.0 표기와 각 저장소의 라이선스는 별개이며, 원본 저장소·revision·URL·`license_name`을 사례 근거에 보존했다. 공유 발췌와 P/E·관계 주석은 이번 검토에서 만든 파생 자료다. GitHub의 `omni-us/jsonargparse`는 확인 당시 `mauvilsa/jsonargparse`로 이동했으며 같은 pinned commit으로 연결했다.
