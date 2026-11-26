"""Build an offline, browsable comparison from verified writing and native episode journals."""
from collections import Counter, defaultdict, deque
from decimal import Decimal
from importlib.util import find_spec
from itertools import combinations, zip_longest
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from agent_memory_eval.adapters.tau2_assessment import single_action_attempts
from agent_memory_eval.analysis.claim_evidence import paired_counts
from agent_memory_eval.analysis.efficiency import archived_effort, episode_effort, paired_effort
from agent_memory_eval.analysis.forms_actions import check_judgment
from agent_memory_eval.analysis.forms_results import checked_rows, contrast
from agent_memory_eval.analysis.selection import worker_choices
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import FORMS, write_json
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.journal import safe_path


def _link(label, origin, target):
    return f"[{label}]({quote(os.path.relpath(target, origin), safe='/.-_')})"


def _literal(text):
    return "\n".join("    " + line for line in str(text).splitlines()) or "    (빈 내용)"


def _outcome(row):
    value = row.get("result", {}).get("official_success")
    return {True: "성공", False: "실패"}.get(value, "미시작" if row["status"] == "not_started" else "미채점")


def _first_pass_check(row, assignment, events, blobs, expected, source):
    """Distinguish missing historical checks from a sealed, graded comparison."""
    if row["result"].get("error_code") in ("FIRST_PASS_MISMATCH", "TASK_START_MISMATCH"):
        return "mismatch_detected"
    proofs = [i for i, e in enumerate(events) if e["type"] == "business.transition"
              and e["payload"].get("origin") == "forms.first_pass_match"]
    if not proofs:
        return "unverified"
    link = assignment.get("archived_first_pass", {})
    if (len(proofs) != 1 or not expected or blobs.get(events[proofs[0]]["payload"]["receipt_ref"]) != expected
            or source != expected["source_run_id"] or link.get("source_run_id") != source
            or link.get("control_sha256") != expected["source_control_sha256"]
            or assignment["native_config_ref"]["sha256"] != expected["native_config_sha256"]):
        raise ContractError("First-pass runtime proof and frozen source identity differ")
    first_customer = None
    worker_seen = False
    restored = None
    if "task_start_sha256" in expected:
        openings = [(i, e["payload"]) for i, e in enumerate(events) if e["payload"].get("origin") == "task_start.first_decision"]
        if openings:
            pos, p = openings[0]
            if (len(openings) != 1 or pos <= proofs[0] or p.get("source_run_id") != source
                    or p.get("source_log_sha256") != expected["source_log_sha256"]
                    or p.get("source_worker_request_id") != expected["source_worker_request_id"]
                    or p["task_start_ref"]["sha256"] != expected["task_start_sha256"]
                    or any(e["type"] == "model.request_started" for e in events[:pos])):
                raise ContractError("Restored opening differs from the registered first decision")
            restored = blobs.get(p["task_start_ref"])["public_input"]["messages"]
    for i, e in enumerate(events):
        if e["type"] != "model.request_started":
            continue
        if i < proofs[0]:
            raise ContractError("First-pass setup must be checked before the first request")
        p = e["payload"]
        if p["actor_role"] == "user_simulator" and first_customer is None and "task_start_sha256" not in expected:
            first_customer = p["request_ref"]["sha256"]
            if first_customer != expected["customer_opening_sha256"]:
                raise ContractError("Customer opening differs from the archived first pass")
        if p["actor_role"] == "worker":
            if not worker_seen and restored is not None and blobs.get(p["request_ref"])["messages"][-2:] != restored[-2:]:
                raise ContractError("The original greeting and customer opening did not reach the first worker decision")
            worker_seen = True
            if blobs.get(p["request_ref"]).get("seed") != expected["native_seed"]:
                raise ContractError("Solver request seed differs from the archived first pass")
    if row["result"]["grading_status"] != "graded":
        return "ungraded"
    if not (first_customer or restored) or not worker_seen:
        raise ContractError("Graded first-pass comparison is missing a role's request")
    return "matched_graded"


def _public(messages):
    return [dict(role=m["role"], content=m.get("content"), tool_calls=m.get("tool_calls") or []) for m in messages]


def _action_messages(messages):
    for i, m in enumerate(messages):
        for c in m["tool_calls"]:
            yield i, (c["name"], c["arguments"])


def _first_tool_difference(left, right):
    for a, b in zip_longest(_action_messages(left), _action_messages(right)):
        if a is None or b is None or a[1] != b[1]:
            return a, b
    return None


def _grade_evidence(report, messages):
    reward = report["reward_info"]
    required = []
    for check in reward.get("communicate_checks") or []:
        cited = check.get("justification") or ""
        turn = next((i for i, m in enumerate(messages) if m["role"] == "assistant" and m["content"]
                     and cited.endswith("'" + m["content"] + "'")), None)
        required.append(dict(info=check["info"], met=check["met"], cited_turn=turn))
    needs_native = (reward.get("reward_basis") == ["ACTION"] and len(reward.get("action_checks") or []) == 1
                    and find_spec("tau2") is None)
    return dict(db_match=(reward.get("db_check") or {}).get("db_match"), communication=required,
                reward_basis=reward.get("reward_basis"), needs_native=needs_native,
                single_action=None if needs_native else single_action_attempts(report),
                reference_actions=[dict(name=c["action"]["name"], matched=c["action_match"])
                                   for c in reward.get("action_checks") or []])


def _grade_section(grade):
    match = {True: "일치", False: "불일치", None: "평가 없음"}
    lines = ["", '<a id="grade"></a>', "", "## 공식 채점 근거", "",
             f"- 최종 DB · {match[grade['db_match']]}"]
    for c in grade["communication"]:
        turn = c["cited_turn"]
        link = f" · [채점기가 인용한 발화 {turn}](#m-{turn})" if turn is not None else ""
        lines.append(f"- 필수 안내 `{c['info']}` · {match[c['met']]}{link}")
    if not grade["communication"]:
        lines.append("- 필수 안내 항목 0개")
    if grade["reference_actions"]:
        basis = ", ".join(grade["reward_basis"] or []) or "기록 없음"
        lines += ["", f"공식 보상 기준은 `{basis}`이다. 아래는 공식 채점기의 참조 행동 검사다.", ""]
        lines += [f"- `{a['name']}` · {match[a['matched']]}" for a in grade["reference_actions"]]
    action = grade["single_action"]
    if grade["needs_native"]:
        lines += ["", "첫 시도 판정에는 이 실험의 τ 벤치마크 패키지가 필요하다."]
    if action is not None:
        attempts = action["attempts"]
        lines += ["", f"**{action['actor']}의 `{action['name']}` 호출** · 총 {len(attempts)}회", ""]
        if attempts:
            first = attempts[0]
            lines.append(f"- [첫 시도](#m-{first['turn']}) · {match[first['matched']]}")
            hit = action["first_correct_attempt"]
            if hit and hit > 1:
                lines.append(f"- [처음 일치한 시도](#m-{attempts[hit - 1]['turn']}) · {hit}번째 호출")
        else:
            lines.append("- 해당 행위자의 호출 0회")
    return lines


def _job_evidence(row, folder, scan, state, blobs, source, writers, pages, grades, *, imported=False):
    job, run, result = row["index"], row["run_id"], row["result"]
    name = f"writer-{job}.md" if imported else f"job-{job}.md"
    task, rep, arm = row["task_id"], row["repetition"], row["arm"]
    text = [f"# 실행 {job} · 과제 {task} · 반복 {rep} · {arm}", "", f"단계 `{row['phase']}` · 상태 `{row['status']}`",
            "", _link("원본 사건 기록", pages, folder / "log.jsonl")]
    starts = [e["payload"] for e in scan["events"].values() if e["type"] == "model.request_started"]
    finishes = {e["payload"]["request_id"]: e["payload"] for e in scan["events"].values()
                if e["type"] == "model.request_finished"}
    exposure = defaultdict(Counter)
    for e in scan["events"].values():
        if e["type"] == "memory.input_included":
            p = e["payload"]
            exposure[p["request_id"]][p["field"]] += 1
    worker_responses = defaultdict(deque)
    messages = []
    if row["phase"] == "write":
        cfg = state["memory_writes"][run]
        proof = blobs.get(blobs.get(cfg["experience_ref"])["source_ref"])
        ref = proof["public_context_ref"]
        source_path = safe_path(folder, ref["path"])
        public_context = blobs.get(ref)
        if digest(canonical(public_context)) != proof["public_context_sha256"]:
            raise ContractError("Writer's public source digest differs from its referenced content")
        source.setdefault(task, (source_path, proof["public_context_sha256"]))
        if source[task][1] != proof["public_context_sha256"]:
            raise ContractError("Task writers received different first-pass experiences")
        writers[task, rep, arm] = name
        text += ["", _link("1회차 공개 경험 원문", pages, source_path), "", "## 전송한 작성 지침", "", _literal(cfg["instruction"])]
        snapshot = result.get("snapshot_id")
    else:
        cfg = state["assignments"][run]
        if task in source:
            text += ["", _link("같은 과제의 1회차 공개 경험", pages, source[task][0])]
        writer_arm = cfg.get("fixed_bank_delivery", {}).get("writer", arm)
        if (task, rep, writer_arm) in writers:
            text += ["", f"[이 조건의 작성 세션]({writers[task, rep, writer_arm]})"]
        snapshot = result.get("start_snapshot_id")
        text += ["", f"공식 결과 **{_outcome(row)}** · 종료 `{result['termination_reason']}`"]
        report_ref = result.get("report_ref") or result.get("native_report_ref")
        if report_ref:
            report = blobs.get(report_ref)
            messages = _public(report["messages"])
            text += ["", _link("native 원본 대화·도구·채점", pages, safe_path(folder, report_ref["path"]))]
            if result["grading_status"] == "graded":
                grade = _grade_evidence(report, messages)
                grades[job] = grade
                text += _grade_section(grade)
        elif result.get("partial_trajectory_ref"):
            ref = result["partial_trajectory_ref"]
            messages = _public(blobs.get(ref))
            text += ["", _link("중단 시점 부분 궤적", pages, safe_path(folder, ref["path"]))]
    items = snapshot_items(blobs, state, snapshot) if snapshot else []
    text += ["", "## 작성된 기억" if row["phase"] == "write" else "## 배정된 시작 기억", ""]
    if not items:
        text.append("항목 0개" if snapshot else "스냅샷 없음")
    for item in items:
        text += [_literal(item["title"]), "", _literal(item["body"]), ""]
    text += ["", "## 요청과 실제 전달 상태", ""]
    for p in starts:
        if p["actor_role"] not in {"writer", "worker"}:
            continue
        f = finishes[p["request_id"]]
        links = [_link("전송 구성", pages, safe_path(folder, p["request_ref"]["path"])),
                 _link("입력 출처", pages, safe_path(folder, p["input_manifest_ref"]["path"]))]
        if f["response_ref"]:
            links.append(_link("응답", pages, safe_path(folder, f["response_ref"]["path"])))
            if p["actor_role"] == "worker":
                raw = blobs.get(f["response_ref"])
                choices = raw.get("choices") if isinstance(raw, dict) else None
                if choices:
                    m = choices[0]["message"]
                    key = (m.get("content") or "", tuple(c["id"] for c in m.get("tool_calls") or []))
                    worker_responses[key].append(links[0])
        fields = exposure[p["request_id"]]
        text.append(f"- {p['actor_role']} · {f['delivery_status']} · 제목 {fields['title']} · 본문 {fields['body']} · "
                    + " · ".join(links))
    if messages:
        text += ["", "## 공개 대화와 도구 호출", ""]
        for i, m in enumerate(messages):
            text += [f'<a id="m-{i}"></a>', "", f"**{i} · {m['role']}**", ""]
            key = m["content"] or "", tuple(c["id"] for c in m["tool_calls"])
            if i and m["role"] == "assistant" and worker_responses[key]:
                text += [worker_responses[key].popleft(), ""]
            if m["content"]:
                text += [_literal(m["content"]), ""]
            for call in m["tool_calls"]:
                text += [_literal(call["name"] + " " + canonical(call["arguments"]).decode()), ""]
    (pages / name).write_text("\n".join(text) + "\n", encoding="utf-8")
    return messages


def _case_page(pages, left, right, evidence, review, grades):
    li, ri = left["index"], right["index"]
    def side(row):
        return (f"[{row['arm']} · {_outcome(row)} · {row['index']}](job-{row['index']}.md)"
                if row["index"] in evidence else f"{row['arm']} · {_outcome(row)} · {row.get('reason', '')}")
    title = f"과제 {left['task_id']} · 반복 {left['repetition']} · {left['arm']} / {right['arm']}"
    text = [f"# {title}", "", side(left) + " · " + side(right), ""]
    if li in grades and ri in grades and left["result"]["official_success"] != right["result"]["official_success"]:
        components = []
        if grades[li]["db_match"] != grades[ri]["db_match"]:
            components.append("최종 DB 상태")
        required = [{c["info"]: c["met"] for c in grades[j]["communication"]} for j in (li, ri)]
        components += [f"필수 안내 `{key}`" for key in sorted(required[0].keys() | required[1].keys())
                       if required[0].get(key) != required[1].get(key)]
        if components:
            text += ["## 공식 점수가 갈린 항목", "", " · ".join(components), "",
                     f"[{left['arm']} 채점 근거](job-{li}.md#grade) · [{right['arm']} 채점 근거](job-{ri}.md#grade)", ""]
    if review:
        sides = review["sides_in_blind_order"]
        data = {li: left, ri: right}
        for s in sides:
            row = data[s["job"]]
            if s["run_id"] != row["run_id"] or s["official_success"] != row["result"]["official_success"]:
                raise ContractError("Paired annotation refers to a different run")
        check_judgment(review["judgment"], [dict(messages=evidence[s["job"]]) for s in sides])
        text += ["## 원문과 대조한 가림 판정", "", _literal(review["judgment"]["first_difference"]), ""]
        for s, j in zip(sides, review["judgment"]["sides"]):
            text += [f"- {s['arm']} · [첫 분기](job-{s['job']}.md#m-{j['index']}) · "
                     f"[결말](job-{s['job']}.md#m-{j['end_index']}) · {j['observed_result']}"]
    if li in evidence and ri in evidence and evidence[li] and evidence[ri]:
        text += ["", "## 도구 호출 순서에서 처음 달라진 지점", ""]
        diff = _first_tool_difference(evidence[li], evidence[ri])
        if diff:
            for row, item in zip((left, right), diff):
                if item is None:
                    text.append(f"- {row['arm']} · 이후 호출 없음")
                else:
                    i, (tool, args) = item
                    text += [f"- {row['arm']} · [공개 발화 {i}](job-{row['index']}.md#m-{i})", "",
                             _literal(tool + " " + canonical(args).decode()), ""]
        else:
            text.append("기록된 도구 이름과 인수의 순서가 같았습니다.")
        text += ["", "앞에서부터 같은 순번의 도구 이름·인수를 비교했습니다. 각 대화에서 고객의 요청과 승인은 개별 실행 원문에서 확인할 수 있습니다."]
    name = f"pair-{li}-{ri}.md"
    (pages / name).write_text("\n".join(text) + "\n", encoding="utf-8")
    return name


def _make_report(run, staged, destination, annotation_path, with_behavior, source_workspace):
    pages = staged / "evidence"
    pages.mkdir()
    sources, writers, evidence, grades = {}, {}, {}, {}
    retrieval = {}
    control = read_json((run / "control.json").read_bytes())
    source_runs = {s["task_id"]: s["run_id"] for s in control["design"]["source"]["cohort"]}
    planned_checks = control.get("preflight", {}).get("first_pass_match", {})
    first_pass_checks = {}
    with_behavior = with_behavior or control["design"]["second_pass"].get("baseline") == "archived_first_pass"
    effort = {}
    def observe(row, folder, scan, state, blobs):
        evidence[row["index"]] = _job_evidence(row, folder, scan, state, blobs, sources, writers, pages, grades=grades)
        if row["phase"] == "solve":
            run_id = row["run_id"]
            cfg = state["assignments"][run_id]
            events = [e for e in scan["events"].values() if e["run_id"] == run_id]
            expected = planned_checks.get(row["task_id"], {}).get(str(row["repetition"]))
            first_pass_checks[row["index"]] = _first_pass_check(
                row, cfg, events, blobs, expected, source_runs[row["task_id"]])
            returned = Counter(e["payload"]["operation"] for e in events if e["type"] == "memory.returned")
            received = {e["payload"]["request_id"] for e in events if e["type"] == "model.request_finished"
                        and e["payload"]["delivery_status"] == "response_received" and e["payload"]["status"] == "response"}
            fields = Counter(e["payload"]["field"] for e in events if e["type"] == "memory.input_included"
                             and e["payload"]["request_id"] in received)
            if sum(fields.values()) != row["result"]["normal_response_memory_spans"]:
                raise ContractError("The report's normal-response exposure differs from the sealed job")
            retrieval[row["index"]] = dict(task=row["task_id"], repetition=row["repetition"], arm=row["arm"],
                items=(cfg["bank_sizes"] or {}).get("items", 0), writer_snapshot=cfg["source_writer_snapshot_id"],
                list_calls=returned["list"], get_calls=returned["get"], title_spans=fields["title"], body_spans=fields["body"])
            if with_behavior:
                trace = worker_choices(folder, run_id=run_id, evidence=(scan, state, run_id))
                name = f"choices-{row['index']}.json"
                write_json(pages / name, trace)
                with (pages / f"job-{row['index']}.md").open("a", encoding="utf-8") as page:
                    page.write(f"\n[각 선택 직전의 공개 입력·메모 후보와 실제 응답]({name})\n")
                messages = evidence[row["index"]] if row["result"].get("native_report_ref") or row["result"].get("report_ref") else None
                effort[row["index"]] = episode_effort(row, messages, trace["choices"])
    def observe_parent(row, folder, scan, state, blobs):
        _job_evidence(row, folder, scan, state, blobs, sources, writers, pages, grades, imported=True)
    design, completion, rows, usage = checked_rows(run, observe_job=observe, observe_parent=observe_parent)
    # Both stage and destination are siblings, so relative file links remain valid on publication.
    source_grades = control["source_outcomes"]
    solves = [r for r in rows if r["phase"] == "solve"]
    by_cell = {(r["task_id"], r["repetition"], r["arm"]): r for r in solves}
    groups = {r["task_id"]: r["source_group"] for r in design["source"]["cohort"]}
    annotations = {}
    if annotation_path:
        raw = read_json(Path(annotation_path).read_bytes())
        annotations = {(x["treatment_job"], x["control_job"]): x for x in raw["pairs"].values()}
    if design.get("delivery_arms"):
        ids = { (c["writer"], c["read_mode"]): arm for arm, c in design["delivery_arms"].items() }
        writer_ids = [c["id"] for c in design["conditions"]]
        pairs = ([(p["reference"], p["candidate"]) for p in design["representation_pairs"].values()]
                 if "representation_pairs" in design else [writer_ids])
        comparisons = [(ids[w, "on_demand"], ids[w, "preload"]) for w in writer_ids]
        comparisons += [(ids[b, mode], ids[a, mode]) for a, b in pairs for mode in ("preload", "on_demand")]
        sets = {"all": list(groups), **design["task_sets"]}
    elif design.get("format") == "agent-memory-new-customer-design/v1":
        if design["stage"] != "comparison":
            raise ContractError("Choose the complete comparison run for the paired report")
        forms = design["second_pass"]["primary_arms"]
        comparisons = [(a, c) for a in forms for c in design["second_pass"]["matched_controls"]]
        comparisons += list(combinations(forms, 2)) + [("empty_preload", "no_memory")]
        sets = {"all": list(groups), **design["task_sets"]}
    elif "representation_pairs" in design:
        comparisons = [(p["candidate"], p["reference"]) for p in design["representation_pairs"].values()]
        sets = {"all": list(groups), **design["task_sets"]}
    elif "conditions" in design:
        a, b = [c["id"] for c in design["conditions"]]
        controls = design["second_pass"].get("matched_controls", ("no_memory", "empty_preload"))
        comparisons = [(b, a)] + [(arm, control) for control in controls for arm in (a, b)]
        sets = {"all": list(groups), **design["task_sets"]}
    else:
        comparisons = [(a, "no_memory") for a in FORMS]
        sets = {"all": list(groups)}
    contrasts = {s: [contrast([r for r in solves if r["task_id"] in tasks], a, b, groups) for a, b in comparisons]
                 for s, tasks in sets.items()}
    transitions = {}
    for arm in dict.fromkeys(r["arm"] for r in solves):
        transitions[arm] = {str(origin).lower(): dict(Counter(_outcome(r) for r in solves
                            if r["arm"] == arm and source_grades[r["task_id"]] is origin)) for origin in (True, False)}
    cost = defaultdict(lambda: dict(jobs=0, http_calls=0, known_usd=Decimal(0), unknown_requests=0, empty_writers=0))
    for r in rows:
        c = cost[r["arm"]]
        c["jobs"] += 1
        c["empty_writers"] += r["phase"] == "write" and r["status"] == "empty"
        for v in r.get("result", {}).get("billing_by_role", {}).values():
            c["http_calls"] += v["http_calls"]
            c["known_usd"] += Decimal(v["reported_usd"])
            c["unknown_requests"] += v["unknown_count"]
    for c in cost.values():
        c["known_usd"] = str(c["known_usd"])
    outcomes = Counter(_outcome(r) for r in solves)
    check_by_job = {r["index"]: ("not_started" if r["status"] == "not_started" else first_pass_checks[r["index"]])
                    for r in solves}
    check_counts = Counter(check_by_job.values())
    matched_tasks = {r["task_id"] for r in solves if check_by_job[r["index"]] == "matched_graded"}
    comparison_scope = dict(by_job=check_by_job, counts=dict(check_counts),
                            matched_source_runs=len({source_runs[t] for t in matched_tasks}),
                            matched_source_groups=len({groups[t] for t in matched_tasks}))
    index = ["# 두 회차 기억 비교", "", f"2회차 배정 {len(solves)}건 · 공식 성공 {outcomes['성공']}건 · "
             f"공식 실패 {outcomes['실패']}건 · 미채점 {outcomes['미채점']}건 · 미시작 {outcomes['미시작']}건", "",
             "[설정·성적·비용 전체 표](summary.json) · " +
             _link("실행 당시 명세", destination, run / "control.json"), "",
             f"1회차 대조 기록과 실제 역할별 요청에서 실행 조건을 확인하고 공식 채점까지 마친 상담은 "
             f"{check_counts['matched_graded']}/{len(solves)}건이다. 이 쌍의 고유 원천은 {comparison_scope['matched_source_runs']}건, "
             f"출처 그룹은 {comparison_scope['matched_source_groups']}개다. 전체 회차 간 성적표에는 관측 전환을 표시한다. "
             "같은 2회차 안에서 두 정책을 비교한 표는 아래에 있다.", "",
             "## 과제 집합별 결과", "",
             "| 과제 집합 | 비교 조건 / 기준 조건 | 함께 채점 | 개선 | 악화 | 모두 성공 | 모두 실패 | 결측 | 과제 평균 차이 (%p) |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for subset, reports in contrasts.items():
        for c in reports:
            n = c["counts"]
            index.append(f"| {subset} | {c['treatment']} / {c['control']} | {c['jointly_graded_pairs']} | {n['gain']} | "
                         f"{n['loss']} | {n['both_success']} | {n['both_failure']} | {n['ungraded']} | {c['task_equal_difference_pp']} |")
    baseline = None
    if design["second_pass"].get("baseline") == "archived_first_pass":
        if set(source_grades) != set(groups) or any(type(s) is not bool for s in source_grades.values()):
            raise ContractError("Each paired task requires its graded original first pass")
        baseline = dict(kind="archived_first_pass", source_log_sha256=design["source"]["log_sha256"],
                        source_by_task={s["task_id"]: dict(run=s["run_id"], success=source_grades[s["task_id"]])
                                        for s in design["source"]["cohort"]}, by_task_set={}, matched_graded_by_task_set={})
        index += ["", "## 같은 과제의 1회차 무기억 대비 변화", "",
                  "한 과제의 원천 점수는 여러 메모 조건과 반복이 공유한다. 성패 네 열과 결측은 전체 배정의 관측 결과다. "
                  "마지막 열은 같은 입력 조건으로 검증하고 채점한 쌍에서의 회복·손실이다.", "",
                  "| 과제 집합 | 메모 조건 | 배정 | 실패→성공 | 성공→실패 | 모두 성공 | 모두 실패 | 결측 | 검증·채점 쌍 (회복 / 손실) |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
        for subset, tasks in sets.items():
            base_rows = baseline["by_task_set"][subset] = {}
            verified = baseline["matched_graded_by_task_set"][subset] = {}
            for arm in transitions:
                group = [r for r in solves if r["arm"] == arm and r["task_id"] in tasks]
                counts = paired_counts((r.get("result", {}).get("official_success"), source_grades[r["task_id"]]) for r in group)
                matched = verified[arm] = paired_counts((r["result"]["official_success"], source_grades[r["task_id"]])
                    for r in group if check_by_job[r["index"]] == "matched_graded")
                base_rows[arm] = counts
                index.append(f"| {subset} | {arm} | {counts['assigned']} | {counts['gain']} | {counts['loss']} | "
                             f"{counts['both_success']} | {counts['both_failure']} | {counts['missing']} | "
                             f"{matched['assigned']} ({matched['gain']} / {matched['loss']}) |")
    index += ["", "## 조회와 실제 노출", "", "| 조건 | 수행한 실행 | 비지 않은 은행 | 목록 조회한 실행 | 본문 조회한 실행 | 본문이 정상 응답 요청에 포함된 실행 |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for arm in dict.fromkeys(r["arm"] for r in solves):
        subset = [v for v in retrieval.values() if v["arm"] == arm]
        counts = [sum(v[k] > 0 for v in subset) for k in ("items", "list_calls", "get_calls", "body_spans")]
        index.append(f"| {arm} | {len(subset)} | " + " | ".join(map(str, counts)) + " |")
    index += ["", "## 과제별 원문", "", "각 비교에서 작성 세션, 실제 요청과 응답, 대화·도구, 점수를 열 수 있습니다.", "",
              "| 과제 | 반복 | 비교 조건 / 기준 조건 | 원문 |", "| --- | ---: | --- | --- |"]
    linked = set()
    for a, b in comparisons:
        for left in (r for r in solves if r["arm"] == a):
            right = by_cell[left["task_id"], left["repetition"], b]
            key = left["index"], right["index"]
            page = _case_page(pages, left, right, evidence, annotations.get(key), grades)
            linked.add(key)
            index.append(f"| {left['task_id']} | {left['repetition']} | {a} / {b} | "
                         f"[{_outcome(left)} / {_outcome(right)}](evidence/{page}) |")
    if annotations.keys() - linked:
        raise ContractError("Annotations include cases outside this comparison")
    index += ["", "## 전체 배정", ""]
    for r in rows:
        name = f"{r['index']} · {r['phase']} · {r['arm']} · 과제 {r['task_id']} · 반복 {r['repetition']} · {r['status']}"
        index.append(f"- [{name}](evidence/job-{r['index']}.md)" if r["index"] in evidence else f"- {name} · {r['reason']}")
    summary = dict(format="agent-memory-comparison-report/v1", completion_sha256=digest((run / "completion.json").read_bytes()),
                   allocation_sha256=completion["allocation_sha256"], conditions=design.get("conditions", []),
                   task_sets=sets, outcomes=dict(outcomes), contrasts=contrasts, first_to_second=transitions,
                   first_pass_comparability=comparison_scope,
                   costs_by_condition=dict(cost), usage_by_role=usage, completion=completion,
                   retrieval_by_job=retrieval, native_grade_components=grades, writer_parent=design.get("writer_parent"),
                   verified_paired_annotations=len(annotations), paired_case_pages=len(linked))
    if baseline is not None:
        summary["baseline"] = baseline
    if "representation_pairs" in design:
        summary["representation_pairs"] = design["representation_pairs"]
    if with_behavior:
        efficiency = {}
        archive = None
        if baseline is not None or design["format"] in {"agent-memory-prompt-comparison-design/v1", "agent-memory-prompt-delivery-design/v1"}:
            archive = archived_effort(design["source"], source_workspace, source_grades)
            archive["by_task_set"] = {}
        for subset, tasks in sets.items():
            efficiency[subset] = []
            for a, b in comparisons:
                matched = [(r, by_cell[r["task_id"], r["repetition"], b]) for r in solves
                           if r["arm"] == a and r["task_id"] in tasks]
                paired = paired_effort((left.get("result", {}).get("official_success"),
                                        right.get("result", {}).get("official_success"),
                                        effort.get(left["index"], {}), effort.get(right["index"], {}))
                                       for left, right in matched)
                efficiency[subset].append(dict(candidate=a, reference=b, **paired))
            if archive is not None:
                archive["by_task_set"][subset] = []
                for arm in design["second_pass"]["primary_arms"]:
                    paired = paired_effort((r.get("result", {}).get("official_success"), source_grades[r["task_id"]],
                                            effort.get(r["index"], {}), archive["by_task"].get(r["task_id"], {}).get("metrics", {}))
                                           for r in solves if r["arm"] == arm and r["task_id"] in tasks)
                    archive["by_task_set"][subset].append(dict(candidate=arm, reference="archived_first_pass", **paired))
        if archive is not None:
            archive["comparison_scope"] = "observed_across_passes"
            summary["first_pass_efficiency"] = archive
        summary.update(second_pass_efficiency=efficiency, effort_by_job=effort)
        index += ["", "## 같은 배정에서의 상담 효율", "",
                  "합계는 각 지표가 양쪽에서 관측된 쌍에 한한다. 비용·토큰·HTTP 요청은 해당 상담의 상담원과 고객 시뮬레이터 전체다. "
                  "메모 작성비는 별도다. 고객 발화는 종료 신호만 보낸 메시지를 제외한다. "
                  "1회차와 짝지은 합계는 반복마다 같은 원천 사용량을 참조한다. 회차별 비용 차이는 관측값이며 "
                  "입력 조건 검증 여부는 배정별 first_pass_comparability에 있다. 원천별 실제 사용량은 JSON의 by_task에 한 번씩 기록한다.", "",
                  "| 후보 / 기준 | 비교 범위 | 지표 | 관측 쌍 / 해당 쌍 | 후보 합계 | 기준 합계 | 차이 |",
                  "| --- | --- | --- | ---: | ---: | ---: | ---: |"]
        labels = dict(customer_turns="고객 발화", business_calls="업무 도구 호출", memory_read_calls="메모 조회 호출",
                      http_calls="모델 요청", tokens="토큰", usd="모델 요금 (USD)")
        all_comparisons = efficiency["all"] + (archive["by_task_set"]["all"] if archive else [])
        for pair in all_comparisons:
            for group, title in (("jointly_graded", "양쪽 채점"), ("both_success", "양쪽 성공")):
                view = pair[group]
                for metric, record in view["metrics"].items():
                    cells = [record[k] if record[k] is not None else "—" for k in ("candidate_total", "reference_total", "difference")]
                    index.append(f"| {pair['candidate']} / {pair['reference']} | {title} | {labels[metric]} | "
                                 f"{record['observed']}/{view['pairs']} | " + " | ".join(cells) + " |")
    if "candidate_proposal" in design:
        summary["candidate_proposal"] = design["candidate_proposal"]
    write_json(staged / "summary.json", summary)
    (staged / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    return summary


def publish_report(run, destination, *, annotations=None, with_behavior=False, source_workspace=None):
    run, destination = Path(run).resolve(), Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(run):
        raise ContractError("Choose a new evidence directory outside the sealed run")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as tmp:
        # Write at sibling depth so links from the evidence pages also point to the original journals.
        staged = Path(tmp)
        root = Path(source_workspace).resolve() if source_workspace is not None else run.parent.parent
        summary = _make_report(run, staged, destination, annotations, with_behavior, root)
        if destination.exists():
            raise ContractError("Another report already uses this destination")
        staged.rename(destination)
    return summary
