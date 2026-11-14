# 실제 개발 경험과 현재 조건의 관계 — discovery v1

2026-11-07. [확장 보고서](../../../docs/reports/2026-11-07-memory-relation-case-expansion.md), [학습 전달서](../../../training/memory_selector/handoffs/2026-11-07-memory-relations.md).

과거 개발 경험의 어떤 전제가 현재 코드·설정·입력과 일치하는지 판독하는 작은 발견용 자료다. **적용 관계의 정답과 실제 이득·손해의 정답을 구별한다.** 새 모델 호출·신경망 학습·GPU 할당은 하지 않았다.

## 구성과 집계 단위

| 자료 | 규모 | 해석 |
| --- | ---: | --- |
| 실제 출처 경험 / 원본 기억 카드 | 10 / 20 | 6개 저장소. 기존 경험의 사실형·절차형 원문 보존 |
| 공개 기준 코드·문제 맥락 | 22과제 | 기존 에이전트 대상 17개와 맥락만 추가한 5개 |
| 관계 판단 | 74행, 14개 현재 과제 맥락 | 62개 관측·코드 관계, 5개 기억 교체, 7개 현재 정보 가림 |
| 관계 라벨 | 근거 있음 30 / 모순 32 / 미확인 12 | 전체 기억의 유용성 라벨이 아님 |
| 올바른 환경에서 수행한 국소 확인 | 29개 | 정상 반환 24개, 의도한 라이브러리 예외 5개 |
| 같은 기억·질문, 다른 현재 조건의 정답 변화 | 17그룹 | 독립된 17개 출처가 아님 |
| 같은 현재 입력·질문, 다른 기억의 정답 변화 | 9그룹 | 실제 기억 2×2 교차 4그룹 + 전제 부재 대조 5그룹 |
| 제공·보류 후보 | 38개 / 출처→대상 19쌍 | 기존 사실형·절차형 본문. 적용·효용 라벨 모두 null |
| 실제 과거 배정 실행 | 116개 | 결과 확인 115개, 외부 중단 1개. 새 실행이 아님 |
| 출처 판정 판독 진단 | 20개 | 현재 적용성 학습에서 분리 |

74행은 독립적인 74개 경험이 아니다. 모든 출처 가족은 프로젝트에서 이미 검토했다. 새 독립 확인 세트로 사용할 수 없다. 현재 정보는 실제 공개 코드 또는 연구자가 고정한 국소 확인 프로그램에서 왔으며, 모든 프로그램이 실제 에이전트의 자연스러운 행동인 것은 아니다.

## 먼저 사용할 파일

- [learner-inputs.jsonl](learner-inputs.jsonl): `sample_id`와 두 문자열 `input.state_text`, `input.candidate_text`만 포함한다.
- [supervision.jsonl](supervision.jsonl): ID, 정답, 출처·대상·facet·stratum. 이 메타데이터는 예측 입력이 아니다.
- [task-contract.json](task-contract.json): 새로운 단일 3분류 관계 과제의 지시·라벨 순서·입력 허용 목록.
- [relations.jsonl](relations.jsonl): 질문, 라벨 근거, 출처 경로·SHA256, 관측 시점이 포함된 전체 주석.
- [case-table.csv](case-table.csv): 사람이 읽을 사례 검토표. 연구자 1인의 근거 검토이며 독립 이중 주석은 아니다.
- [source-bank.json](source-bank.json): 원본 카드와 실제 출처 판정. 본문을 고치지 않았다.
- [paired-contrasts.json](paired-contrasts.json): 현재 조건 또는 기억 교체로 답이 바뀌는 묶음.
- [group-splits.json](group-splits.json): 연결된 출처·대상·파생 사례를 함께 두는 6개 저장소 제외 검증 fold. 최종 test는 없다.
- [transfer-candidates.jsonl](transfer-candidates.jsonl): 향후 에이전트 제공·보류 후보. 사실형과 절차형의 정보량도 달라 순수한 표현 방식 대조가 아니다.
- [effect-observations.jsonl](effect-observations.jsonl), [effect-cells.csv](effect-cells.csv): 실제 과거 행동·성공·비용 기록. 개별 기억의 반사실 효용 라벨은 없다.
- [baseline-diagnostics.json](baseline-diagnostics.json), [token-audit.json](token-audit.json), [validation.json](validation.json): 편법·길이·근거 연결 검사.
- [preservation-audit.json](preservation-audit.json): 기존 고정 기록 5,528개와 첫 학습 산출물 54개의 해시 대조.

## 라벨과 관측 시점

`0=supported`, `1=contradicted`, `2=not_established`다. **질문에 적힌 제한된 관계**에 대한 정답이다. 예를 들어 원래 구현 파일이 사라졌다는 모순은 그 기억의 원리까지 쓸모없다는 뜻이 아니다. 미확인은 자동 보류 정답도 아니다. 앞선 숫자 근거 판독·두 출력 범위 판독과 학습 목표가 다르다.

상태 쪽에는 현재 질문·관측만, 후보 쪽에는 과거 기억·출처 근거를 둔다. 현재 과제의 숨은 정답 패치·평가 테스트 패치·최종 해결 결과를 예측 문자열에 넣지 않는다. 과거 **출처**의 공식 결과는 이미 알려진 정보로 후보에 명시하며, 이 표시의 편법 효과도 진단한다.

현재 출력이 필요한 사례는 원시 반환값·예외·형태를 관측한 **뒤**의 관계 판독이다. `resolved_to_str_type` 같은 정답 판정 flag는 입력에서 제거한다. 이 데이터로 만든 모델을 시작 시 선택기로 사용할 때, 이후에만 얻는 관측을 소급해 넣어서는 안 된다. 공통 조사·확인을 실제 정책에 포함하면 그 비용도 별도 지불해야 한다.

기억만 보는 비교도 **관계 질문은 유지**하고 현재 관측을 제거한다. `state_text`에서 첫 빈 줄 앞의 질문 블록을 추출할 수 있다. 질문까지 제거한 `candidate_text` 단독 비교는 과제 정체성도 없애는 더 강한 제거 대조다.

## 코드·환경과 준비 오류

`evidence/public-code/`는 정확한 `base_commit`의 `git show` 결과다. 공개 필드는 문제·저장소·버전·기준 커밋·작성일로 제한했다. 현재 파일과 기준 파일의 해시도 기록한다. `django__django-15213`은 기존 다른 이미지의 Git 객체에서 **정확한 과거 코드만 읽은** 예외다. 그 이미지로 해당 과제를 실행했다고 주장하지 않으며 `runtime_eligible=false`다.

국소 실행은 네트워크 없는 읽기 전용 컨테이너, 1 CPU·1GB, `/tmp` 임시 공간에서 수행했다. Python은 실제 에이전트와 같은 `/opt/miniconda3/envs/testbed/bin/python`을 명시했다. sklearn의 옛 파일이 Git 트리에서 없어도 런타임이 생성한 호환 wrapper가 존재할 수 있어 `evidence/compatibility-wrappers/`로 별도 보존했다.

최초 25개 준비 확인에서 기본 Python을 잘못 선택해 의존성 import 오류 등이 발생했다. 해당 결과와 당시 코드는 [preparation-failure-default-python/](preparation-failure-default-python/)에 보존하고 라벨·29개 완료 집계에서 제외했다. 이후 올바른 환경의 25개, 추가 3개, 별도 구버전 alias 1개를 사용했다. 뒤의 추가 확인은 앞 결과를 보고 선택한 탐색이다. `runtime-plan.json`, `runtime-plan-v2.json`, `runtime-extension-v3.json`, `probe-revisions/`에 순서를 보존했다.

## 알려진 편법과 사용 제한

- 7개 현재 정보 가림은 명시적으로 관측 부재를 말하므로 쉽게 미확인으로 분류할 수 있다. 이 층은 62개 알려진 조건과 따로 평가한다.
- 5개 기억 교체는 같은 저장소의 실제 다른 경험이지만, 필요한 전제가 없는 비교다. 관련성 구분만으로 풀리는 효과와 복잡한 적용 판단을 혼동하지 않는다.
- 실제 기억 2×2 교차 8행은 class·직접 상속 관계를 묻는다. 간단한 규칙으로 풀 수 있다. 별도 신경망의 필요성을 입증하지 않는다.
- 같은 입력의 충돌 정답으로 계산한 상태 단독 상한은 전체 87.8%, 알려진 조건 93.5%다. 여전히 상태 단독 모델이 높은 점수를 얻을 여지가 있다. 기억+질문만의 상한은 각각 63.5%, 67.7%다. 이는 **관측 자료 내부의 정보 상한**이며 실제 학습 성적이 아니다.
- 별도 모델 평가에는 전체 관계 입력, 상태만, 질문+기억만, 질문 서식·길이, 출처 성공 여부, 명시적 규칙·실행 기준선을 함께 둔다. 단일 총 정확도만 보고하지 않는다.
- 실제 행동 기록은 `framing12 / transfer24 / prescription32 / replication16 / restoration32`를 분리한다. 누적48회를 다시 더하지 않았다. 정상 예산 종료 미제출은 실패로, 외부 중단은 미확인으로 남겼다. 관측되지 않은 할당 확률을 만들어 오프라인 정책 추정을 하지 않는다.
- 국소 실행으로 성립을 확인한 조건도 전체 SWE 과제의 해결을 보장하지 않는다. 출처 실패 경험의 일부 정보가 맞다고, 그 기억이 이득이라는 뜻은 아니다.

## 재현·무결성

빌드 스크립트는 고정 후의 덮어쓰기를 거절한다. 변경은 새 버전 디렉터리에서 한다. `audit_dataset.py`는 빌드 중 검사와 진단 JSON을 생성하므로 고정 후에는 원본에서 다시 실행하지 않는다. 사후 읽기 전용 검사는 다음과 같다.

```bash
uv run --frozen python datasets/memory_relations/20261107-discovery-v1/verify_frozen.py
uv run --frozen python datasets/memory_relations/20261107-discovery-v1/audit_preservation.py --check-only
```

전체 다시 수집에는 이 컴퓨터의 기존 이미지와 로컬 원본 `runs/`가 필요하다. 이미지 ID·원본 파일 SHA는 보존했으나 이미지 전체와 원본 실행 전체를 이 폴더에 복제한 공개 배포 패키지는 아니다.
