# 메모리 선택 연구의 외부 데이터와 평가 벤치 조사

2026-11-07. 논문 원문·저자 저장소·Hugging Face 카드와 API를 확인했다. **학습용 경험은 공개 실행 기록에서 확장하고, 내용 개입은 SkillsBench에서 점검하며, 실제 작업 성과는 기존 SWE와 Terminal-Bench로 확인하는 구성을 권한다.** 아래 우선순위와 실험안은 우리의 판단이며 논문 결과와 구별한다.

이번 조사는 유료 모델 호출·추가 학습·GPU 할당을 포함하지 않는다. [조회 기록·카탈로그](reports/data/dataset-benchmark-survey-20261107/README.md)에 HF 21개 저장소의 revision·접근 상태·스키마와 학습 기록 10개의 구조 확인을 남겼다. 전체 대용량 데이터·평가 정답·oracle 파일을 내려받거나 실행하지 않았다. 웹의 카드 미리보기에 노출되는 사례는 완전한 비공개 평가로 취급할 수 없다.

## 우선 선택

| 목적 | 먼저 볼 자료 | 선택 이유 |
| --- | --- | --- |
| 새 출처 경험과 실패 사례 확보 | SWE-rebench OpenHands, SWE-smith의 tool split | 실제 행동·관측·제출 결과가 있고 기존 원본 수집 비용을 줄일 수 있음 |
| 성공/실패보다 세밀한 과정 진단 | AgentProcessBench, CodeTraceBench | 단계별 오류·중립 행동 또는 오류 구간 주석이 있음 |
| 같은 내용을 넣고 빼는 메커니즘 실험 | SkillsBench v1.1 | 작업·절차 문서·실행 환경·검증기가 함께 있고 제공/미제공 비교를 지원 |
| 더 작은 행동 적용 실험 | Mem2ActBench | 기억을 도구 호출 인자로 옮기는 과정에 정답이 있음 |
| 코드 에이전트의 실제 효과 | 기존 SWE 구성 → Verified | 기존 실행·계측을 활용하고 내용 개입에 집중 가능 |
| 다른 실제 작업으로 일반화 | Terminal-Bench 4.0 | 명령·파일·도구·환경 전제가 다양함. 자원 요구가 커 후속 확인으로 배치 |

각 자료가 이미 `유익한 기억 / 유해한 기억` 정답을 제공한다는 뜻은 아니다. 출처 실행의 결과, 현재 조건과의 관계, 기억을 제공한 뒤의 성과 차이를 계속 구분해야 한다. 유명 벤치는 최종 성과의 전달력에 도움이 되지만, 같은 경험을 다음 문제에 재사용할 수 있는 관계까지 자동으로 제공하지는 않는다.

## 1. 경험·학습 재료로 유망한 공개 자료

### SWE-rebench OpenHands — 첫 출처 은행으로 추천

[공식 데이터](https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories)의 train은 **67,074개 실행 기록**이다. `trajectory`, `repo`, `instance_id`, `model_patch`, `exit_status`, `resolved`, 생성 테스트 관련 필드를 제공한다. 이번에 확인한 두 행에는 실제 assistant/tool 왕복과 성공·실패가 각각 있었다. 저자는3,792개 이슈·1,823개 저장소에서 수집했다고 설명한다.

우리에게는 명령 실패→복구, 국소 검사→최종 판정, 특정 환경·API 전제를 가진 처방을 뽑을 출처로 유망하다. 먼저 알려진 저장소와 최종 평가 저장소를 제외한 작은 부분을 골라 원문 위치·조건·관측을 연결하는 방식을 권한다. `resolved=0`을 유해한 기억의 정답으로 만들지 않는다. 같은 이슈의 반복은 같은 분할에 두고, 현재 문제와 연결할 때 별도 task 메타데이터에서 커밋·시간을 확인해야 한다.

### SWE-smith — 통제 가능한 버그 가족을 추가

[공식 실행 기록](https://huggingface.co/datasets/SWE-bench/SWE-smith-trajectories)은 현재 `tool=24,100`, `xml=26,076`, `ticks=25,826`으로 보인다. 처음에는 **tool 24,100개만** 사용한다. 형식별 행을 독립 경험으로 합산하지 않는다. 문서의 옛5,017개 선별 학습 설명과 현재 배포 행 수는 다른 대상을 가리킨다.

[원 논문](https://arxiv.org/abs/2504.21798)은 실제 저장소에 합성한 버그로 실행 가능한 학습 문제를 늘리는 방법이다. 현재 task 데이터는[HF에서59,136개](https://huggingface.co/datasets/SWE-bench/SWE-smith)로 확인됐다. 합성 버그의 유형·같은 원본 코드에서 나온 변형을 묶으면 조건 변화 대조를 설계하기 좋다는 것이 우리의 판단이다. 저장소·기준 커밋·수정 위치·mutation 가족을 함께 분할해야 한다.

### SWE-Gym / R2E-Gym — 보조 출처와 검증 판독

[SWE-Gym 실행 기록](https://huggingface.co/datasets/SWE-Gym/OpenHands-Sampled-Trajectories)은6,055개이며 `messages`, `tools`, `resolved`, `test_result`를 포함한다. 확인한 실패 행에는 빈 제출과 평가 상태가 구분돼 있었다. 기존 SWE 평가와의 저장소 중복, 환경 실패와 에이전트 실패를 확인한 뒤 사용한다.

[R2E 검증 예제](https://huggingface.co/datasets/R2E-Gym/R2EGym-Verifier-Trajectories)는5,750개, [SFT 실행 기록](https://huggingface.co/datasets/R2E-Gym/R2EGym-SFT-Trajectories)은3,231개다. 전자의 확인한 두 행은 `messages`3개와 `rewards`, `docker_images` 구조였다. 최상위 다단계 도구 로그라고 가정하면 안 된다. 패치·검증 주장의 판독 보조 과제와 전체 경험 수집을 구분하고, 원문에 담긴 실행 경로와 출처 ID 복원이 가능한지 먼저 확인한다. 성공 선별 SFT만으로 실패 경험의 다양성을 확보할 수도 없다.

### OpenThoughts-Agent — 터미널 경험의 후보

[공식 SFT 자료](https://huggingface.co/datasets/open-thoughts/OpenThoughts-Agent-v1-SFT)는15,209개다. 저자는 nl2bash와 InferredBugs에서 생성한 실행이라고 설명한다. 확인한 행은 `conversations`, 모델·작업·episode·run 식별자를 담고 있었고, 별도의 `resolved`나 보상 열은 없었다.

쉘 명령·명령 결과·복구 방법의 후보를 모으는 용도로 유망하다. 성과 라벨이 필요하면 원래 작업의 검증기와 다시 연결해야 한다. Terminal-Bench 평가 문제·해법을 이 자료에 보충해 학습하지 않는다. 단순 shell 형식 학습과 새로운 과제에서의 기억 적용 효과도 구분한다.

### AgentProcessBench / CodeTraceBench — 과정의 실패를 더 세밀하게 보기

[AgentProcessBench](https://huggingface.co/datasets/LulaCola/AgentProcessBench)는 BFCL·GAIA-dev·HotpotQA·τ²의4개 하위집합 각250개, 총1,000개 실행이다. `step_labels`, `final_label`, 도구·메시지 정보가 있다. [논문 v2](https://arxiv.org/abs/2603.14465v2)는8,509개 단계 주석을 보고한다. 유익한 탐색과 오류를 구별하는 보조 진단에 적합하다.

그러나 이3분류는 **행동의 옳음·중립·오류**다. 우리 관계 판독의 **근거 있음·모순·미확인**이나 기억의 효용과 다른 정답이다. 숫자 코드가 비슷하다고 합치지 않는다. 공개 split은 전부 test이므로 학습에 사용하려면 개발용 재분할임을 명시하고 원래 벤치의 독립 점수로 보고하지 않는다. 단계 예측에서는 이후 메시지·정답이 현재 입력에 들어가지 않아야 한다.

[CodeTraceBench](https://huggingface.co/datasets/NJU-LINK/CodeTraceBench)는 현재 HF에 `full=3,316`, `verified=1,000`으로 보인다. `solved`, 단계·오류 구간, 원본 artifact 경로가 있다. 두 split의 관계를 확인하기 전에는 독립4,316개로 합산하지 않는다. 주석 테이블과 완전한 원본 로그도 다르다. 터미널 평가 과제와 겹칠 수 있어 최종 평가 목록과 대조한 뒤 진단 자료로 사용하는 것이 좋다.

## 2. 우리 방식을 시험할 평가 벤치

### SkillsBench v1.1 — 내용 삭제·복원에 가장 직접적인 추가 후보

[현재 공식판](https://www.skillsbench.ai/blogs/skillsbench-1-1)은 **87개 작업·8개 영역**이다. [HF task mirror](https://huggingface.co/datasets/benchflow/skillsbench)는 task·환경·skill·oracle·검증기를 제공하고, [별도 결과 저장소](https://huggingface.co/datasets/benchflow/skillsbench-leaderboard)는 제공/미제공 실행 기록을 보관한다. 논문 초기86개·11영역과 현재판을 섞지 않는다.

우리의 **제안**은 제공된 절차를 고정해 무제공 / 전체 / 적용 조건 삭제 / 확인 절차 삭제를 비교하는 것이다. 이후 같은 사실·함수명·수정 정보·검사 정보를 가진 서술형과 지시형을 비교한다. 관련 없는 skill이나 현재 조건에 맞지 않는 skill을 추가하는 조건은 별도의 변형 실험으로 표시한다.

두 가지 경계가 있다. 첫째, skill은 전문가가 작성한 절차 패키지이며 실제 과거 경험에서 추출된 기억과는 출처가 다르다. 둘째, skill에는 텍스트 외에 스크립트·참조 파일도 있을 수 있다. 문장 표현 효과를 보려면 파일·도구 접근은 동일하게 유지해야 한다. 전체 skill package 제공의 효과와 텍스트만의 효과는 다르다.

기존 결과를 탐색하면 성공·악화가 나타난 후보를 찾을 수 있지만 그것을 확인용 평가에 그대로 쓰면 선택 편향이 생긴다. 공식 결과 export도 검토된 non-timeout 실행 기준이므로, 우리 주 지표에서는 정상 예산 종료·timeout을 전체 배정 분모에 포함한 별도 집계를 해야 한다. 전체 결과 저장소는 약208GB로 표시돼 전체 다운로드를 권하지 않는다. manifest·작은 요약·선별된 실행부터 확인한다.

### Mem2ActBench — 기억이 행동 인자로 옮겨지는지 작게 확인

[논문](https://arxiv.org/html/2601.19935v1)은 대화 속 갱신된 사실·선호·상태를 도구 호출에 적용하는 문제다. 준비된 [small 배포](https://github.com/Cantaloupe-M/Mem2ActBench/tree/main/Mem2ACTBench_small)의 통계는 **400문항·429세션**, 대화 파일은 약7.35MB다. 전체 구축 코퍼스는2,029세션으로, small 평가와 규모가 다르다. 생성 파이프라인을 유료로 다시 돌릴 필요 없이 준비된 자료부터 확인할 수 있다.

현재 조건과 충돌하는 과거 값, 최신 수정 사항, 근거가 부족한 상태를 비교하기 좋다. 원문 주 실험은 정답 도구를 제공해 주로 인자 grounding을 본다. 따라서 우리는 도구 제공 조건과 자유 선택 조건을 구분하고, 도구명 정확도·인자별 일치·전체 JSON 호출의 완전 일치를 명확히 정의해야 한다. 원문의 TA/TSA/EM 명칭만 보고 동일한 지표로 가정하지 않는다. 이것은 실제 외부 서비스의 최종 상태를 검증하는 벤치와도 다르다.

### SWE와 Terminal-Bench — 실제 작업 성과의 중심

SWE는 기존 계측과 공식 검사 대조를 활용할 수 있어 계속 중심으로 두는 것이 좋다. 새 평가에서는 같은 출처→대상의 기억 제공/보류·구성요소 포함/제외를 반복하고, 중간 패치·예산 내 제출·공식 통과·검사 범위·전체 비용을 분리한다. 기억의 관련성만 높다고 새 해결률이 오르는 것은 아니다.

Terminal-Bench의 **현재 공식판은4.0**이다. [공식 설명](https://www.tbench.ai/news/terminal-bench-4-0)은 환경 자원·과제·검증기 수정과 기본8시간 제한을 명시한다. [실행 안내](https://www.tbench.ai/run)에는 GPU가 필요한 과제도 포함된다고 나온다. Harbor의 [custom-agent 경로](https://docs.harborframework.com/agents/custom-agents)를 통해 별도 기억 모듈을 붙일 확장면은 있다. 현재 OpenRouter 실행기와의 연결이 이미 검증됐다는 뜻은 아니다.

따라서 제한된 예산에서 바로 전체를 돌리기보다 CPU·텍스트·환경 준비 상태 같은 **결과 전 메타데이터**로 작은 파일럿을 정한다. 시간 한도를 줄이면 그 한도 아래의 효과를 측정한 변형 실험이며 공식4.0 점수와 직접 비교하지 않는다. 초기2.0·2.1 결과와도 버전·검증기를 섞지 않는다. 공식 평가 과제·oracle·공개 해법은 학습 코퍼스에 넣지 않는다.

### 추가 후보의 적합성

| 후보 | 우리 연구에서 볼 수 있는 것 | 우선순위·한계 |
| --- | --- | --- |
| [ALFWorld](https://github.com/alfworld/alfworld/blob/master/README.md) | 텍스트 환경에서 과거 행동 절차가 새로운 배치에 전이되는지 | 비교적 작은 실행 실험 후보. 정확한 위치 암기와 일반 절차를 구분하고 전체 episode를 분할 |
| [AppWorld](https://arxiv.org/abs/2407.18901) | 도구 선택·순서·현재 상태에 맞는 절차, 최종 앱 상태 | 750개 작업·9개 앱. 실행/환경 adapter 준비가 추가로 필요해 후속 확장 |
| [MemoryArena](https://memoryarena.github.io/) | 이전 subtask의 기억이 다음 행동에 미치는 영향 | 명시적 상호의존이 있어 유망. 전체 시퀀스·공통 배경을 함께 분할. 웹 부분은 환경 변동도 고려 |
| [LifelongAgentBench](https://huggingface.co/datasets/csyq/LifelongAgentBench) | 누적 경험의 순서·반복·전이 | HF에서 확인한1,396개는 SQL 형태의 문제 테이블. 전체 벤치의 모든 환경·실행 로그가 담겼다고 가정하지 않음 |
| [AMA-Bench](https://huggingface.co/datasets/AMA-bench/AMA-bench) | 에이전트 이력의 과정·관계·원인 회상 | 208개 episode×12QA. 새 작업 수행 효과와 구분한 보조 판독 평가 |
| [MemoryAgentBench](https://huggingface.co/datasets/ai-hyz/MemoryAgentBench) | 기억 갱신·충돌·선택적 망각 | 행이 긴 stream이고 질문은 내부에 여러 개. 행동 가치 평가를 대신하지 않음 |
| [EvoMemBench](https://github.com/DSAIL-Memory/EvoMemBench) | episode 내/간, 지식/행동의4가지 범위를 함께 점검 | 원래 MemoryAgentBench·BFCL·ALFWorld를 재사용. 새 독립 가족으로 중복 계산하지 않음. 저자는 원본 형식을 권장 |
| [LongMemEval](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned) | 대화 사실의 시간 갱신·미확인 처리 | 회상 보조 평가로 활용. 이번 viewer 응답에서는 사용할 split/schema를 얻지 못했으며 실제 준비 확인은 추가 필요 |

## 3. 아주 가까운 선행연구와 공개 자료 상태

**MemDev-Bench**는[논문](https://arxiv.org/html/2610.27524v1)에서555개 base·4,869개 역사 과제·22개 저장소, 시간 순서와 관련성 주석을 갖춘다고 설명한다. 우리와 가장 가까운 실험 구성 중 하나다. 그러나 이번에 원문·웹 검색·HF 검색에서 내려받을 공식 데이터 패키지는 확인하지 못했다. HF의 정확한 이름 검색 결과0개도 보존했다. 이는 미공개라고 단정하는 근거가 아니라 **현재 확보 확인이 안 됐다는 상태**다. 당장 데이터 확장의 전제로 삼기보다 공개 주소 확인 후 검토한다.

**SWE-Skills-Bench**는[논문](https://arxiv.org/html/2603.15401v1)에서49개 skill·약565개 작업, 버전 불일치에 따른 성과 저하와 토큰 비용을 다룬다. 매우 가까운 선행이다. 다만 논문이 지정한 `GeniusHTX/SWE-Skills-Bench`의 저장소와 README는 이번 GitHub 조회에서404였다. 방법 참고와 즉시 실행 가능한 배포를 구분한다.

**Skill Availability and Presentation Granularity**의[통제 연구](https://arxiv.org/html/2605.31408v1)는30개 작업·6조건·5반복·2모델, 총1,800회를 분석했다. 내용 ledger로 변형을 관리하되 길이·강조·의미 차이의 잔여 혼입을 인정한다. 과제별 반복 평균 후 과제 단위 불확실성을 계산하고, 세부 표현 차이는 불확실하다고 보고한다. 논문은 전체 행별 패키지의 공개를 향후로 명시한다. 우리의 표현 실험도 단순히 지시형이 낫다는 가정으로 출발하지 않아야 한다.

**TabAgent**는[논문 v3](https://arxiv.org/abs/2602.16429v3)와[공식 skill-selection 모델 카드](https://huggingface.co/dolev31/tabagent-skillsbench-skill-selection)에서 작은 분류기로 skill을 고르는 방식을 제공한다. 카드의 자료는87개 작업×195개 후보=16,965행이고 정답은 **큐레이터의 skill 묶음 소속 여부**다. 배포 checkpoint는87개 전체로 학습했으므로 같은87개에 독립 평가하면 안 된다. 우리의 학습 선택기에도 문자열·유사도·이런 간단한 분류기를 강한 기준선으로 둘 가치가 있다. 다만 묶음 소속과 실제 성과·비용의 한계 효과는 다른 목표다.

**ReasoningBank**의[저자 구현](https://github.com/google-research/reasoning-bank/blob/main/README.md)은 성공·실패 경험에서 전략을 만들고 SWE와 WebArena에 재사용하며 mini-SWE-agent 경로도 제공한다. 현재 코드와 연결할 설계 참고가 된다. 이것을 이미 정답이 붙은 대규모 기억 효용 데이터셋으로 소개하지 않는다. 자기 판정·요약 과정의 비용과 오류는 별도로 검토해야 한다.

이 선행들을 보면 별도 작은 모델, 제공/미제공, skill의 이득·손해, 표현 수준 비교 자체는 이미 연구됐다. 우리의 기여 후보는 **실제 경험의 근거 있는 어떤 조각이 현재 조건에 따라 가치가 바뀌는지, 그 차이를 새 출처에 전이할 수 있는지, 판단 비용까지 포함해 정책을 개선하는지**로 더 구체화하는 것이 적절하다. 아직 그 독창성과 효과가 입증된 것은 아니다.

## 4. 지금 계획에 반영할 순서

1. **출처 은행을 넓힌다.** 제안은 SWE-rebench train에서100개 출처 실행·20개 저장소 정도를 목표로 작은 수집 명세부터 만드는 것이다. 성공·실패·환경 오류·국소 검사와 최종 판정의 차이를 층별로 확보한다. 기존 경험 및 최종 평가와의 중복을 결과 전에 제거하고, 같은 이슈 반복·수정 가족은 함께 분할한다. 이 규모는 실행 완료 수가 아닌 다음 계획의 제안이다.
2. **현재 관계 자료와 보조 과제를 분리한다.** 로그의 종료 상태와 숫자는 구조화하고, 기억의 전제·검사 범위·현재 API·설정을 대조한다. 과정 주석을 보조 목표로 쓰더라도 우리3분류와 숫자 코드만으로 합치지 않는다. 공개 test를 학습 자료로 전용하면 개발용 사용임을 기록하고 최종 평가를 새 가족에서 만든다.
3. **SkillsBench에서 작은 내용 개입을 준비한다.** CPU·텍스트 과제 중 사전 기준으로6–10개 정도를 골라 oracle/무동작 대조를 확인한다. 전체·선택 조각 삭제·복원에서 다른 파일·스크립트·질문·제출 안내를 고정한다. 과제별 이득이 관측됐다는 이유로 확인용 문제를 고르지 않는다. 이 단계는 자원·노출 경로 점검이며 표본이 작아 효과의 확증은 어렵다.
4. **고정된 선택기를 SWE에서 검증한다.** 항상 제공·항상 보류·유사도·명시적 전제 규칙·학습 선택기를 같은 후보와 예산에서 비교한다. 새로운 출처→대상에서 배정 전체의 공식 성공과 전체 비용을 모은다. 이후 Terminal4.0 또는 AppWorld로 범위를 확장한다. 작은 도구 인자 실험이 필요하면 Mem2ActBench를 별도 보조 축으로 둔다.

효용은 같은 현재 상태·나머지 기억S에서 `평균 해결(S+K) − 평균 해결(S)`와 `평균 비용(S+K) − 평균 비용(S)`로 측정한다. 현재 조건이 맞는 층·맞지 않는 층의 차이를 결과 전에 정의한다. 한 번의 성공·실패 반전을 개별 기억의 확정 효용으로 만들지 않는다. 새로운 출처→대상 묶음과 반복을 늘리고, 출처/작업 가족 단위의 불확실성을 보고한다.

우리 모델이 적용 관계만 잘 맞히거나 관련 skill 목록을 잘 복원한 결과는 그 수준으로 보고한다. 실제 해결률 개선 없이 오류·비용만 줄면 그 효과를 따로 제시한다. 전조건 성공·실패인 패널에서는 성공률 효과의 학습 신호가 부족하므로 과정 지표와 새 문제 구성으로 돌아간다.

## 5. 사용을 보류하거나 별도 취급할 자료

[TRAIL](https://huggingface.co/datasets/PatronusAI/TRAIL)은148개 실행·841개 오류 주석으로 설명되지만, 자동 gated 상태이고 이번 schema 조회는401이었다. 카드는 평가 자동화 시스템을 학습시키는 용도로 쓰지 말라는 별도 사용 범위와 재공유 조건을 명시한다. 내용을 확보하거나 조건에 동의하지 않았고, 우리 선택기 학습용으로 추천하지 않는다. 오류 taxonomy의 문헌 참고와 허용된 평가 용도를 구분한다.

[Procedure Memory System Benchmark](https://huggingface.co/datasets/shiweid1/Procedure_Memory_System_Benchmark)는 같은 무메모리 source rollout에서 은행을 만든 뒤 고정해 ALFWorld·BFCL·AppWorld를 비교한 커뮤니티 기록이다. 실패·성공 사례 탐색과 재현 참고에는 가치가 있다. 하지만 시스템 전체·내용·전달 방식이 다르고 사례당 한 번인 비교를 개별 기억의 효과 정답으로 가져오지는 않는다. 원래 benchmark의 데이터 범위·재공유 조건도 상속해서 확인한다.

이번 API에서 license 필드가 비어 있는 저장소가 몇 개 있었지만 그것만으로 사용 불가라고 판단하지 않았다. 예를 들어 Mem2ActBench의 GitHub API license는null이었으나 실제 README는MIT를 명시했다. 카드·파일·원출처를 함께 확인한 사실을 기록하며, 접근·배포 확인이 안 된 자료를 이미 실행 가능한 학습 세트라고 부르지 않는다.

## 기록과 검증 범위

[기계 카탈로그](reports/data/dataset-benchmark-survey-20261107/catalog.json)·[CSV](reports/data/dataset-benchmark-survey-20261107/catalog.csv)에는21개 HF 저장소의 용도·우선순위·한계가 있다. 메타데이터는21개 모두200, 사용할 feature schema는16개에서 얻었다. package 형태·비활성 viewer·gated·빈 schema는 구분했다. 공개된 학습 trace5종에서 첫2개씩만 구조를 확인했으며, 대표성이나 전체 성공 분포를 추정한 표본이 아니다.

Hub revision은 기록했지만 dataset viewer가 동일 revision에서 응답했다고 보장할 수 없다. 실제 수집에는 고정된 원본 파일을 내려받아 ID·중복·내용 해시를 다시 확인해야 한다. 이번 조사만으로 환경 실행·oracle 통과·OpenRouter 연결·지표 구현이 끝난 벤치는 없다. 기존74행 발견 자료와 원래 실험 결과도 변경하지 않았다.
