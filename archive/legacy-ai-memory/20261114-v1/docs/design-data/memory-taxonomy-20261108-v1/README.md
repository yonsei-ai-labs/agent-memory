# 메모리 내용·선택 문제의 분류 초안

2026-11-08. [수집·증강 설계 노트](../../ai-memory-data-collection-and-taxonomy-2026-11-08.md)의 검토용 부속 자료다. 기존 발견 자료를 다시 분류했으며 새 원본 경험·효용 라벨·학습 행은 만들지 않았다.

- [분류 정의](taxonomy.json): 기억 내용 6종, 선택 문제 8종과 적용 규칙.
- [기억 내용 표](memory-tags.csv): 기존 카드 12개에서 주로 재사용하려는 내용.
- [선택 문제 표](case-tags.csv): 기존 19쌍에 붙인 겹칠 수 있는 문제 유형.
- [범위와 연결 해시](coverage.json): 원본 12개 카드·18개 현재 문제·19쌍과 대응하는 개수 및 참조 파일 해시.

이는 한 에이전트가 원래 카드·문제·코드 검토 설명을 읽고 붙인 잠정 태그다. 독립 검토나 새 실행 검증이 아니다. 원본 [관계·근거 자료](../memory-evidence-discovery-20261108-v1/README.md)의 주석, 과거 기억 본문, 분할·효용 상태를 바꾸지 않았다.

기억의 주된 내용을 하나씩 택하면 M2(입력·출력·API 계약) 10개, M4(수정 절차·순서) 2개다. 예를 들어 numpydoc 카드는 여러 단계를 함께 수정하는 절차가 중심이지만, 현재 문제에서는 진단의 힌트로 쓸 가능성도 있다. 종류는 배타적인 자연 법칙이 아니며 이 표의 일차 분류 선택이다. 모든 카드에 검사 결과가 있다는 이유로 모두를 M5 중심 기억이라고 세지도 않았다.

선택 문제 태그는 S1 14쌍, S2 3쌍, S3 7쌍, S4 3쌍, S5 5쌍이다. 중복 태그가 있으므로 합계는 19가 아니다. S3은 과거 처리의 일부가 다른 경로에 이미 구현된 경우와 본문에 단서가 있는 경우를 포함한다. 일곱 문제 전체가 해결됐다는 뜻이 아니다.

S6–S8은 이번 19쌍에서 검증한 자료가 없다는 뜻으로 0을 기록한다. 메모리 요약 손실·후보 충돌·검증 비용 문제가 없다는 결과가 아니다. 특히 현재는 실제 작업자 실행이나 복원 가능한 선택 시점 snapshot이 없다.

태그 정의와 원문 ID의 대응, 해시, 개수를 간단히 다시 확인할 수 있다. 저장소 루트에서 실행하며 표준 라이브러리만 사용한다.

```bash
python3 - <<'PY'
import csv, hashlib, json
from collections import Counter
from pathlib import Path
root = Path("docs/design-data/memory-taxonomy-20261108-v1")
spec = json.loads((root / "taxonomy.json").read_text())
report = json.loads((root / "coverage.json").read_text())
for item in report["references"]:
    assert hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() == item["sha256"]
cards = {r["id"]: r for r in map(json.loads, Path(spec["source_cards"]).read_text().splitlines())}
cases = {r["id"]: r for r in map(json.loads, Path(spec["source_cases"]).read_text().splitlines())}
m = list(csv.DictReader((root / "memory-tags.csv").open()))
c = list(csv.DictReader((root / "case-tags.csv").open()))
assert len(m) == len(cards) and {r["memory_id"] for r in m} == set(cards)
assert len(c) == len(cases) and {r["case_id"] for r in c} == set(cases)
mt = Counter(r["primary_content"] for r in m)
ct = Counter(t for r in c for t in r["selection_challenges"].split("|"))
assert set(mt) <= {r["id"] for r in spec["memory_content"]}
assert set(ct) <= {r["id"] for r in spec["selection_challenges"]}
assert {r["id"]: mt[r["id"]] for r in spec["memory_content"]} == report["primary_content_counts"]
assert {r["id"]: ct[r["id"]] for r in spec["selection_challenges"]} == report["selection_challenge_counts"]
assert len({r["target_instance_id"] for r in cases.values()}) == report["current_problems"]
assert all(r["observed_utility"] is None and not r["learner_export_allowed"] for r in cases.values())
assert all(r["observed_utility"] is None and not r["learner_export_allowed"] for r in cards.values())
assert spec["observed_utility"] is None and not spec["learner_export_allowed"]
print("review-only:", len(cards), "memories;", len(cases), "pairs; utility labels: 0")
PY
```

이 검사는 태그의 의미가 맞는지 보증하지 않는다. 독립 재주석과 이견 검토는 후속 작업이다.
