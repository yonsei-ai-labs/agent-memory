# 새 문제군 수집을 위한 공개 데이터 조사

2026-11-09. **외부 검색 평가는 LongMemEval, 학습 사례 확장은 PersonaMem-v2, 도구 인자 적용은 정제한 Mem2ActBench, 절차·환경 전제 분석은 LongMemEval-V2를 우선 검토하는 것이 적절함.** 실제 환경에서의 성공·손해 검증은 BFCL multi-turn 또는 AppWorld로 이어갈 수 있음.

Hugging Face 저장소 18개의 공개 메타데이터와 공식 저장소·원문을 확인해 24개 후보 항목을 정리함. 후보 항목 수는 독립 원천 수가 아님. 작은 원문 파일·일부 레코드의 구조를 조사했으며, **학습 데이터에 편입하거나 모델을 실행한 결과는 아님.** [후보 목록 JSON](reports/data/fresh-source-survey-20261109/catalog.json)·[CSV](reports/data/fresh-source-survey-20261109/catalog.csv)에 역할·우선순위·한계를 기록함.

## 우선 후보와 사용할 역할

| 후보 | 확보할 사례·라벨 | 우리 연구에서의 역할 | 먼저 확인할 점 |
| --- | --- | --- | --- |
| [LongMemEval](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned) | 대화 이력, 질문·답, 근거 세션/turn 표시 | 현재 검색기의 여행 밖 전이, 시간·갱신·보류 평가 | 평가 자료를 학습에 섞지 않음. oracle과 전체 이력 구분 |
| [PersonaMem-v2](https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2) | 합성 persona별 이력, 선호·갱신·주체·근거 snippet | 다양한 기억 내용과 적용 조건의 학습 후보 | persona 단위 분할, 정답·전체 프로필의 입력 누출 차단 |
| [Mem2ActBench](https://github.com/Cantaloupe-M/Mem2ActBench) | 과거 대화→현재 요청→도구 인자, 출처·grounding | 기억을 찾아 실제 호출 인자에 적용하는 소규모 대조 | 공개판의 참조 연결 예외와 schema 불일치 정제 필요 |
| [LongMemEval-V2](https://huggingface.co/datasets/xiaowu0162/longmemeval-v2) | 웹·업무 agent 이력과 절차·환경·전제 질문 | 과정 정보·실패 경험·적용 전제의 외부 진단 | 근거 state ID 추가 주석, 공유 은행과 멀티모달 범위 구분 |
| [BFCL](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard) | memory API 선행 대화와 후속 질문, stateful multi-turn | 저장/조회 행동 및 도구 실행 검증 | memory QA와 multi-turn 실행 평가를 별도 취급 |
| [AppWorld](https://github.com/StonyBrookNLP/appworld) | 앱 과제·검증 해법·실행 채점·환경 | 메모리 제공 전후의 실제 과제 성공·비용 | 같은 task generator의 파생 문제를 같은 가족으로 분할 |
| [LifelongAgentBench](https://huggingface.co/datasets/csyq/LifelongAgentBench) | SQL·OS·KG 과제, instruction·skill 정보 | 도메인을 바꾼 절차 재사용과 경험 축적 | 도메인별 원본 로더와 실행 환경 필요 |
| [SWE-rebench trajectories](https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories) | 실제 코드 agent의 명령·응답·성패 | 새로운 저장소·과거 경험의 원문 수집 | 현재 사용한 이슈/PR/수정 가족 제외, 과거→현재 시점 복원 |

역할·우선순위는 이번 연구에 대한 판단임. 실제 사용 가능 범위는 아래의 파일 확인 결과와 각 원본의 고정 버전을 따름.

## 직접 파일을 보고 확인한 핵심

### LongMemEval: 근거가 연결된 외부 평가용으로 우선 적합

[공식 문서](https://github.com/xiaowu0162/LongMemEval)는 500개 평가 질문과 정보 추출·여러 세션 추론·갱신·시간·보류 유형을 제공함. 현재 `S` 원문 첫 레코드에서도 `question_id`, `question_date`, `haystack_sessions`, `answer_session_ids`, turn의 `has_answer`를 확인함. 표본은 이력 53세션·정답 근거 1세션이었으며 전체 평균은 아님.

이는 현재의 query–memory 판독 형식으로 옮길 근거가 비교적 분명함. 세션/turn을 나눠 후보로 삼고 근거 참조율·전체 근거 포함률을 계산할 수 있음. `answer`, `has_answer`, `answer_session_ids`는 예측 입력에서 제외함. 공개된 oracle 파일은 정답 근거만 남긴 조건이므로 검색 주 평가에는 전체 `S` 이력을 사용함. variant는 행 순서가 아닌 question ID로 연결함.

답변 채점은 공식 LLM judge 경로가 있으므로 검색 정확도만의 검사와 비용을 구분함. 자체 judge로 바꾸면 별도 조건으로 보고함. 이 자료는 우선 외부 평가로 보존하고, 별도 수집원의 학습이 잘 옮겨가는지 확인하는 용도가 적절함.

### PersonaMem-v2: 새 사용자별 사례를 늘릴 후보

[공식 저장소](https://github.com/bowen-upenn/PersonaMem-v2)는 1,000개의 합성 persona와 다양한 암묵적 선호를 설명함. 실제 text CSV와 [열 설명](https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2/blob/ed956dea41521fc4499acbc63f966e0fd3c053ba/column_descriptions.md)에서 `persona_id`, 이력 경로, `related_conversation_snippet`, `who`, `updated`, `prev_pref` 등을 확인함. 이전 선호·바뀐 선호, 사용자 본인·다른 사람의 선호를 구분하는 사례를 만들 여지가 있음.

다만 **train/val 각각 첫 100행의 persona가 5개 겹쳤음.** 이는 원래 프로토콜의 오류라는 뜻이 아님. 우리 목표가 새 사용자 전이라면 공식 파일 이름을 그대로 독립 사용자 분할로 간주해서는 안 됨. 한 persona의 모든 질문·32k/128k 이력·바꿔쓰기를 같은 분할에 둬야 함. benchmark와의 전체 중복 여부는 이번 prefix 표본으로 확정하지 않았음.

`expanded_persona`, 정답 선호, `related_conversation_snippet`을 모두 현재 관측으로 제공하면 기억 없이도 답을 알 수 있음. 실제로 보였던 대화 이력과 정답/주석을 분리해 수집함. 참고용 구성 예시는 ‘사용자가 정정한 취향과 동료의 취향을 구별해 답하기’이며, 이번에 관측한 모델 효과는 아님.

### Mem2ActBench: 인자 적용이 유망하나 기계적 연결 검사가 선행돼야 함

[공개 소형판](https://github.com/Cantaloupe-M/Mem2ActBench/tree/c935c65be9aa19c930b60657c0c4b2b7ea13a34a/Mem2ACTBench_small)은 실제 파일 기준 **400 QA·429세션**임. L1/L2/L3/L4 분포는 230/111/15/44였음. `source_conversation_ids`, `evolution_chain`, `tool_call.arguments`, `grounding_info`가 있어 직접 복사·해석·종합·충돌 해결을 나눌 수 있음.

실제 `qa_001`은 예전에 정한 목적지·기간으로 날씨를 요청하는 사례이며 정답은 `GetWeatherForecast(location="New York", days=7)`임. 현재 요청의 표면 단어를 맞히는 것에서 과거 정보를 행동 인자로 옮기는 실험으로 연결하기 좋음. 다만 제공 schema가 목표 도구 하나뿐이면 도구 선택 자체는 자명해지므로 인자 정확도를 주 지표로 하거나 도구 후보 은행을 별도로 고정해야 함.

**참조 원문 ID 11개가 소형판 이력에 연결되지 않아 12 QA에 영향이 있었음.** 전체 2,029세션 파일의 `original_conversation_ids`와 turn `source_id`에서도 같은 ID를 찾지 못함. 별도로 `qa_283`의 도구 이름은 정수 `4`, schema 이름은 `4D Dream Dictionary`로 달랐음. 이는 원문 연결·schema 예외이며 해당 질문이 의미적으로 모두 무효라는 확정은 아님. [예외 목록](reports/data/fresh-source-survey-20261109/mem2act-small-join-details.json)과 [전체판 재확인](reports/data/fresh-source-survey-20261109/mem2act-full-join-check.json)을 보존함.

생성된 정답의 `evolution_chain`/`source_text`를 그대로 정상 기억으로 넣어 예외를 숨기지 않음. 원래 기록에서 복구하거나 사전 규칙에 따라 미확정/제외 사유를 남긴 파생 버전을 만들어야 함. QA의 정답 도구 호출은 실제 외부 도구 실행 성공과도 구분함.

### LongMemEval-V2: 원래 관심인 과정 정보·적용 전제와 가장 가까운 내용

[데이터 카드](https://huggingface.co/datasets/xiaowu0162/longmemeval-v2/blob/f152293e235517d504809563c833d7190b8c713b/DATA_CARD.md)는 1,870개 agent 실행 이력과 451질문을 제공함. 직접 읽은 질문은 enterprise 211·web 240개였으며 절차·동적 상태·환경 특이점·잘못된 전제를 다룸. 원문 이력에는 goal·outcome·action·accessibility tree·screenshot 경로가 있음. 자연 사용자 로그가 아니라 벤치 환경에서 생성한 agent 이력임.

그러나 [원본 스키마](https://huggingface.co/datasets/xiaowu0162/longmemeval-v2/blob/f152293e235517d504809563c833d7190b8c713b/SCHEMA.md)의 질문 필드에는 정답 근거의 정확한 state/trajectory ID가 없음. **small의 모든 질문은 domain마다 같은 100개 이력을 공유해 전체 고유 이력은 200개임.** 451질문을 독립 은행 451개로 세거나 질문을 무작위 분할해 새 기억 은행 전이라고 주장하면 안 됨.

우선 학습과 분리된 외부 진단으로 보존하고, 텍스트 근거가 있는 20개 정도의 질문에 supporting trajectory/state를 검토하는 것이 현실적임. 이미지 없는 질문도 이력 이미지가 필요할 수 있으므로 `image=null`만으로 텍스트 완결성을 단정하지 않음. 공식 평가는 과거 이력에 대한 QA이며, 여기서 높은 점수가 새 환경의 실행 성공을 직접 증명하지는 않음.

## 추가 후보의 위치

- **BFCL:** [공식 memory 설명](https://gorilla.cs.berkeley.edu/blogs/16_bfcl_v4_memory.html)은 선행 대화→memory snapshot→후속 조회를 제공함. 고객지원·학생상담·할 일 등으로 과제를 넓힐 수 있음. memory QA와 파일시스템/차량/거래 등의 stateful multi-turn 실행은 다른 평가로 사용함.
- **AppWorld:** [공식 생성 안내](https://github.com/StonyBrookNLP/appworld/blob/42b5bcf3cd334fee33f0c37c02070a9f5807add5/guides/generating_base_database_and_tasks.md)는 task generator·검증 해법·평가를 함께 제공함. 동일 generator의 `_1/_2/_3` 변형을 같은 가족으로 묶음. task마다 DB 초기 상태가 달라 과거의 개인 사실을 다른 task에 그대로 옮기지 않음.
- **LifelongAgentBench:** [공식 실행 코드](https://github.com/caixd-220529/LifelongAgentBench)는 DB·OS·KG 환경과 반복 학습 경로를 제공함. HF 기본 viewer 표본은 SQL형이므로 이를 전체 환경의 rollout 데이터라고 부르지 않음. 도메인별 Parquet/환경을 직접 연결해야 함.
- **SWE 경험:** [SWE-rebench](https://nebius.com/blog/posts/openhands-trajectories-with-qwen3-coder-480b), SWE-smith, SWE-Gym은 명령·응답·실패·수정 경험을 넓히는 후보임. 현재 자료와 repository/issue/PR/수정 가족 중복을 제거함. 기존 실행의 `resolved`는 기억 제공의 이득 라벨이 아님.
- **MemoryAgentBench·EvoMemBench:** [MAB](https://github.com/HUST-AI-HYZ/MemoryAgentBench)는 LongMemEval 등을 포함하고, [EvoMemBench](https://github.com/DSAIL-Memory/EvoMemBench)는 MAB/BFCL/CL-Bench/ALFWorld 등을 묶음. 새 파일·새 record ID만으로 독립 원천이 늘었다고 세지 않음.
- **AMA-Bench:** [공식 자료](https://huggingface.co/datasets/AMA-bench/AMA-bench)의 208 episode는 실행 이력과 QA를 연결해 분석하기 좋음. 저장 성공 여부와 후속 QA 정답은 제공/보류의 실제 효과와 구분함.
- **LoCoMo·RealMem:** [LoCoMo](https://github.com/snap-research/locomo)는 실제 공개 파일 10대화·1,986 QA이고, [RealMem](https://github.com/AvatarMemory/RealMemBench)은 공개 트리에 persona 파일 10개가 있음. 많은 질문·세션을 같은 수의 독립 사용자로 세지 않음. RealMem의 `memory_used`는 생성 과정의 사용 기록으로 완전한 인과 근거 라벨이 아님.
- **SkillsBench·tau-bench:** [SkillsBench](https://huggingface.co/datasets/benchflow/skillsbench)는 skill 내용 삭제·복원에, [tau-bench](https://github.com/sierra-research/tau2-bench)는 정책·상태·고객지원 행동에 활용할 수 있음. 전자는 supplied skill과 실제 과거 경험을, 후자는 release/번역/반복을 구분함. 전체 실행 비용 때문에 이번 첫 수집보다 뒤에 두는 것이 적절함.

접근 상태도 구분함. 이번 `cx-cmu/agent_trajectories`는 Hub 메타데이터만 접근됐고 gated=auto·viewer 401이었음. 약관 동의나 gated 원문 다운로드는 하지 않음. viewer 실패를 데이터 부재로 단정하지 않았으며, 필요한 후보는 고정된 native 파일로 구조를 확인함.

## 어떻게 수집할까

1. **출처와 부모부터 고정함.** dataset/revision, 원문 파일·내용 해시, persona/episode/task generator/원본 conversation ID, 시점을 보존함. QA·긴 이력 버전·반복·바꿔쓰기를 부모 가족으로 연결한 뒤 분할함.
2. **현재 입력·은행·주석을 분리함.** query/current observation와 과거 turn/state를 입력으로 만들고, 근거 ID·정답 답변/호출·선호·자동 요약의 provenance를 별도 필드에 둠. 특정 QA의 정답 근거만 뽑아 만든 은행은 oracle 진단임을 명시함.
3. **결과 생성 전에 데이터 품질을 확인함.** 참조 ID join, 시간 경계, 도구 schema, 타입, 답의 존재를 검토함. 연결 예외·출처 부족은 모델 실패와 분리하고 원문을 고쳐 덮어쓰지 않음.
4. **가족 수를 우선 늘림.** PersonaMem-v2의 30–50 persona에서 주체·갱신·암묵 선호를 골라 수집하는 것을 시작 규모로 제안함. 충분한 통계 표본 수라는 보장은 아님. 같은 문장 변형 수는 별도로 기록함.
5. **외부 평가는 보존함.** LongMemEval은 우선 학습과 분리해 고정 모델의 검색/답변을 평가함. 일부를 개발에 썼다면 그 가족은 확인 평가에서 분리하고 공개 test 전체 성적과 구분함. LongMemEval이 포함된 MAB/EvoMem 파생 자료로 학습하는 누출도 검사함.
6. **행동 적용을 작은 단위로 잇음.** 정제한 Mem2Act의 20–40개 원본 가족에서 인자 grounding 대조를 준비하고, 이후 BFCL multi-turn/AppWorld에서 실제 실행·최종 상태를 확인함. 사례는 원천/난도 규칙으로 고르며 효과가 큰 사례만 남기지 않음.

현재 모델의 지시문은 여행의 사람·날짜·항목에 맞춰져 있음. 외부 자료에는 query–memory 공통 입력 어댑터가 필요함. 가중치를 고정하고 지시문만 바꾼 조건도 ‘원래 설정 그대로’와 구분하고, 새 자료 학습을 추가했다면 적응 결과를 따로 보고함. 긴 세션은 근거가 보존되는 turn/state 단위로 분할하며 조용한 절단으로 정답 근거를 없애지 않음.

## 어떤 라벨을 학습할까

| 라벨 | 얻는 경로 | 의미 |
| --- | --- | --- |
| 참조/근거 | 원본 evidence ID·grounding 주석 및 검토 | 이 기억이 정답을 뒷받침하는가 |
| 적용 조건 | 시점·주체·현재 관측·버전/환경 대조 | 지금 사용 가능한 근거인가 |
| 실제 효용 | 고정 worker의 같은 상태에 제공/보류 실행 | 성공·악화·비용이 어떻게 바뀌었는가 |

이번 후보에서 **개별 기억의 제공/보류 인과 효과가 완성 라벨로 제공됨을 확인한 자료는 없음.** 앞의 두 라벨을 마지막 라벨로 바꿔 쓰지 않음. 후속 수집은 무기억·기본 검색·학습 검색·근거 제공 상한을 같은 예산에서 비교하고, 개선·악화·보류·비용을 별도로 저장함. 자료별 정답 정의도 유지함. 예컨대 잘못된 전제를 지적해야 하는 질문의 단순 UNKNOWN을 자동 정답으로 처리하지 않음.

## 조사 근거·재현 범위

- [Hub 조사](reports/data/fresh-source-survey-20261109/huggingface-probe.json): 18개 저장소의 revision·선언 license·파일·viewer 상태. viewer가 Hub의 동일 revision이라고 보장하지 않음.
- [고정 원문 확인](reports/data/fresh-source-survey-20261109/native-probe.json): 15개 파일의 전체/부분 읽기 구분·내용 해시·구조. 대형 이력은 prefix 표본이며 전체 내용 검증이 아님.
- [가족·연결 점검](reports/data/fresh-source-survey-20261109/candidate-grouping-audit.json): Mem2Act 소형 파일 전체, PersonaMem-v2 split별 앞 100행, LongMemEval S의 첫 완전 레코드.
- [공식 문서 확인](reports/data/fresh-source-survey-20261109/official-source-audit.json): 공식 저장소·commit/blob 식별자와 문서 경로.

입력 데이터 재배포 조건은 각 고정 원본을 따름. 예를 들어 PersonaMem-v2 Hub는 CC BY 4.0, LoCoMo 실제 LICENSE는 CC BY-NC 4.0임. RealMem README badge와 LICENSE 내용의 표기가 달라 실제 LICENSE를 함께 기록함. 메타데이터가 비어 있는 것을 사용 불가로 단정하지 않음.

이번 작업에서는 공개 자료와 schema를 조사하고 메타데이터·해시·예외 목록만 저장함. worker/GPU·유료 모델 API·새 학습은 실행하지 않음. 초기 수집 우선순위는 **근거가 있는 외부 평가 → 사용자 가족의 다양화 → 정제한 도구 인자 사례 → 실제 환경 실행 검증**임.
