# 이전 데이터 보관과 재사용

2026-11-14 · 추후 재사용을 위한 작업 트리 snapshot

**이전 `ai-memory`에서 만든 datasets 전체와 설계 자료를 새 레포에 복사해 보존했다.** 현재 자율 기억 프레임워크의 기본 입력으로 연결하지 않았다. 원본 `/home/depa/ai-memory`는 그대로 두었다.

## 위치와 보관 범위

보관 루트는 [archive/legacy-ai-memory/20261114-v1](../../archive/legacy-ai-memory/20261114-v1/)이다. 원래 경로 구조를 유지해 dataset 간 상대 참조를 보존한다.

| 범위 | 파일 수 | 역할 |
| --- | --- | --- |
| [datasets](../../archive/legacy-ai-memory/20261114-v1/datasets/README.md) | 853 | 34가족의 데이터·버전·manifest·입력·주석·분할·일부 실제 결과 |
| [docs/design-data](../../archive/legacy-ai-memory/20261114-v1/docs/design-data/) | 180 | 설계 예시·검토 패킷·근거·대조·프레임워크 합성 계약 |
| [data_pipeline](../../archive/legacy-ai-memory/20261114-v1/data_pipeline/README.md) | 91 | 당시 수집·생성·검토 코드의 참고본 |
| 과거 루트·문서·보고서 Markdown | 144 | 당시 데이터 역할·라벨 의미·출처·재현 한계의 설명 |

전체 1,268파일·212,081,023bytes, 약 202.26MiB다. JSON·JSONL·CSV·gzip의 원래 bytes를 유지하며 버전 이름·라벨·분할을 바꾸지 않았다. 네 개의 원래 ignore된 여행 JSONL도 복사했다. 파이썬 캐시와 가상환경은 데이터에 포함하지 않는다.

`runs/`, `evaluation/results/`, `docs/reports/data/`, 학습 출력·가중치·실행 환경과 기존 실험 구현은 원래 위치에 남겼다. 데이터 안에 이미 포함된 결과는 보존했지만, 원시 실행 전체를 옮기거나 과거 실험을 재현한 것은 아니다. 기존 문서의 일부 링크·명령은 이 추가 자료를 필요로 한다.

## 원본 식별과 무결성

[파일별 manifest](../../artifacts/legacy-datasets-manifest.json)에 선택 범위, 파일 경로·크기·SHA-256·역할·복사 시 Git 상태·HEAD blob ID를 기록했다. 출처 HEAD는 `d0044c6367e17c1488db4dae37f44e5b358342f9`지만, **이번 데이터 보관은 작업 트리 기준**이다.

| 복사 시 출처 상태 | 파일 수 |
| --- | --- |
| Git 추적 파일·변경 없음 | 995 |
| Git 추적 파일·미커밋 수정 | 10 |
| 미추적 | 259 |
| 원래 ignore됨 | 4 |

HEAD만으로는 미커밋 내용을 재구성할 수 없다. 최초 설계 문서의 고정 commit 이관과 구분해 별도 manifest를 사용한다. 보관 파일을 고치거나 해시를 현재 원본에 맞춰 갱신하지 않는다. 추후 새 자료는 새 버전과 새 manifest를 만든다.

```bash
# 새 저장소만으로 전체 파일 목록·크기·SHA-256 확인
uv run python scripts/verify_legacy_data.py

# 원래 작업 폴더가 있을 때 현재 원본과도 비교
uv run python scripts/verify_legacy_data.py --source-root /home/depa/ai-memory

# 작은 임시 파일에서 손상·누락·추가·경로/링크·출처 변경 탐지 확인
uv run python -m unittest discover -s tests -p 'test_legacy_data_archive.py'
```

두 번째 명령은 현재 원본의 변경도 탐지한다. 이후 원본이 정상적으로 발전하면 비교가 달라질 수 있고, 그것만으로 보관본이 잘못된 것은 아니다. 기본 검증은 이전 저장소·API·GPU·예전 학습 의존성을 요구하지 않는다. bytes의 일치를 확인할 뿐 과거 라벨·의미·득실 측정의 타당성을 새로 증명하지 않는다.

## 찾아보는 순서와 활용 후보

먼저 [과거 datasets 안내](../../archive/legacy-ai-memory/20261114-v1/datasets/README.md)와 [역할 목록](../../archive/legacy-ai-memory/20261114-v1/datasets/catalog.json)을 읽고, 해당 버전의 README·계약·manifest를 확인한다. 저장된 입력과 정답/분석 주석의 공개 경계를 지킨다.

| 자료 | 향후 참고할 수 있는 용도 | 해석할 때 보존할 구분 |
| --- | --- | --- |
| `hidden_memory_traps`, `memory_context_session` | 긴 기억 은행·관측 시점·세션 분리·조회 오류의 사례 | 저작/설계된 관계 정답과 실제 자율 저장·작업 성공은 다름 |
| `memory_state_supplement`, `memory_state_augmentation`, `memory_context_value` | 같은 기억과 다른 현재 관측·목표의 대조 | 부모 버전·본문 해시로 결합. 상태 수를 독립 경험 수로 세지 않음 |
| `memory_decisions`, `api_worker_utility`, `travel_worker_utility` | 과거 제공/보류 득실·고정 작업자의 평가 방식 | 당시 환경·작업자·제공 방식의 관측이며 새 Luna·τ² 효과가 아님 |
| `memory_source_pilot`, `memory_source_scale`, `memory_external_test` | 출처·분할·부분 감독·외부 평가의 관리 사례 | 관계 감독과 효용, 개발과 이미 본 시험 자료를 구분 |
| `statebench_preparation`, `statebench_pilot`, `memory_units` | 경험에서 근거·절차를 추출하는 사례 | 준비 자료·원문·실제 결과·미주석 항목의 역할을 구분 |
| `docs/design-data` | 검토 패킷·정보 누출·기록 연결의 설계 사례 | 합성/예시를 실제 수행 결과로 집계하지 않음 |

이는 활용 가능성의 안내이며 새 평가셋 채택 결정이 아니다. 전체 목록과 나머지 가족은 보관된 catalog에 있다.

## 새 프레임워크에 도입할 때

과거 기억의 관계 라벨을 자율 작성 기억의 유일 정답으로 붙이지 않는다. 고정 은행에 대한 관련성·근거 상태·직접 적용 가능성·실제 제공 효과는 서로 다른 감독이다. 이미 검토·학습·평가한 자료를 새 독립 test라고 부르지 않는다.

도입하는 실험에서는 원본 snapshot·파일 해시·부모 버전·분할/출처 그룹·채택 목적·작업자 공개 필드를 기록하고 변환본을 새 위치에 만든다. 필요하면 새 자율 생성 기억에 별도로 근거/적용성 주석과 통제된 효과 측정을 추가한다.

과거 문서의 BM25·2k 예산은 역사적 실험 설정이다. 새 프레임워크의 공통 정책·예산·도구 계약으로 승격하지 않는다. 과거 생성 코드는 원본 위치를 덮어쓰지 말고 별도 환경·새 출력 경로에서 검토한다. 현재 기준은 [연구 계획](../research/plan.md)과 [상세 설계](../design/architecture.md)다.
