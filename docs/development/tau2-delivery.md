# 고정 기억의 시작 제공과 자율 조회 비교

2026-11-14 · 구현·오프라인 검증·실제 개발 비교

**같은 고정 은행을 여러 과제·조건·반복에 배정하고, 본문 시작 제공과 자율 조회를 같은 native τ² 경로에서 비교할 수 있다.** `full_preload`는 시작 snapshot의 모든 제목·본문을 원문 그대로 제공한다. 과제·반복·예산이 비어 있는 현재 [프로필 초안](../../protocols/drafts/tau2-delivery-comparison-v1.json)은 실행 가능한 실험이 아니다.

전체 연구 계획을 해석하는 범용 PlanCompiler는 후속이다. 이 실행기는 쓰기가 없는 고정 은행 전달 비교를 위한 구현이며 [기존 자율 조회 파일럿](tau2-autonomy-pilot.md)과 과거 원본을 유지한다. 작성 시점·표현·누적·다른 벤치와 내용 개입은 전체 연구 범위에 남는다.

별도 [고정 프로토콜](../research/delivery-development-pilot.md)로 실제 train 세 과제·네 조건·48회를 완료했다. [결과·해석](../reports/20261114-tau2-delivery-development-results.md)과 [receipt](../../artifacts/pilots/20261114-tau2-delivery-development-results.json)를 확인한다. 아래 초안의 미정 값과 실제로 실행한 고정 프로필은 구분한다.

## 전달과 출처

| 조건 | 은행 | 시작 시 본문 | 조회 도구 |
| --- | --- | --- | --- |
| `no_memory` | 없음 | 없음 | 없음. 기억 지침도 없음 |
| `empty_bank` | 빈 snapshot 복제본 | 없음 | list/get |
| `on_demand` | 고정 기억 snapshot 복제본 | 없음 | list/get, 사용은 모델 선택 |
| `full_preload` | 같은 고정 기억 snapshot 복제본 | 모든 제목·본문 | `preload_query_tools`로 명시 |

`empty_bank`와 `on_demand`의 지침·도구·정책은 같다. `full_preload`의 조회 도구를 유지하면 내부 모드는 `preload_and_on_demand`, 끄면 `preload`다. 실행별 정책 JSON에 모드를 기록하므로 전달 정책 전체의 비교로 해석한다. 현재 대조만으로 기억 내용의 효과와 모든 문맥 길이·지침 효과를 분리했다고 주장하지 않는다.

본문은 system 다음 위치의 별도 user 문맥 메시지에 고정 순서의 JSON으로 넣으며, 이후 worker 요청에도 같은 문맥으로 재노출된다. 공개 필드는 항목 ID·버전·제목·본문이다. 원천 대화·분석 주석·채점 정답·은행 revision은 제공하지 않는다. 시작 시 준비는 모델 요청 전에 한 번 수행하고, 실제 요청마다 제목·본문 구간과 해시를 다시 연결한다. 사용자 시뮬레이터에는 이 메시지와 기억 도구를 직접 제공하지 않는다. 작업자가 사용자에게 전달한 답변은 정상 대화로 유지한다.

자동 제공을 list/get 호출로 꾸미지 않는다. `memory.preloaded`는 제공할 자료의 준비이고, `memory.input_included`는 특정 요청 안의 구간이다. 요청·응답의 delivery status와 함께 읽어야 하며 전송되지 않은 요청이나 입력 포함 자체를 실제 활용으로 세지 않는다.

기존 v1.1 스키마·fixture·실행을 보존하고 [v1.2 확장](../../schemas/README.md)을 추가했다. 새 journal v1.2에는 기존 v1.1 사건과 자동 제공용 v1.2 사건이 함께 들어간다. snapshot 출처가 쓰인 요청 manifest만 v1.2이며, 출처 종류는 `memory_snapshot`, 연결 ID는 `preloaded_event_id`다. 같은 실험 안에서 journal 버전을 바꾸지 않는다.

## 실행 준비와 배정

프로필에는 과제별 `id`·`source_group_id`, 조건, 반복 수, 배정 seed, 고정 source descriptor, 페이지 크기·기억 한도, 전체 호출·금액·시간 한도와 1차 비교를 명시한다. 출처 그룹은 연구자가 지정한 값을 보존하며 자동으로 올바른 그룹을 추론하지 않는다. [과제 후보·채점 기준](../research/tau2-task-selection-and-rubric.md)은 분석 자료이며 실행 입력의 허용 출처가 아니다.

현재 전송 프로필은 Luna reasoning low·DeepSeek V4.1 Flash 1017·제공자 고정·전체 응답 캐시 끔을 유지한다. 과제 ID만 배정에 맞게 바꾸고 다른 native·모델 설정은 참조한 연결 프로필을 따른다. 따라서 해당 프로필의 native 40단계·오류 5회, run별 모델 요청 24회·시간 180초 설정도 실행 전에 확인할 값이다. 새 프로필의 guards는 실험 전체 한도이며 실제 설정과 원본에 함께 남는다. 사용자 모델에 미지원 seed를 보내지 않는다. 동일 seed 반복을 독립 난수 표본으로 간주하지 않는다.

준비 검사는 공식 분할·과제 존재, 고정 source의 출처·종료·비어 있지 않음·변조 여부, 저장 한도와 본문 전체의 제공량 한도를 확인한다. 본문이 한도에 맞지 않으면 전송 전에 거절하며 자르거나 자동 요약하지 않는다. 모델 문맥 한계나 provider 오류도 기록하며 임의 재시도로 원본을 덮지 않는다. source가 비어 있지 않을 때까지 다시 작성하는 절차는 없다.

배정은 결과를 보기 전에 고정한다. 과제·반복마다 조건 블록을 만들고 seed로 블록 순서를 섞는다. 조건 순서는 무작위 시작 순열을 회전하며, 조건 수만큼 반복한 구간에서는 각 위치가 균형을 이룬다. 회전이 덜 끝난 반복에서는 완전한 위치 균형을 보장하지 않는다. 모든 회차는 새 작업자·사용자·업무 환경·session과 은행 복제본을 쓴다.

## 명령

초안의 미정 필드를 확인한다. 네트워크·모델 호출과 출력 폴더 생성은 없다.

```bash
uv run --extra tau2 python scripts/run_tau2_delivery.py
```

새 파일로 실행 프로토콜을 확정한 뒤 로컬 준비 검사를 한다. 이 단계도 네트워크를 쓰지 않으며 실제 모델의 가용성 확인은 별도다.

```bash
uv run --extra tau2 python scripts/run_tau2_delivery.py --config protocols/ready-delivery.json --preflight
```

실제 실행에는 `ready` 프로필과 새/빈 폴더가 필요하다. 실행 준비에서 모델·제공자의 현재 지원 파라미터를 확인한 뒤 명시한 한도로 호출한다.

```bash
uv run --extra tau2 python scripts/run_tau2_delivery.py --config protocols/ready-delivery.json --output runs/new-delivery --live
uv run --extra tau2 python scripts/run_tau2_delivery.py --output runs/new-delivery --audit-only
```

위 ready 파일명과 출력 폴더는 사용 예시다. 실제 개발 실행은 [별도 프로필](../../protocols/pilots/tau2-delivery-development-v1.json)을 고정해 사용했고 초안은 미정 상태로 유지했다. `--audit-only`는 새 요청을 보내거나 원본을 고치지 않으며, 가져온 source의 보관 로그와 blob으로 원래 source 폴더 없이도 검사한다. 대상 전달 실행의 원본 폴더는 필요하다.

## 분석과 검증 범위

`delivery-summary.json`은 배정·실행·채점·생략, 과제별 조건 결과, 제목/본문 입력 구간, 조회 횟수·업무 호출·native 단계·오류·토큰·비용을 원본에서 재계산한다. 1차 비교와 무기억 반복의 관측 반전도 남기지만 공유 과제·은행·출처 그룹의 비교를 독립 표본으로 검정하지 않는다. 기억 생성 비용은 평가 지출과 구분한다. 미확인 비용이 발생하면 남은 배정을 생략하고, 미실행·미채점을 실패로 채우지 않는다.

공식 결과는 native 채점을 유지한다. 필요한 행동·금지 행동·사용자 설명의 의미 판정은 [주석 기준](../research/tau2-task-selection-and-rubric.md)에 따라 별도로 진행한다. 현재 실행기가 그 기준의 자동 판정기나 통계 추론까지 구현한 것은 아니다.

[파일 코어 검증](../../tests/test_preload.py) 8개는 원문·노출·다른 은행·역할·버전·시점·한도를 검사한다. [전달 비교 검증](../../tests/test_tau2_delivery.py) 8개는 draft 거절, 배정/위치, 분할, 두 과제·네 조건의 native 경로, 조회 도구 없는 제공, 전송 전 용량 검사, 비용 미확인 시 생략과 원본 audit를 다룬다. 새 세션·DB 분리, 원천 대화 차단, 본문 노출과 native 단계 보존을 모의 HTTP로 확인했다. DB를 변경하지 않은 모의 실행에서 과제 0은 공식 성공, 과제 40은 공식 실패로 구분됐다. 이는 채점 연결 검증이며 새 Luna 실험 결과가 아니다.
