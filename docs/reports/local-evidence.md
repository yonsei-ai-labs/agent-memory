# 보고서에서 인용한 로컬 원본

실험별 공유 분석 파일은 Git에서 바로 열 수 있다. 아래에는 보고서가 직접 인용한 상담 원문·요청·완료 장부·생성 보고서의 로컬 위치를 모았다. 경로는 저장소 루트를 기준으로 한다.

`runs/`에는 실험의 원본이, `outputs/`에는 그 원본에서 생성한 비교 보고서가 있다. 같은 파일인지 확인하려면 `sha256sum 경로`로 대조한다. `blobs/sha256/` 아래 파일은 파일명 자체가 SHA-256이다. 해당 경로가 공유 분석에 등장하면 그 행도 연결했다.

## 소매 테스트 40과제의 두 회차 비교

아래는 상담 대화 전체가 들어 있는 원본이다. 공유 분석에는 같은 실행의 메모·업무 변경·채점 문항이 연결돼 있다. [40과제 보고서](20261119-retail-full-forms.md)

**60번 절차 · 이어버드의 방수 조건을 정정한 고객** · [공유 분석의 해당 상담](../../artifacts/analyses/20261119-retail-full-cells.json#L21457)

`runs/tau2-retail-full-forms-01/task-60/solve1/jobs/0002/blobs/sha256/45060353d3eec7b62433492e1df0db6542b3406ef4063a2c89cda82bfba728d0`

**60번 일기 · 빈 작성 뒤 세 상품을 제시한 상담** · [공유 분석의 해당 상담](../../artifacts/analyses/20261119-retail-full-cells.json#L21841)

`runs/tau2-retail-full-forms-01/task-60/solve1/jobs/0005/blobs/sha256/e48c4af341a6e79e8f7525f99026389c2f3d3f1619ec188c863cd20a77d99f40`

**60번 무기억 · 첫 번째 방수 제품을 승인한 고객** · [공유 분석의 해당 상담](../../artifacts/analyses/20261119-retail-full-cells.json#L21329)

`runs/tau2-retail-full-forms-01/task-60/solve1/jobs/0001/blobs/sha256/3ad357df92084be1e3a43fecf137968cffcf937d72ec447b78ee2cc13c26d0c9`

**51번 절차 · 반품 전 상담원 이관** · [공유 분석의 해당 상담](../../artifacts/analyses/20261119-retail-full-cells.json#L17119)

`runs/tau2-retail-full-forms-01/task-51/solve1/jobs/0003/blobs/sha256/e17dd06c0e72f6bf6eb21bf386a4d4a9fc45e00efaa2ae53b2a3d98b028d89bb`

**51번 무기억 · 고객 확인 뒤 카메라 반품** · [공유 분석의 해당 상담](../../artifacts/analyses/20261119-retail-full-cells.json#L16893)

`runs/tau2-retail-full-forms-01/task-51/solve1/jobs/0001/blobs/sha256/98d61ba723931071a77266b65bf09516cb102b3ed94f492e439972400bc97821`

**68번 일기 · 고객이 마지막에 고른 주문과 안내 금액** · [공유 분석의 해당 상담](../../artifacts/analyses/20261119-retail-full-cells.json#L25751)

`runs/tau2-retail-full-forms-01/task-68/solve1/jobs/0002/blobs/sha256/453e4f5c36dea43d3aa497095d7f45526368fc1070dbbd00acb4f79baf403aeb`

**49번 일기 · 같은 주문 안에서 고른 교환 상품** · [공유 분석의 해당 상담](../../artifacts/analyses/20261119-retail-full-cells.json#L16269)

`runs/tau2-retail-full-forms-01/task-49/solve1/jobs/0004/blobs/sha256/e7bb8c702a40b89943574f7f34c1131cf3349a4ac8e73d7c753bd1644ec0fda2`

## 소매 재확인 지침의 두 라운드

[개발 고객의 작성·노출·채점](../../artifacts/analyses/20261119-retail-recheck-development-results.json) · [확인 고객의 작성·노출·채점](../../artifacts/analyses/20261119-retail-recheck-confirmation-results.json) · [두 라운드 집계](../../artifacts/analyses/20261119-retail-recheck-two-round-results.json)

<a id="source-3fec9f6db831"></a>

**49번 이어폰 교환 · 보완 절차 · 첫 반복** · [분석에 기록된 위치](../../artifacts/analyses/20261119-retail-recheck-confirmation-results.json#L19046)

`runs/tau2-retail-recheck-two-rounds-01/confirmation/task-49/solve1/jobs/0001/blobs/sha256/ed364275d33664d7c920efaa818f44eb6c4e3fef317234f7a6a61122af8b949f`

<a id="source-15281e5f6642"></a>

**49번 이어폰 교환 · 보완 절차 · 둘째 반복** · [분석에 기록된 위치](../../artifacts/analyses/20261119-retail-recheck-confirmation-results.json#L20494)

`runs/tau2-retail-recheck-two-rounds-01/confirmation/task-49/solve2/jobs/0001/blobs/sha256/c6cefae6bbe95633a510ee43650c3ed578a457593031b19ffa853398b8c99868`

<a id="source-92e2f4a0132b"></a>

**74번 노트북 변경 · 보완 교훈 · 첫 반복** · [분석에 기록된 위치](../../artifacts/analyses/20261119-retail-recheck-confirmation-results.json#L7075)

`runs/tau2-retail-recheck-two-rounds-01/confirmation/task-74/solve1/jobs/0007/blobs/sha256/0fb7ae7aa0e07add75069344e9f3baaf003ff497891ca5c8a3f7c90c841069d4`

<a id="source-b2e5f74945a5"></a>

**74번 노트북 변경 · 보완 교훈 · 둘째 반복** · [분석에 기록된 위치](../../artifacts/analyses/20261119-retail-recheck-confirmation-results.json#L8527)

`runs/tau2-retail-recheck-two-rounds-01/confirmation/task-74/solve2/jobs/0006/blobs/sha256/69eccf63aee0e9655864d3e9f8a9159ccc27dad851e6689b34b3fdc2e909767c`

<a id="source-bfd0567bf14b"></a>

**66번 주문 취소 · 보완 단순 정보 · 첫 반복** · [분석에 기록된 위치](../../artifacts/analyses/20261119-retail-recheck-development-results.json#L2907)

`runs/tau2-retail-recheck-two-rounds-01/development/task-66/solve1/jobs/0002/blobs/sha256/36d1bb7045beeb3b266cb65bdb14702bdb480cabfc7510543177bd60cdb8b074`

<a id="source-395d0109fdcb"></a>

**66번 주문 취소 · 기존 단순 정보 · 첫 반복** · [분석에 기록된 위치](../../artifacts/analyses/20261119-retail-recheck-development-results.json#L3865)

`runs/tau2-retail-recheck-two-rounds-01/development/task-66/solve1/jobs/0010/blobs/sha256/01e9973b5780d7bd6789887140792d5c284e2cf8001198cc53b167a2ce9b2171`

## 같은 고객의 다음 항공 업무

[메모·전체 상담·고정 대화 결과](../../artifacts/analyses/20261119-same-customer-transfer-results.json)

<a id="source-d9dd476d7d36"></a>

**고정 대화 완료 기록**

`runs/tau2-same-customer-decisions-01/completion.json`

SHA-256 `a66bdadc8caf3b6463c3ea6ae38811f01c78583b00bb7577598152b7ed1c25e6`

<a id="source-9adf5f252e49"></a>

**전체 상담 완료 기록**

`runs/tau2-same-customer-transfer-01/completion.json`

SHA-256 `0dcbc59a6284183cc4a46eb52107802dffd17b9db73dd40f11d4aa0a59709879`

<a id="source-1d37f79f9f49"></a>

**교훈을 받은 상담의 종료** · [분석에 기록된 위치](../../artifacts/analyses/20261119-same-customer-transfer-results.json#L2284)

`runs/tau2-same-customer-transfer-01/task-22-wave-1/jobs/0004/blobs/sha256/e44a711a7d5585a1eccd8f56504a1f9f7fcccd22e0016d053dbb09a0b3af5c74`

<a id="source-974b5be2cbb9"></a>

**무기억의 변경 거절 원문** · [분석에 기록된 위치](../../artifacts/analyses/20261119-same-customer-transfer-results.json#L4034)

`runs/tau2-same-customer-transfer-01/task-22-wave-1/jobs/0010/blobs/sha256/6f8da82fa9867bd0d213f30c45c5f53012084067cee6ebb1cb61ba3f39565258`

<a id="source-59d642f25232"></a>

**절차 조건의 원래 도구 인수** · [분석에 기록된 위치](../../artifacts/analyses/20261119-same-customer-transfer-results.json#L7431)

`runs/tau2-same-customer-transfer-01/task-22-wave-2/jobs/0005/blobs/sha256/3d93f56b22363e16a7595ed0c51b1e2013b1bf785d13d1d1c5dc57708b334405`

<a id="source-0eb746c27e3f"></a>

**빈 메모의 날짜 해석** · [분석에 기록된 위치](../../artifacts/analyses/20261119-same-customer-transfer-results.json#L9493)

`runs/tau2-same-customer-transfer-01/task-22-wave-2/jobs/0012/blobs/sha256/8eed4bc4be68069a0f9527ca4ade23da111330802ab93a270740967ea209484a`

## 다른 고객에게 옮긴 항공 기억

[고객·기억별 관측](../../artifacts/analyses/20261119-bank-transfer-results.json) · [환불 수단 재생](../../artifacts/analyses/20261119-bank-transfer-refund-replay.json)

<a id="source-db2da3f60a34"></a>

**실행 완료 장부**

`runs/tau2-bank-transfer-01/completion.json`

SHA-256 `3c9c5ae9db8a468002f5e6b1b9a16fa9ef7833df041169cc88fb4b2c5ec26206`

<a id="source-521fe78fe2d2"></a>

**한 카드로 변경한 원문** · [분석에 기록된 위치](../../artifacts/analyses/20261119-bank-transfer-results.json#L4316)

`runs/tau2-bank-transfer-01/task-18-wave-1/jobs/0005/blobs/sha256/1f037632e0a09ffb3358d226b90f5a9cce1a7d235a705194164ba83fd2b5cf15`

<a id="source-31462c89190a"></a>

**이전 절차가 담긴 실제 입력**

`runs/tau2-bank-transfer-01/task-18-wave-1/jobs/0005/blobs/sha256/e6073ec39e51f22b694894d82f2a5b761e912b093225cb3bd6ce102f4e02e595`

<a id="source-272100f5f6f4"></a>

**원래 결제 수단을 알고도 한 카드를 다시 요구한 대화** · [분석에 기록된 위치](../../artifacts/analyses/20261119-bank-transfer-results.json#L9901)

`runs/tau2-bank-transfer-01/task-18-wave-2/jobs/0003/blobs/sha256/d94bfb0f616f4443d47df38c5da8ddfb80ee3190dd5893b96dafd1bb687fd0b5`

<a id="source-5470fde337e9"></a>

**네 건만 변경한 무기억 실행** · [분석에 기록된 위치](../../artifacts/analyses/20261119-bank-transfer-results.json#L10323)

`runs/tau2-bank-transfer-01/task-18-wave-2/jobs/0004/blobs/sha256/19ef2472194ed37d06a6f2ce6704f29ce8135bad5f6ece73f6e88857c21cab3d`

<a id="source-b5aa2094f8f9"></a>

**같은 묶음에서 성공한 빈 메모** · [분석에 기록된 위치](../../artifacts/analyses/20261119-bank-transfer-results.json#L11161)

`runs/tau2-bank-transfer-01/task-18-wave-2/jobs/0006/blobs/sha256/ffe6513f187ae29f07e7cbc60ec3db4cd62bc6a2ea3ae920ab2e2253fadd90fd`

<a id="source-47940651c12e"></a>

**다섯 건 승인 뒤 변경 없이 인계한 대화** · [분석에 기록된 위치](../../artifacts/analyses/20261119-bank-transfer-results.json#L12906)

`runs/tau2-bank-transfer-01/task-18-wave-2/jobs/0010/blobs/sha256/98c2a5eebefaacc155768536a6345ef4f23bf2b02772e599d6ffd2068c1268a7`

<a id="source-648009af3436"></a>

**인계 요약에만 근거를 남긴 무기억 대화** · [분석에 기록된 위치](../../artifacts/analyses/20261119-bank-transfer-results.json#L1354)

`runs/tau2-bank-transfer-01/task-26-wave-1/jobs/0001/blobs/sha256/c75a1ac0a8f9670f27080342d9f1041090defa00f782b2d24f4cd5d44e8ccd02`

<a id="source-4fade5523243"></a>

**잘못 취소한 대화와 도구 호출** · [분석에 기록된 위치](../../artifacts/analyses/20261119-bank-transfer-results.json#L8211)

`runs/tau2-bank-transfer-01/task-26-wave-2/jobs/0006/blobs/sha256/2ac476a03e31754f9328339d31e708322cffecef8198f7e52b196dffce64b6da`

## 같은 기억을 제공한 반복 상담

[공식 성적과 행동·노출 근거](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json)

<a id="source-18958f3941e6"></a>

**실행 완료 기록**

`runs/tau2-fixed-bank-repeats-01/completion.json`

SHA-256 `542bfb0cf55e8f420bc8c317a40819ebdaca9dd054c55f34550a144d465c57d8`

<a id="source-476e6819f311"></a>

**절차 원문** · [분석에 기록된 위치](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json#L3399)

`runs/tau2-fixed-bank-repeats-01/task-03-wave-1/jobs/0001/blobs/sha256/46177e415f22b214c2a17ae198fa7f8574bb62112104fca1823f20feeb7dc391`

<a id="source-3e6d8ca7ce65"></a>

**빈 일기 원문** · [분석에 기록된 위치](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json#L3826)

`runs/tau2-fixed-bank-repeats-01/task-03-wave-1/jobs/0007/blobs/sha256/499a4701673337c13525f098149fda0601cdb36386e8e3c41548d8abfbdb8afe`

<a id="source-a1ffa21fe208"></a>

**무기억 원문** · [분석에 기록된 위치](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json#L11933)

`runs/tau2-fixed-bank-repeats-01/task-03-wave-2/jobs/0004/blobs/sha256/d02a2dd6f49eec07f3ddf18da395d15637cb8c30b814e7bf93b13ba11a80251b`

<a id="source-4a189b6b743e"></a>

**절차 실행 원문** · [분석에 기록된 위치](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json#L4243)

`runs/tau2-fixed-bank-repeats-01/task-14-wave-1/jobs/0001/blobs/sha256/fe95841c2f43f8e008373bd9ba3a2c142e7a7289d15d2cf25389ae9c9c20f3cb`

<a id="source-4545951724c1"></a>

**같은 묶음의 무기억 기록** · [분석에 기록된 위치](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json#L4722)

`runs/tau2-fixed-bank-repeats-01/task-14-wave-1/jobs/0003/log.jsonl`

SHA-256 `c1d4699ef3f5071679f87ae58ac61270cefd7f46d13e2ced395da9bbb40e36ce`

<a id="source-faaa2fe16a82"></a>

**201단계의 원래 대화·도구 궤적** · [분석에 기록된 위치](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json#L7278)

`runs/tau2-fixed-bank-repeats-01/task-17-wave-1/jobs/0002/blobs/sha256/787cbaa2519c90bba906adec06933612180a3dd0fe9924c90a03097b6f632519`

<a id="source-19985ffa3884"></a>

**빈 메모 기록** · [분석에 기록된 위치](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json#L8497)

`runs/tau2-fixed-bank-repeats-01/task-17-wave-1/jobs/0008/log.jsonl`

SHA-256 `f81b0b56f6cff80a7959674d03324f0fd862fa6075a55a594e9637b31b6baec0`

<a id="source-46586e8c76e6"></a>

**무기억 원문** · [분석에 기록된 위치](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json#L8900)

`runs/tau2-fixed-bank-repeats-01/task-17-wave-1/jobs/0010/blobs/sha256/f2e81c4e0cede3f8574e7f722936f66545cd5679b8d8b4b51cce64ab92236416`

<a id="source-01850b05ccb4"></a>

**일기 원문** · [분석에 기록된 위치](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json#L9042)

`runs/tau2-fixed-bank-repeats-01/task-17-wave-1/jobs/0011/blobs/sha256/e9218de74c1198013c3573dab815c2610589c2ddc6b29479795abb61dca10839`

<a id="source-b84128c23b02"></a>

**31단계의 공식 채점 원문** · [분석에 기록된 위치](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json#L17249)

`runs/tau2-fixed-bank-repeats-01/task-17-wave-2/jobs/0011/blobs/sha256/ceed0fb42cc26fe9bd961091c79db9fb05eff828923b7f17cfc5a8bae8794c77`

<a id="source-eebe6d5b8312"></a>

**첫 번째 수행의 실제 입력**

`runs/tau2-fixed-bank-repeats-01/task-42-wave-1/jobs/0005/blobs/sha256/d0478a71c5c2540801bde51a3829855be6bba7353b9a91ebdd013115ae5e2d7e`

<a id="source-12f6eb651826"></a>

**빈 메모 원문** · [분석에 기록된 위치](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json#L17759)

`runs/tau2-fixed-bank-repeats-01/task-42-wave-2/jobs/0002/blobs/sha256/9538985663ca2d1224f9bb79716593c9a9f24e50d901dfe4bd990d68c02b1f35`

<a id="source-cc3d7f87991f"></a>

**일기 실행 원문** · [분석에 기록된 위치](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json#L17866)

`runs/tau2-fixed-bank-repeats-01/task-42-wave-2/jobs/0003/blobs/sha256/b9173bf5fe5805de27c188620401875279d0f0f3290973e123eb26570e6a0330`

<a id="source-aefe7edb5f21"></a>

**무기억 원문** · [분석에 기록된 위치](../../artifacts/analyses/20261119-fixed-bank-repeats-results.json#L17987)

`runs/tau2-fixed-bank-repeats-01/task-42-wave-2/jobs/0004/blobs/sha256/346e984d10bf124b1f7a35afcacd39d365f7b51495bc0b7d47e696c95a42d800`

<a id="source-cd01520edbf6"></a>

**두 번째 수행의 실제 입력**

`runs/tau2-fixed-bank-repeats-01/task-42-wave-2/jobs/0005/blobs/sha256/d8f30afbefd7fe6f6ed1c5c943f7ffe9beaf4f4b68d280e59e74db2ee635d730`

## 기억 형식과 귀국 날짜 판단

[네 형식의 판정·요청 근거](../../artifacts/analyses/20261118-format-content-decisions.json) · [귀국 날짜별 첫 행동](../../artifacts/analyses/20261118-return-date-calibration.json) · [실제 프로필 조회 확인](../../artifacts/analyses/20261119-return-date-profile-lookup.json)

<a id="source-110ebf4e7a5a"></a>

**실행 완료 기록**

`runs/tau2-format-content-decisions-01/completion.json`

SHA-256 `6e75d6ba514b6c2925d0435e28c3fe0fc4f42de1d644ca6f84e2e24683369f7c`

<a id="source-9f989272436c"></a>

**실제 투입한 여덟 작성물 전체**

`runs/tau2-format-content-decisions-01/control.json`

SHA-256 `c1303d38a59418220c51f4d1d89037f9555872390c852e90cc4f7ce559ef79b9`

<a id="source-7bcd23170cd8"></a>

**완료 기록**

`runs/tau2-return-date-calibration-01/completion.json`

SHA-256 `774f0b682a64dc6320265ae01cbbf8aefb984d9efd3463eb2e22a873800f6f68`

<a id="source-2e4d128fdf08"></a>

**실행 직전 배정과 해시**

`runs/tau2-return-date-calibration-01/control.json`

SHA-256 `f20706fb369977b970b114978d2f776468cbf62de1e0ac1eda53430db552fe65`

## 항공 신규 고객의 원천·2회차·조건별 비교

[공식 성적·득실](../../artifacts/analyses/20261118-new-customer-forms-final/results.json) · [조건별 행동](../../artifacts/analyses/20261118-new-customer-forms-final/behavior.json) · [빈 메모와 실제 전송 입력 점검](../../artifacts/analyses/20261118-new-customer-design-audit.json)

<a id="source-292df62502f0"></a>

**32번 절차 조건 대화**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/job-58.md`

문서 내 위치 `#m-8`

SHA-256 `99f3118a50504b562d251eb018e214b8e790990899b6298a32dd9f2734cdc67c`

<a id="source-5a2c37810b42"></a>

**일기 원문·예약 도구**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/job-69.md`

문서 내 위치 `#m-46`

SHA-256 `9439855d73a165669427a7fc26d081a13f1a00fbc1f10dc0c062180c70a52933`

<a id="source-857c830c052f"></a>

**32번 일기 작성 원문**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/job-7.md`

SHA-256 `46250057ce14b7decd8108f1d5855f407573d0371770173cc14782199c8a78d4`

<a id="source-a207d9ea586b"></a>

**기존 예약을 유지한 무기억 수행**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/job-74.md`

문서 내 위치 `#m-23`

SHA-256 `879535569d1291214c0897e42b1105b2b8391b54bb4c9e4a992af13e8efff131`

<a id="source-f585e70a7f89"></a>

**절차와 빈 메모**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/pair-105-106.md`

SHA-256 `05bc90eadc60d5c27f88e2331fe55aa0452c6bff8c708998a396f022ee14a0b9`

<a id="source-b546e77ff47c"></a>

**두 번째 반복의 절차와 무기억**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/pair-105-107.md`

SHA-256 `bbb0556209910d5202181c190a60400f40b5312715d7a8dcf56e355f760aeed2`

<a id="source-a248b4d7ffa6"></a>

**절차와 무기억**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/pair-68-71.md`

SHA-256 `fa2b02b3be86ac6b194060241f7b9bf1c9c806f0b1c9cf5eb3e5d51675e7ce10`

<a id="source-032c338683f1"></a>

**단순 정보와 빈 메모**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/pair-70-67.md`

SHA-256 `d1e31997b40a06385c7a45f6a7f55cdf069e5ed845622a7dd46ce3cda936057e`

<a id="source-398cd4b90329"></a>

**교훈과 빈 메모**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/pair-80-83.md`

SHA-256 `46b7eee8e100675b91b0e093e48903592a356b7fc731f04498e0c120f459db31`

<a id="source-524852bbf9ee"></a>

**교훈과 무기억**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/pair-80-84.md`

SHA-256 `c9580c672ba0581a7452a190f2d861ee599a5049faafb09a66441712c26837e8`

<a id="source-dc3f1d0ed16e"></a>

**동일한 첫 요청의 두 실행**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/pair-85-89.md`

SHA-256 `1d70799f612f15bd71bffdef8c63c6f09196e8ff4f4a75e2edaa7539ca785989`

<a id="source-eab9b763d69e"></a>

**두 빈 은행의 실제 대화와 변경 기록**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/pair-87-89.md`

SHA-256 `fd29ede79dd1861c98b69bed9ac8c1c41cd9992df625fd7a181751e628949971`

<a id="source-3759ab11c114"></a>

**단순 정보와 빈 메모**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/pair-91-92.md`

SHA-256 `25f9f2232e8b337caa2716ae11ff76a0688df83868f688ec5d2a6705b6c5aa39`

<a id="source-f40962b3b3b1"></a>

**단순 정보와 무기억**

`outputs/new-customer-forms-20261118-01/report-combined/evidence/pair-91-94.md`

SHA-256 `08e300ea025f4e7fccda21812705ba1cd21bc6c90ecfcdd5774dcb9a09a30a76`

<a id="source-67ba9919269b"></a>

**전체 15가지 짝 비교와 180개 비교 원문**

`outputs/new-customer-forms-20261118-01/report-combined/index.md`

SHA-256 `bf0120d1c9655df0f702064b05183061889a3af80ca34c8a236e7dd715429029`

<a id="source-40ef1c1733d4"></a>

**원래 예약 직전 요청**

`runs/tau2-new-customer-forms-continuation-01/jobs/0107/blobs/sha256/1221ff7a2b2fad9bcedf548753d37f4d8ff36895417015466647fd528b3ffe27`

<a id="source-df65fc3559f4"></a>

**원래 첫 요청**

`runs/tau2-new-customer-forms-continuation-01/jobs/0107/blobs/sha256/8a55511ec877215d5d1cb5cfd2e72c916deeebbef8a6bf8602ecabbf495aa0d0`

<a id="source-9041fb8fb6b7"></a>

**기존 대화에 들어간 실제 도구 응답**

`runs/tau2-new-customer-forms-continuation-01/jobs/0107/blobs/sha256/cecfad9ba04da7d50001e77e02d52c04336b9ce140f2cec6aacc63bacaddd8d5`

<a id="source-0a18910862d7"></a>

**1회차 공개 예약 기록**

`runs/tau2-new-customer-source-01/jobs/0006/blobs/sha256/6ef70af1baee8e40826338e4eb85b62e49c2d9257f5e157476082454d9f43c57`

<a id="source-03303c55222b"></a>

**1회차 원문과 정정 근거**

`runs/tau2-new-customer-source-01/jobs/0006/blobs/sha256/df2344f2d6d6fe0e4dce6872e0c8eb317cc1bb883760e950135489cadb56c91d`

## 항공 단순 정보형의 작성·조회 지침 비교

[비교 결과](../../artifacts/pilots/20261118-fact-recheck-results.json) · [작성 지침의 공식 재채점](../../artifacts/analyses/20261118-fact-recheck-writing-native-regrade.json) · [조회 지침의 공식 재채점](../../artifacts/analyses/20261118-fact-recheck-delivery-native-regrade.json)

<a id="source-037ac1347dfc"></a>

**과거 네 형식 비교**

`outputs/prompt-comparison-20261118-01/reports/archive/index.md`

SHA-256 `2e1b681b9a2d080eecc574716c96ad7a18cbdd09fbf1c7bcde459d185c759831`

<a id="source-dba4688ec962"></a>

**빈 은행 두 실행의 원문**

`outputs/prompt-comparison-20261118-01/reports/delivery/evidence/pair-11-12.md`

SHA-256 `76aa3cd067bc15b5db257e60a52f07c3af5af5e18056d172dc92e4bae5cf2b5b`

<a id="source-64a0f109caec"></a>

**조회 비교**

`outputs/prompt-comparison-20261118-01/reports/delivery/index.md`

SHA-256 `902da4d2bccc00dcfeb10713072c87326da9630bd560be960f6fae181592d5f4`

<a id="source-4ae032d5f9c6"></a>

**연도 확인과 예약 행동 원문**

`outputs/prompt-comparison-20261118-01/reports/writing/evidence/pair-37-38.md`

SHA-256 `4e10ed307bbeac881307ede2428ed1e5840d40712d86f8aea83b09e711b0d31e`

<a id="source-ce585f69dfef"></a>

**두 실행의 채점 근거와 원문**

`outputs/prompt-comparison-20261118-01/reports/writing/evidence/pair-42-43.md`

SHA-256 `28a5901d223751ee1c2f002827c28d09a43d40dc17d47efb9250c09bb116a8bf`

<a id="source-eab5ba269490"></a>

**작성 비교**

`outputs/prompt-comparison-20261118-01/reports/writing/index.md`

SHA-256 `398513cdc61fbf9d35531ca5b30ff85e262c4377cba527584513bde3f8dcde21`
