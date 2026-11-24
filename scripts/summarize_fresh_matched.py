"""Render a Korean results table and cross-check page totals against the sealed allocations."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.recording.experience import _source
from agent_memory_eval.recording.journal import safe_path
from scripts.run_fresh_matched import FORMS, MODES, STAGES, jobs_for, read, source_from


DOMAINS = {"airline": "항공", "retail": "소매"}
FORM_NAMES = dict(zip(FORMS, ("정보", "경험 경과", "절차", "교훈", "자유"), strict=True))
MODE_NAMES = {"preload": "자동 제공", "on_demand": "자율 조회"}


def verify_writer_input(folder, row, source, registration):
    """Match the first recorded writing request to its fresh public consultation."""
    result = row["result"]
    with _source(safe_path(folder, row["directory"]), result["log_sha256"]) as (scan, blobs, _, _):
        first = next((e for e in scan["events"].values() if e["type"] == "model.request_started"
                      and e["run_id"] == row["run_id"]), None)
        if first is None:
            if result["status"] in {"completed", "empty"}:
                raise ContractError("A completed writer lacks its first input")
            return False
        wire = blobs.get(first["payload"]["request_ref"])
        if "first_writer_inputs" in registration and (first["payload"]["request_ref"]["sha256"] !=
                registration["first_writer_inputs"][row["arm"]]):
            raise ContractError("Observed writer request differs from the preregistered feedback comparison")
        if first["payload"]["actor_role"] != "writer" or [m["role"] for m in wire["messages"]] != ["system", "user", "user"]:
            raise ContractError("Unexpected role or extra messages in the first writer input")
        instruction, experience, feedback = (m["content"] for m in wire["messages"])
        if (source is None or scan["starts"][row["run_id"]]["payload"]["source_run_ids"] != [source.source_run_id]
                or digest(instruction.encode()) != registration["writer_prompt_sha256"][row["arm"]]
                or experience != source.public_context
                or read_json(feedback.encode()) != source.feedback_message(registration.get("writer_feedback_level", "none"))
                or [t["function"]["name"] for t in wire["tools"]] != ["memory_store"]):
            raise ContractError("Writer did not receive exactly its assigned instruction and fresh public experience")
        return True


def ledger_coverage(root, registration, audit):
    if audit["registration_sha256"] != digest(canonical(registration)):
        raise ContractError("The analysis belongs to a different registration")
    totals, charges, issues = defaultdict(Counter), defaultdict(Decimal), Counter()
    seals = read(root / "completion.json")["stages"]
    sessions, banks = set(), set()
    http = unknown = 0
    for case in registration["cases"]:
        case_data = audit["cases"][case["task"]]
        first = root / ("task-" + case["task"]) / case["scheduled_stages"][0]
        source = source_from(first, case["task_id"]) if (first / "completion.json").exists() else None
        for stage, folder_name in zip(STAGES, case["scheduled_stages"], strict=True):
            name = "task-" + case["task"] + "/" + folder_name
            ledger = root / name / "allocations.json"
            if not ledger.exists() and seals[name].get("reason") != "spend_limit":
                raise ContractError("An executed or carried stage has lost its allocation ledger")
            jobs = jobs_for(case, stage)
            rows = read(ledger) if ledger.exists() else [None] * len(jobs)
            for job, row in zip(jobs, rows, strict=True):
                arm = job["arm"]
                reported = case_data[arm]
                item = row and row.get("result")
                if row and row["arm"] != arm:
                    raise ContractError("Allocation order differs from the registered schedule")
                if item:
                    if item["run_id"] != reported["run_id"] or (stage != "write" and item["official_success"] != reported["success"]):
                        raise ContractError("The episode outcome or identity differs from its allocation ledger")
                    session, bank = item["session_id"], item.get("bank_id")
                    if session in sessions or (bank is not None and (bank in banks or stage == "source")):
                        raise ContractError("Sessions and mutable banks must be isolated across all observed assignments")
                    sessions.add(session)
                    if bank is not None:
                        banks.add(bank)
                    if stage == "write":
                        totals[stage]["inputs_verified"] += verify_writer_input(ledger.parent, row, source, registration)
                    for v in item["billing_by_role"].values():
                        charges[stage] += Decimal(v["reported_usd"])
                        http += v["http_calls"]
                        unknown += v["unknown_count"]
                totals[stage]["assigned"] += 1
                if stage == "write" and item and item["status"] in {"completed", "empty"}:
                    totals[stage]["observed"] += 1
                    totals[stage]["empty"] += item["status"] == "empty"
                elif stage != "write" and item and item["grading_status"] == "graded":
                    totals[stage]["observed"] += 1
                else:
                    reason = (item or {}).get("error_code") or (row or {}).get("reason") or reported.get("reason") or reported["status"]
                    issues[stage, reason] += 1
    for stage in STAGES:
        if (totals[stage]["assigned"] != registration["assigned"][stage]
                or charges[stage] != Decimal(audit["stage_cost_usd"][stage]["confirmed"])):
            raise ContractError("The allocation count or observed charge differs from the audit")
    if len(sessions) != audit["distinct_sessions"]:
        raise ContractError("The number of distinct observed sessions differs from the request audit")
    return totals, http, unknown, issues, len(banks)


def render(root, audit):
    entry = read(root / "registration.json")
    registration = entry["preflight"]
    totals, http, unknown, issues, banks = ledger_coverage(root, registration, audit)
    cases = defaultdict(list)
    for c in registration["cases"]:
        cases[c["domain"]].append(audit["cases"][c["task"]])
    domains = {k: v for k, v in DOMAINS.items() if k in cases}
    costs = audit["stage_cost_usd"]
    paid = sum(Decimal(v["confirmed"]) for v in costs.values())
    held = sum(Decimal(v["held"]) for v in costs.values())
    if paid + held != Decimal(read(root / "completion.json")["total_covered_usd"]):
        raise ContractError("The combined cost differs from the final receipt")
    lines = ["# 공식 항공·소매 과제에서 새로 측정한 기억의 효과", "",
             f"1회차 무기억 상담 {totals['source']['observed']}/{totals['source']['assigned']}건과 "
             f"2회차 메모 상담 {totals['solve']['observed']}/{totals['solve']['assigned']}건이 공식 채점됐다. "
             f"작성 {totals['write']['assigned']}건 중 {totals['write']['observed']}건이 끝났고, 그중 "
             f"{totals['write']['empty']}건에서 작성자가 저장을 생략했다. 새 2회차 무기억 배정은 0건이다.", "",
             f"실행 결과가 있는 배정에서 세션 ID {audit['distinct_sessions']:,}개, 가변 메모 은행 ID {banks:,}개의 "
             f"고유성을 확인했다. 작성자 첫 입력 {totals['write']['inputs_verified']}건의 공개 대화 원문을 해당 무기억 상담에서 다시 만들고 "
             "실제 전송용 본문과 대조했다. 작성 지침과 성패 피드백을 비운 설정도 일치했다. 같은 작성본을 두 상담에서 쓸 때는 각 은행에 복제했다.", "",
             f"공급자 요청은 {http:,}건이고 확인 비용은 ${paid}이다. 비용 미확정 요청은 {unknown}건, "
             f"보류액은 ${held}이다." + (" 최초 두 기술 결측의 6요청과 $0.001702677을 포함한다."
                                      if "continued_from" in entry else ""), "",
             "## 같은 과제의 성공 변화", "",
             "회복은 1회차 실패·2회차 성공, 손실은 1회차 성공·2회차 실패다. "
             "차이는 양쪽이 채점된 과제에서 구한 성공률 차이다. 고객별 차이는 같은 고객의 과제들을 평균한 뒤 고객에 같은 가중치를 준 값이다. "
             "본문 노출의 분모는 상담원이 한 번 이상 응답한 메모 상담이다.", ""]
    for domain, label in domains.items():
        first = [c["no_memory"].get("success") for c in cases[domain]]
        assigned_customers = {c["customer"] for c in registration["cases"] if c["domain"] == domain}
        graded_customers = {c["customer"] for c in registration["cases"] if c["domain"] == domain
                            and audit["cases"][c["task"]]["no_memory"].get("success") is not None}
        lines += [f"### {label}", "", f"1회차 무기억은 채점 {sum(x is not None for x in first)}건 중 "
                  f"{sum(x is True for x in first)}건 성공했다. 등록 과제는 {len(first)}개이고, "
                  f"고객은 배정 {len(assigned_customers)}명 중 {len(graded_customers)}명에게서 1회차 성적을 얻었다.", "",
                  "| 메모 | 전달 | 채점쌍 | 회복 | 손실 | 차이(%p) | 고객별 차이(%p) | 본문 노출 |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for form in FORMS:
            for mode in MODES:
                group = audit["contrasts"]["/".join((domain, form, mode, "no_memory"))]
                paired = Counter((c[form + "__" + mode].get("success"), c["no_memory"].get("success")) for c in cases[domain])
                gain, loss = paired[True, False], paired[False, True]
                observed = sum(v for pair, v in paired.items() if None not in pair)
                if (gain, loss, observed) != (group["outcomes"]["gain"], group["outcomes"]["loss"], group["jointly_graded"]["pairs"]):
                    raise ContractError("Recount of per-task outcomes differs from the paired analysis")
                diff = f"{100 * (gain - loss) / observed:+.1f}" if observed else "—"
                customer = group["equal_customer_difference_pp"]
                cw = f"{customer:+.1f}" if customer is not None else "—"
                lines.append(f"| {FORM_NAMES[form]} | {MODE_NAMES[mode]} | {observed} | {gain} | {loss} | {diff} | {cw} | "
                             f"{group.get('body_seen', 0)}/{group.get('observed_reader_episodes', 0)} |")
        lines.append("")
    domain_headers = [DOMAINS[d] for d in domains]
    lines += ["## 작성자가 저장한 비율", "",
              "각 셀은 메모를 하나 이상 저장한 작성자 수와 정상 종료한 작성자 수다. 정상 종료에는 자발적으로 빈 은행을 남긴 경우도 포함한다.", "",
              "| 형식 | " + " | ".join(domain_headers) + " |", "| --- | " + " | ".join("---:" for _ in domains) + " |"]
    for form in FORMS:
        cells = []
        for domain in domains:
            completed = [c[form] for c in cases[domain] if c[form]["status"] in {"completed", "empty"}]
            stored = sum(w["written_items"] > 0 for w in completed)
            cells.append(f"{stored}/{len(completed)}")
        lines.append(f"| {FORM_NAMES[form]} | " + " | ".join(cells) + " |")
    changes = []
    for mode in MODES:
        observed = [(c[f + "__" + mode].get("success"), c["no_memory"].get("success"))
                    for records in cases.values() for c in records for f in FORMS if c[f].get("written_items") == 0]
        paired = Counter(pair for pair in observed if None not in pair)
        changes.append(f"{MODE_NAMES[mode]} {paired.total()}쌍에서 회복 {paired[True, False]}건·손실 {paired[False, True]}건")
    lines += ["", "저장을 생략한 은행만 따로 집계하면 " + ", ".join(changes) + "이다. "
              "작성자가 자발적으로 비운 경우를 모은 사후 관찰이며, 이 성패 변화에는 전달 안내와 독립적으로 생성된 대화의 변동이 포함된다."]
    lines += ["", "## 같은 메모를 자동 제공한 상담과 자율 조회에 맡긴 상담", "",
              "각 셀은 양쪽이 채점된 쌍 중 자동 제공만 성공한 수와 자율 조회만 성공한 수를 차례로 표시한다. 괄호는 채점쌍 수다.", "",
              "| 형식 | " + " | ".join(domain_headers) + " |", "| --- | " + " | ".join("---:" for _ in domains) + " |"]
    for form in FORMS:
        cells = []
        for domain in domains:
            group = audit["contrasts"]["/".join((domain, form, "preload", form + "__on_demand"))]
            matched = group["outcomes"]
            cells.append(f"{matched['gain']} / {matched['loss']} ({group['jointly_graded']['pairs']})")
        lines.append(f"| {FORM_NAMES[form]} | " + " | ".join(cells) + " |")
    lines.append("")
    lines += ["## 고객 입력과 실제 첫 발화", "",
              "상담을 시작할 때 고객 모델에 보낸 요청은 등록 해시와 대조했다. 아래는 그 고객 모델이 실제로 생성한 "
              "첫 공개 응답이 1회차와 일치한 횟수다. 각 행은 다섯 형식의 상담을 합산했고 같은 과제의 1회차를 공유한다.", "",
              "| 도메인 | 전달 | 첫 응답 일치 / 양쪽 관측 | 본문 노출 / 상담원 응답 관측 | 저장된 메모가 있던 상담의 본문 노출 | 조회한 상담 | 조회 호출 |",
              "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for domain, label in domains.items():
        for mode in MODES:
            groups = [audit["contrasts"]["/".join((domain, f, mode, "no_memory"))] for f in FORMS]
            identical, comparable, seen, readers, calls = (sum(g.get(key, 0) for g in groups) for key in (
                "identical_opening_replies", "comparable_opening_replies", "body_seen", "observed_reader_episodes", "read_calls"))
            observations = [(c[f], c[f + "__" + mode]) for c in cases[domain] for f in FORMS
                            if c[f + "__" + mode].get("body_seen") is not None]
            nonempty = [r for w, r in observations if w.get("written_items")]
            opened = sum(r["reads"] > 0 for _, r in observations)
            lines.append(f"| {label} | {MODE_NAMES[mode]} | {identical}/{comparable} | {seen}/{readers} | "
                         f"{sum(r['body_seen'] for r in nonempty)}/{len(nonempty)} | {opened}/{readers} | {calls} |")
    lines.append("")
    lines += ["## 같은 성공을 얻는 데 든 노력", "",
              "양쪽 모두 성공한 상담쌍에서 조건별 합계를 비교했다. 각 형식은 같은 과제의 1회차를 공유한다. "
              "토큰과 상담 비용은 상담원·고객의 실측이고, 작성·채점까지 포함한 전체 비용은 별도로 집계했다. "
              "괄호에는 해당 지표를 양쪽에서 관측한 쌍의 수를 표시한다.", "",
              "| 도메인 | 메모 | 전달 | 양쪽 성공 | 고객 발화 증감 | 업무 호출 증감 | 토큰 증감 | 상담 비용 증감(USD) |",
              "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for domain, label in domains.items():
        for form in FORMS:
            for mode in MODES:
                group = audit["contrasts"]["/".join((domain, form, mode, "no_memory"))]["both_success"]
                metrics = [group["metrics"][k] for k in ("customer_turns", "business_calls", "tokens", "usd")]
                values = [f"{m['difference']} ({m['observed']})" if m["observed"] else "— (0)" for m in metrics]
                lines.append(f"| {label} | {FORM_NAMES[form]} | {MODE_NAMES[mode]} | {group['pairs']} | " + " | ".join(values) + " |")
    lines += ["", "## 비용과 결측", "", "| 단계 | 확인 비용(USD) | 보류액(USD) | 결측 원인별 배정 수 |",
              "| --- | ---: | ---: | --- |"]
    for stage, name in zip(STAGES, ("1회차 무기억", "메모 작성", "2회차 메모"), strict=True):
        missing = ", ".join(f"`{key}` {count}" for (s, key), count in sorted(issues.items()) if s == stage)
        lines.append(f"| {name} | {costs[stage]['confirmed']} | {costs[stage]['held']} | {missing or '0'} |")
    lines += ["", "## 해석 범위", "",
              "각 과제에서 다섯 형식의 작성자는 한 무기억 경험을 공유한다. 같은 작성물의 자동 제공과 자율 조회는 독립 세션에서 한 번씩 실행했다. "
              "최초 업무 환경과 고객 모델에 보낸 첫 요청은 고정했다. 실제 생성된 발화와 이후 경로에는 변동이 있다. "
              "노출은 본문이 상담원 입력에 들어갔다는 기록이다. 성공의 원인을 특정 문장에 돌리려면 행동 근거와 별도 비교가 필요하다.", "",
              "같은 은행의 전달 비교, 관측된 첫 발화 일치 수, 세션별 성공과 요청 근거는 전체 분석에서 확인할 수 있다. "
              "[전체 분석](../../artifacts/analyses/20261124-fresh-matched-results.json) · "
              "[설계와 격리 규칙](../research/fresh-matched-series.md) · "
              "[원장에서 표 다시 만들기](../../scripts/summarize_fresh_matched.py)", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("audited_json", type=Path)
    args = parser.parse_args()
    print(render(args.run_directory, read(args.audited_json)), end="")
