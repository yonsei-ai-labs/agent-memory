"""Build an offline, browsable comparison from verified writing and native episode journals."""
from collections import Counter, defaultdict, deque
from decimal import Decimal
from itertools import combinations, zip_longest
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from agent_memory_eval.analysis.forms_actions import check_judgment
from agent_memory_eval.analysis.forms_results import checked_rows, contrast
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
    return dict(db_match=(reward.get("db_check") or {}).get("db_match"), communication=required,
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
        lines += ["", "참조 행동과의 일치 여부도 native 결과에 기록됩니다. 이 항공 실험의 공식 점수는 최종 DB와 필수 안내를 사용합니다.", ""]
        lines += [f"- `{a['name']}` · {match[a['matched']]}" for a in grade["reference_actions"]]
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


def _make_report(run, staged, destination, annotation_path):
    pages = staged / "evidence"
    pages.mkdir()
    sources, writers, evidence, grades = {}, {}, {}, {}
    retrieval = {}
    def observe(row, folder, scan, state, blobs):
        evidence[row["index"]] = _job_evidence(row, folder, scan, state, blobs, sources, writers, pages, grades=grades)
        if row["phase"] == "solve":
            run_id = row["run_id"]
            cfg = state["assignments"][run_id]
            events = [e for e in scan["events"].values() if e["run_id"] == run_id]
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
    def observe_parent(row, folder, scan, state, blobs):
        _job_evidence(row, folder, scan, state, blobs, sources, writers, pages, grades, imported=True)
    design, completion, rows, usage = checked_rows(run, observe_job=observe, observe_parent=observe_parent)
    # Both stage and destination are siblings, so relative file links remain valid on publication.
    source_grades = read_json((run / "control.json").read_bytes())["source_outcomes"]
    solves = [r for r in rows if r["phase"] == "solve"]
    by_cell = {(r["task_id"], r["repetition"], r["arm"]): r for r in solves}
    groups = {r["task_id"]: r["source_group"] for r in design["source"]["cohort"]}
    annotations = {}
    if annotation_path:
        raw = read_json(Path(annotation_path).read_bytes())
        annotations = {(x["treatment_job"], x["control_job"]): x for x in raw["pairs"].values()}
    if design.get("delivery_arms"):
        ids = { (c["writer"], c["read_mode"]): arm for arm, c in design["delivery_arms"].items() }
        a, b = [c["id"] for c in design["conditions"]]
        comparisons = [(ids[w, "on_demand"], ids[w, "preload"]) for w in (a, b)]
        comparisons += [(ids[b, mode], ids[a, mode]) for mode in ("preload", "on_demand")]
        sets = {"all": list(groups), **design["task_sets"]}
    elif design.get("format") == "agent-memory-new-customer-design/v1":
        if design["stage"] != "comparison":
            raise ContractError("Choose the complete comparison run for the paired report")
        forms = design["second_pass"]["primary_arms"]
        comparisons = [(a, c) for a in forms for c in design["second_pass"]["matched_controls"]]
        comparisons += list(combinations(forms, 2)) + [("empty_preload", "no_memory")]
        sets = {"all": list(groups), **design["task_sets"]}
    elif "conditions" in design:
        a, b = [c["id"] for c in design["conditions"]]
        comparisons = [(b, a), (a, "no_memory"), (b, "no_memory"), (a, "empty_preload"), (b, "empty_preload")]
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
    index = ["# 두 회차 기억 비교", "", f"2회차 배정 {len(solves)}건 · 공식 성공 {outcomes['성공']}건 · "
             f"공식 실패 {outcomes['실패']}건 · 미채점 {outcomes['미채점']}건 · 미시작 {outcomes['미시작']}건", "",
             "[설정·성적·비용 전체 표](summary.json) · " +
             _link("실행 당시 명세", destination, run / "control.json"), "",
             "## 과제 집합별 결과", "",
             "| 과제 집합 | 비교 조건 / 기준 조건 | 함께 채점 | 개선 | 악화 | 모두 성공 | 모두 실패 | 결측 | 과제 평균 차이 (%p) |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for subset, reports in contrasts.items():
        for c in reports:
            n = c["counts"]
            index.append(f"| {subset} | {c['treatment']} / {c['control']} | {c['jointly_graded_pairs']} | {n['gain']} | "
                         f"{n['loss']} | {n['both_success']} | {n['both_failure']} | {n['ungraded']} | {c['task_equal_difference_pp']} |")
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
                   costs_by_condition=dict(cost), usage_by_role=usage, completion=completion,
                   retrieval_by_job=retrieval, native_grade_components=grades, writer_parent=design.get("writer_parent"),
                   verified_paired_annotations=len(annotations), paired_case_pages=len(linked))
    write_json(staged / "summary.json", summary)
    (staged / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    return summary


def publish_report(run, destination, *, annotations=None):
    run, destination = Path(run).resolve(), Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(run):
        raise ContractError("Choose a new evidence directory outside the sealed run")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as tmp:
        # Write at sibling depth so links from the evidence pages also point to the original journals.
        staged = Path(tmp)
        summary = _make_report(run, staged, destination, annotations)
        if destination.exists():
            raise ContractError("Another report already uses this destination")
        staged.rename(destination)
    return summary
