"""Register all native bank tasks in the cohort and bind reusable sources to sealed recordings."""

from collections import Counter, defaultdict
from decimal import Decimal

from agent_memory_eval.adapters.banking_trajectories import _shard, _study
from agent_memory_eval.analysis.clustered_outcomes import clustered_contrast
from agent_memory_eval.analysis.efficiency import episode_effort, paired_effort
from agent_memory_eval.analysis.recorded_decisions import READS, RecordedDecisions, require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.experiments.paired_repetitions import FORMS, MODES
from agent_memory_eval.recording.batch import sealed_rows
from agent_memory_eval.recording.journal import safe_path


def cohort_slots(spec, split, history, parent_plan):
    groups = spec["task_groups"]
    eligible = {task: meta for task, meta in split.items() if meta["group"] in groups}
    require(dict(Counter(meta["group"] for meta in eligible.values())) == groups
            and tuple(spec["forms"]) == FORMS and tuple(spec["deliveries"]) == MODES, "Cohort or assignments changed")
    confirmed = {r["customer"] for r in eligible.values() if r["group"] == "confirmation"}
    require(None not in confirmed and not confirmed.intersection(
        r["customer"] for r in eligible.values() if r["group"] != "confirmation"), "Confirmation customers overlap")
    draws = range(1, spec["source_draws_per_task"] + 1)
    expected = {(task, draw) for task, r in eligible.items() if r["group"] == "development" for draw in draws}
    past = {(r["task"], r["draw"]): r for r in history["observations"]}
    archived = {(c["task_id"], c["source_draw"]): c for c in parent_plan["cases"]}
    require(expected == past.keys() == archived.keys() and len(past) == len(history["observations"])
            and len(archived) == len(parent_plan["cases"]), "Duplicate, missing or extra archived draw")
    registration = digest(canonical(parent_plan))
    slots, missing = [], []
    for task, meta in sorted(eligible.items()):
        for draw in draws:
            name = f"{task}__source-{draw}"
            cell = dict(task=name, task_id=task, source_draw=draw, cohort=meta["group"],
                        customer=meta["customer"] or "unresolved-" + task, identity_basis=meta["identity_basis"])
            if (task, draw) in past:
                old = past[task, draw]
                require(archived[task, draw]["task"] == name and archived[task, draw]["customer"] == cell["customer"],
                        "Archived allocation identity changed")
                if old["status"] != "graded":
                    require(old["success"] is None, "An ungraded historical draw has a score")
                    missing.append(dict(cell, reason=old["reason"] or old["status"], run=old["run"]))
                    continue
                require(type(old["success"]) is bool, "Historical grade missing")
                cell["imported_first_pass"] = dict(directory=f"{history['binding']['recording']}/task-{name}/source",
                    registration_sha256=registration, run_id=old["run"], log_sha256=old["journal_sha256"],
                    public_context_sha256=old["public_experience_sha256"])
            slots.append(cell)
    imports = [c["imported_first_pass"] for c in slots if "imported_first_pass" in c]
    require(len(imports) == history["graded"] == len({r["run_id"] for r in imports})
            and len(slots) + len(missing) == len(eligible) * len(draws), "Repeated source or missing slot")
    return slots, missing


def historical_registration(workspace, history):
    binding = history["binding"]
    folder = safe_path(workspace / "runs", binding["recording"])
    registration = (folder / "registration.json").read_bytes()
    completion = (folder / "completion.json").read_bytes()
    require(digest(registration) == binding["registration_sha256"] == read_json(completion)["registration_sha256"]
            and digest(completion) == binding["completion_sha256"], "Historical source study changed")
    return read_json(registration)["preflight"], read_json(completion)


def first_release(cases):
    first = {}
    for case in sorted(cases, key=lambda c: (c["task_id"], c["source_draw"])):
        if "imported_first_pass" in case:
            first.setdefault(case["task_id"], case["task"])
    return set(first.values())


def allocation_counts(cases, repeat):
    imported = sum("imported_first_pass" in c for c in cases)
    banks = len(cases) * len(FORMS)
    return dict(reused_first_pass=imported, source=len(cases) - imported, write=banks,
                solve=banks * len(MODES) * repeat, second_pass_no_memory=0)


def observation(folder, row, *, writer=None):
    """Audit one native episode, including delivered input and official outcome, using its sealed journal."""
    result = row.get("result") or {}
    obs = dict(run=row.get("run_id"), status=row["status"], reason=row.get("reason") or result.get("error_code"),
               journal=result.get("log_sha256"), success=None, effort={}, dispatched_worker=0,
               body_requests=0, title_requests=0, reads={}, returns={})
    if not obs["journal"]:
        require(result.get("official_success") is None, "Unsealed grade")
        return obs
    log = RecordedDecisions(safe_path(folder, row["directory"]), row["run_id"], obs["journal"])
    require(log.start["fresh_session"], "Shared solver session")
    if row["arm"] == "no_memory":
        require(not log.items and not log.start["source_run_ids"], "Baseline received prior memory")
    else:
        require(writer and log.start["source_run_ids"] == [writer["run_id"]] and log.items == writer["items"]
                and log.state["assignments"][log.run]["input_items_sha256"] == digest(canonical(writer["items"])),
                "Reader used another bank")
        obs.update(writer=writer["run_id"], writer_snapshot=writer["snapshot_id"])
    reads, choices = Counter(), []
    for req in log.requests:
        if req["actor_role"] != "worker":
            continue
        end = log.finishes[req["request_id"]]
        sent = end["delivery_status"] != "not_submitted"
        if sent:
            fields = {s["field"] for s in log.visible(req, log.blobs.get(req["request_ref"]))}
            if not obs["dispatched_worker"]:
                obs.update(first_worker=req["request_ref"]["sha256"], first_fields=sorted(fields))
            obs["dispatched_worker"] += 1
            obs["title_requests"] += "title" in fields
            obs["body_requests"] += "body" in fields
        calls = []
        if end["status"] == "response":
            calls = log.reply(req)["message"].get("tool_calls") or []
        private = [c["function"]["name"] for c in calls if c["function"]["name"] in READS]
        reads.update(private)
        choices.append(dict(status=end["status"], delivery=end["delivery_status"], reads=private))
    obs.update(reads=dict(reads), returns=dict(Counter(e["payload"]["operation"] for e in log.of_type("memory.returned"))))
    grade, = [e["payload"] for e in log.of_type("outcome.scored")]
    require(grade["official_success"] is result["official_success"] and grade["grading_status"] == result["grading_status"]
            and grade["official_artifact_ref"] == result["report_ref"], "Native grade differs from the journal")
    messages = None
    if grade["grading_status"] == "graded":
        official = log.blobs.get(grade["official_artifact_ref"])
        require(official["task_id"] == row["task_id"] and official["reward_info"]["reward"] == grade["official_score"]
                and (grade["official_score"] == 1) is grade["official_success"], "Official artifact disagrees")
        messages = official["messages"]
        obs.update(success=grade["official_success"], report=grade["official_artifact_ref"]["sha256"])
    obs["effort"] = episode_effort(row, messages, choices)
    for entry in log.of_type("business.transition"):
        p = entry["payload"]
        if p.get("origin") == "task_start.first_decision":
            obs.update(start=p["task_start_ref"]["sha256"], start_source=p["source_run_id"])
    return obs


def arm_summary(readers):
    seen = [r for r in readers if r["dispatched_worker"]]
    graded = [r for r in readers if r["success"] is not None]
    return dict(assigned=len(readers), dispatched=len(seen), graded=len(graded), successes=sum(r["success"] for r in graded),
                with_body=sum(bool(r["body_requests"]) for r in seen), with_title=sum(bool(r["title_requests"]) for r in seen),
                body_at_open=sum("body" in r["first_fields"] for r in seen),
                list_attempts=sum(r["reads"].get("memory_list", 0) for r in seen),
                get_attempts=sum(r["reads"].get("memory_get", 0) for r in seen),
                body_returns=sum(r["returns"].get("get", 0) for r in seen),
                tasks_exposed=len({r["task"] for r in seen if r["body_requests"]}))


def release_check(arms, rules):
    auto, optional = (arms[mode] for mode in MODES)
    failures = []
    if any(not a["assigned"] or a["graded"] < a["assigned"] * rules["minimum_graded_fraction_per_delivery"] for a in arms.values()):
        failures.append("grade_coverage")
    if rules["require_every_dispatched_preload_has_body"] and auto["dispatched"] != auto["body_at_open"]:
        failures.append("preload_body_coverage")
    if optional["tasks_exposed"] < rules["minimum_distinct_tasks_with_on_demand_body"]:
        failures.append("optional_body_task_coverage")
    return dict(meets_readiness_targets=not failures, reasons=failures)


def outcome_tables(cells):
    index = {(r["task"], r["draw"], r["form"], r["repeat"], r["delivery"]): r for r in cells}
    require(len(index) == len(cells), "Duplicate reader")
    arms, clusters, deliveries = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in cells:
        key = f"{r['form']}__{r['delivery']}"
        source = r["source"]
        arms[key].append((r["success"], source["success"], r["effort"], source["effort"]))
        if r["delivery"] == "preload":
            other = index[r["task"], r["draw"], r["form"], r["repeat"], "on_demand"]
            require(r.get("expected_writer") == other.get("expected_writer"), "Delivery contrast used different memories")
            subgroup = "confirmation" if r["cohort"] == "confirmation" else "development"
            slot = (r["draw"], r["repeat"])
            clusters[subgroup].append((r["customer"], r["task"], r["form"], slot, other["success"], r["success"]))
            deliveries[subgroup].append((r["success"], other["success"], r["effort"], other["effort"]))
    return dict(versus_first_pass={k: paired_effort(v) for k, v in sorted(arms.items())},
                delivery_pairs={k: paired_effort(v) for k, v in sorted(deliveries.items())},
                preload_minus_on_demand={k: clustered_contrast(v, cluster_unit="customer" if k == "confirmation"
                    else "identified customer or unresolved task") for k, v in sorted(clusters.items())})


def read_release(workspace, frozen, release):
    root = workspace / "runs" / (frozen["series"] + "-" + release)
    plan, close, binding = _study(root, frozen["protocol_sha256"])
    require(all(plan[k] == v for k, v in frozen.items()) and plan["release"] == release, "Different cohort release")
    registration = digest(canonical(plan))
    sources, writers, readers = [], [], []
    for case in plan["cases"]:
        ref = case.get("imported_first_pass")
        if ref:
            folder = safe_path(workspace / "runs", ref["directory"])
            _, _, (row,) = sealed_rows(folder)
            require(row["run_id"] == ref["run_id"] and row["result"]["log_sha256"] == ref["log_sha256"], "Changed source receipt")
        else:
            folder, (row,) = _shard(root, case, "source", "source", close, registration)
        src = observation(folder, row)
        sources.append(dict(src, task=case["task_id"], draw=case["source_draw"], reused=bool(ref)))
        wdir, written = _shard(root, case, "write", "write", close, registration)
        banks = {}
        for w in written:
            res = w.get("result") or {}
            if w["status"] == "completed":
                bank = read_form_bank(safe_path(wdir, w["directory"]), expected_log_sha256=res["log_sha256"])
                require(len(bank["items"]) == 1 and (bank["source_run_id"], bank["source_log_sha256"], bank["result_feedback"]["success"])
                        == (src["run"], src["journal"], src["success"]) and bank["arm"] == w["arm"], "Wrong writer source or feedback")
                banks[w["arm"]] = bank
            writers.append(dict(task=case["task_id"], draw=case["source_draw"], form=w["arm"], status=w["status"],
                                reason=w.get("reason") or res.get("error_code"), run=w.get("run_id"),
                                journal=res.get("log_sha256"), snapshot=res.get("snapshot_id"), source=src["run"]))
        for repeat, stage in enumerate([s for s in case["scheduled_stages"] if s.startswith("solve")], 1):
            folder, rows = _shard(root, case, stage, "solve", close, registration)
            for row in rows:
                form, mode = row["arm"].split("__")
                bank = banks.get(form)
                obs = observation(folder, row, writer=bank)
                if obs["dispatched_worker"]:
                    require(obs.get("start_source") == src["run"] and obs.get("start") == src.get("start"),
                            "Reader first public decision differs from its source")
                readers.append(dict(obs, task=case["task_id"], draw=case["source_draw"], repeat=repeat, form=form,
                                    delivery=mode, cohort=case["cohort"], customer=case["customer"],
                                    expected_writer=bank["run_id"] if bank else None,
                                    source={k: src[k] for k in ("run", "success", "effort")}))
    expected = plan["assigned"]
    require(len(sources) == expected["source"] + expected["reused_first_pass"] and len(writers) == expected["write"]
            and len(readers) == expected["solve"], "Unaccounted cohort assignment")
    costs = defaultdict(lambda: defaultdict(Decimal))
    for path, v in close["stages"].items():
        stage = path.split("/")[-1].split("-")[0]
        for field in ("reported", "held", "spent"):
            costs[stage][field] += Decimal(v[field])
    spend = sum((v["spent"] for v in costs.values()), Decimal(0))
    require(spend + Decimal(plan["prior_covered_usd"]) == Decimal(close["total_covered_usd"])
            <= Decimal(plan["authorization"]["total_usd"]), "Cohort budget does not reconcile")
    return dict(binding=binding, prior_binding=plan["prior_release"], prior_covered_usd=plan["prior_covered_usd"],
                by_stage_usd={k: {f: str(s) for f, s in v.items()} for k, v in costs.items()},
                spent=str(spend), sources=sources, writers=writers, readers=readers)


def cohort_results(workspace, frozen, rules, *, full):
    releases = ["first", "remainder"] if full else ["first"]
    raw = {name: read_release(workspace, frozen, name) for name in releases}
    if full:
        require(raw["remainder"]["prior_binding"] == raw["first"]["binding"]
                and Decimal(raw["remainder"]["prior_covered_usd"]) == Decimal(raw["first"]["spent"]),
                "Remainder was not bound to this initial release and its spending")
    readers = [r for release in raw.values() for r in release["readers"]]
    deliveries = {mode: arm_summary([r for r in readers if r["delivery"] == mode]) for mode in MODES}
    first_readers = raw["first"]["readers"]
    check = release_check({mode: arm_summary([r for r in first_readers if r["delivery"] == mode]) for mode in MODES}, rules)
    return dict(format="banking-memory-cohort-results/v1", scope=releases, historical_missing=frozen["historical_missing"],
                releases=raw, tables=outcome_tables(readers), delivery=deliveries, first_release_check=check)
