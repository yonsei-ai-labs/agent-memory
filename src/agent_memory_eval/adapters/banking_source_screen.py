"""Select development sources and measure public retrieval opportunities in native banking tasks."""

from collections import Counter, defaultdict
from decimal import Decimal
import re

from agent_memory_eval.adapters.banking_trajectories import _searches, _shard, _study
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.analysis.trajectory import recorded_effort, total_effort
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.experiments.forms_write import _project_context
from agent_memory_eval.recording.journal import safe_path


def contains(text, value):
    return bool(re.search(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", text))


def partition(historical, tasks, customers):
    """Use only pre-outcome records. Keep unidentified development tasks explicit."""
    rows = {r["task"]: r for r in historical}
    require(rows.keys() == tasks.keys() and len(rows) == len(historical), "Task inventory changed")
    seen = {r["unit"] for r in historical if r["identified"] and r["room"] != "not_run"}
    reserved = {r["unit"] for r in historical if r["identified"]} - seen
    require(reserved <= customers.keys(), "Missing reserved customer in the base database")
    split = {}
    for tid, row in sorted(rows.items()):
        task = tasks[tid]
        scenario = task["user_scenario"]["instructions"].casefold()
        raw = canonical(task).decode().casefold()
        aliases = sorted(uid for uid in reserved if contains(raw, uid) or contains(scenario, customers[uid]["name"].casefold()))
        identified = row["unit"] if row["identified"] else None
        identity_basis = "base_db" if identified else "unresolved"
        state = task.get("initial_state") or {}
        initial = (state.get("initialization_data") or {}).get("agent_data") or {}
        local_users = (initial.get("users") or {}).get("data", {})
        if identified is None:
            verified = {a["arguments"].get("user_id") for a in task["evaluation_criteria"]["actions"]
                        if a["name"] == "log_verification"} & local_users.keys()
            if len(verified) == 1:
                identified, = verified
                identity_basis = "reset_db_verification"
        if row["room"] != "not_run":
            require(not aliases, "A proposed confirmation customer appeared in past development")
            group = "previous"
        elif row["unit"] in reserved:
            group = "confirmation"
        elif aliases:
            group = "possible_overlap"
        elif (set(task["evaluation_criteria"]["reward_basis"]) not in ({"DB"}, {"ACTION"})
              or task["evaluation_criteria"].get("nl_assertions")):
            group = "unsupported_grading"
        else:
            require(not state.get("message_history") and not state.get("initialization_actions"),
                    "Development task has a pre-existing conversation or business action")
            group = "development"
        split[tid] = dict(group=group, customer=identified, identity_basis=identity_basis, reserved_matches=aliases)
    return split


def document_opportunity(required, first_wire, public_messages):
    """Only linked, writer-visible grep tool returns count as past evidence."""
    calls, returns = _searches(public_messages)
    known = {doc for r in returns.values() for doc in r["documents"] or []}
    complete = calls.keys() == returns.keys() and all(r["documents"] is not None for r in returns.values())
    present = {doc for doc in required if contains(canonical(first_wire).decode(), doc)}
    source_only = (known & required) - present
    return dict(observed=True if source_only else (False if required and complete else None),
                required=sorted(required), in_first_request=sorted(present), source_only=sorted(source_only),
                complete_search_history=complete, search_calls=len(calls),
                returns=[dict(call=cid, sha256=r["sha256"], required=sorted(required.intersection(r["documents"] or [])))
                         for cid, r in returns.items()])


def source_observation(folder, row, case, required):
    tid = case["task_id"]
    result = row.get("result") or {}
    obs = dict(status=row["status"], success=None, run=row.get("run_id"),
               reason=row.get("reason") or result.get("error_code"), effort=None, opportunity=None)
    if not result.get("log_sha256"):
        require(result.get("official_success") is None, "Unsealed grade")
        return obs
    log = RecordedDecisions(safe_path(folder, row["directory"]), row["run_id"], result["log_sha256"])
    start, = log.of_type("run.started")
    require(start["condition_id"] == "no_memory" and log.start["fresh_session"] and not log.start["source_run_ids"] and not log.start["bank_id"]
            and not log.start["start_snapshot_id"] and not any(e["type"].startswith("memory.") for e in log.events.values()),
            "The first pass contains prior experience or memory")
    obs["session"] = start["session_id"]
    if log.requests:
        control, = [e["payload"]["control_ref"] for e in log.of_type("business.transition")
                    if e["payload"].get("origin") == "matched_trial.native_control"]
        first_customer = log.requests[0]
        require(control["sha256"] == case["native_sha256"] and first_customer["actor_role"] == "user_simulator"
                and first_customer["request_ref"]["sha256"] == case["customer_opening_sha256"],
                "Opening customer request or native controls differ from the registered task")
        obs["customer_opening_sha256"] = first_customer["request_ref"]["sha256"]
    grade, = [e["payload"] for e in log.of_type("outcome.scored")]
    require(result["task_id"] == tid and grade["official_success"] is result["official_success"]
            and grade["official_artifact_ref"] == result["report_ref"]
            and grade["grading_status"] == result["grading_status"], "Source result differs from journal")
    obs.update(status=grade["grading_status"], journal_sha256=result["log_sha256"], effort=recorded_effort(log))
    if grade["grading_status"] != "graded":
        return obs
    report = log.blobs.get(grade["official_artifact_ref"])
    require(report["task_id"] == tid and report["reward_info"]["reward"] == grade["official_score"]
            and (report["reward_info"]["reward"] == 1) is grade["official_success"], "Native grade mismatch")
    worker = [r for r in log.requests if r["actor_role"] == "worker"]
    first, last = worker[0], worker[-1]
    public = _project_context(log.blobs.get(last["request_ref"]), log.blobs.get(log.finishes[last["request_id"]]["response_ref"]))
    obs.update(success=grade["official_success"], report_sha256=grade["official_artifact_ref"]["sha256"],
               first_worker_sha256=first["request_ref"]["sha256"], public_experience_sha256=digest(canonical(public)),
               opportunity=document_opportunity(required, log.blobs.get(first["request_ref"]), public["messages"]))
    return obs


def summarize_task(rows, repeats):
    require(sorted(r["draw"] for r in rows) == list(range(1, repeats + 1)), "Missing or duplicate source draw")
    graded = [r for r in rows if r["success"] is not None]
    wins = sum(r["success"] for r in graded)
    positive = [r["draw"] for r in graded if r["opportunity"]["observed"] is True]
    if len(graded) < repeats:
        pattern = "incomplete"
    elif wins == 0:
        pattern = "all_failure"
    elif wins == repeats:
        pattern = "all_success"
    else:
        pattern = "mixed"
    return dict(graded=len(graded), successes=wins, pattern=pattern, positive_draws=positive,
                candidate=pattern == "mixed" and bool(positive))


def screen_report(root, protocol_sha, frozen, domain, repeats):
    plan, close, binding = _study(root, protocol_sha)
    require({k: plan[k] for k in frozen} == frozen, "Cohort differs from preflight")
    cases = plan["cases"]
    require(digest(canonical(cases)) == frozen["cases_sha256"] and
            set(close["stages"]) == {f"task-{c['task']}/source" for c in cases}, "Changed screening allocation")
    registration = digest(canonical(plan))
    observations, by_task = [], defaultdict(list)
    for case in cases:
        tid = case["task_id"]
        path, (row,) = _shard(root, case, "source", "source", close, registration)
        task_bytes = (domain / "tasks" / (tid + ".json")).read_bytes()
        require(digest(task_bytes) == frozen["task_files_sha256"][tid], "Native target annotations changed")
        obs = source_observation(path, row, case, set(read_json(task_bytes).get("required_documents") or []))
        obs.update(task=tid, draw=case["source_draw"])
        observations.append(obs)
        by_task[tid].append(obs)
    runs = [o["run"] for o in observations if o["run"]]
    sessions = [o["session"] for o in observations if "session" in o]
    require(len(runs) == len(set(runs)) and len(sessions) == len(set(sessions)), "Two draws share a run or session")
    tasks = {tid: summarize_task(items, repeats) for tid, items in sorted(by_task.items())}
    cost = {key: sum((Decimal(val[key]) for val in close["stages"].values()), Decimal(0))
            for key in ("reported", "held", "spent")}
    require(cost["reported"] + cost["held"] == cost["spent"] == Decimal(close["total_covered_usd"])
            <= Decimal(plan["authorization"]["total_usd"]), "Screen cost does not reconcile")
    return dict(format="agent-memory-banking-source-screen-results/v1", binding=binding,
                protocol_sha256=protocol_sha, assigned=len(observations),
                graded=sum(t["graded"] for t in tasks.values()), successes=sum(t["successes"] for t in tasks.values()),
                patterns=dict(Counter(t["pattern"] for t in tasks.values())),
                candidates=[tid for tid, t in tasks.items() if t["candidate"]],
                effort=total_effort(observations), costs_usd={k: str(v) for k, v in cost.items()},
                tasks=tasks, observations=observations)
