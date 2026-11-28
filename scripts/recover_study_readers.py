"""Resume only provider-503 readers listed in an approved sealed cohort supplement."""

import argparse
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
import subprocess
import time

from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.batch_recovery import stage_lease
from agent_memory_eval.experiments.forms_batch import endpoint_terms
from agent_memory_eval.experiments.forms_supplement import cooldown_seconds
from agent_memory_eval.recording.assets import asset_bytes
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.study.adapters import adapter_for
from agent_memory_eval.study.adapters.tau_banking import bank_shard, study_design, study_jobs, study_models
from agent_memory_eval.study.adapters.tau_cohort import bind_cohort_pools, cohort_catalog
from agent_memory_eval.study.adapters.tau_reader_stage import reader_batch_stages, reader_budget_check
from agent_memory_eval.study.orchestrator import shard_accounting
from agent_memory_eval.study.plan import load_plan
from agent_memory_eval.study.reader_stage import _context
from agent_memory_eval.study.records import fixed, read, sealed_rows
from agent_memory_eval.study.writer_stage import file_ref


POLICY = ROOT / "artifacts/approvals/own-mixed-readers-airline-http503-01.json"


def provider_failure(folder, row):
    """A received provider error and its server cooldown, bound to its last actual request."""
    result = row["result"]
    log = RecordedDecisions(safe_path(folder, row["directory"]), row["run_id"], result["log_sha256"])
    finished = log.of_type("model.request_finished")[-1]
    end = finished["payload"]
    start, = [r for r in log.requests if r["request_id"] == end["request_id"]]
    require(end["status"] == "error" and end["delivery_status"] == "response_received", "Missing received 503")
    response = log.blobs.get(end["response_ref"])["error"]
    require(response["code"] == 503 and result["error_code"] == "HTTP_503", "Missing provider 503 receipt")
    at = datetime.fromisoformat(finished["timestamp_utc"])
    return dict(role=start["actor_role"], at=at.timestamp(), retry_after=cooldown_seconds(response, 0, at),
                response_sha256=end["response_ref"]["sha256"])


def prepare(workspace, policy):
    plan = load_plan(safe_path(ROOT, policy["plan"]["path"]), ROOT)
    asset_bytes(ROOT, policy["plan"])
    batch = read_json(asset_bytes(ROOT, policy["batch"]))
    release = read_json(asset_bytes(ROOT, policy["parent_results"]))
    grant = read_json(asset_bytes(ROOT, policy["original_grant"]))
    pid, task = policy["plan_id"], policy["task"]
    part = batch["plans"][pid]
    bench = release["benchmarks"][policy["benchmark"]]
    require(policy["format"] == "agent-memory-reader-http503-supplement/v1" and policy["user_authorization"].strip()
            and pid == plan["id"] and task == bench["task"] and part["tasks"] == [task] and part["plan"] == policy["plan"]
            and part["benchmark"] == policy["benchmark"] and batch["id"] == release["batch"]
            and grant["batch_ref"] == policy["batch"] and policy["batch"]["sha256"] == release["batch_sha256"]
            and policy["cumulative_benchmark_max_usd"] == part["max_usd"] == grant["plan_caps_usd"][pid]
            and policy["cumulative_batch_max_usd"] == batch["max_usd"] == grant["max_usd"]
            and policy["http_status"] == 503 and policy["max_concurrent"] == 1, "Different recovery scope or budget")
    base = workspace / "runs" / pid
    parent = safe_path(base, "reader-batches/" + batch["id"])
    folder = safe_path(parent, "supplements/" + policy["id"])
    marker = read_json(asset_bytes(parent, dict(path="registration.json", sha256=bench["batch_registration_sha256"])))
    receipt = read_json(asset_bytes(parent, dict(path="result.json", sha256=policy["result_sha256"])))
    require(bench["result_sha256"] == policy["result_sha256"] and receipt["complete"]
            and marker["batch_ref"] == policy["batch"] and marker["approval_ref"] == policy["original_grant"]
            and receipt["reported_usd"] == bench["reported_usd"] and receipt["held_usd"] == bench["held_usd"]
            and receipt["controller_sha256"] == digest((parent / "controller/completion.json").read_bytes()),
            "Parent reader, receipt or controller changed")
    adapter = adapter_for(plan)
    _, scope, reg, resolved, sources = _context(plan, workspace, ROOT, adapter, ROOT / policy["batch"]["path"], None)
    require(all(marker[k] == v for k, v in scope.items()), "Released original reader prerequisites changed")
    cfg = dict(resolved["cfg"], effective_sources=sources)
    case, = [c for c in reg["preflight"]["cases"] if c.get("donor") and c["task_id"] == task]
    stages = reader_batch_stages(base)
    ck = read(base / "through-write.json")
    donors = [c for c in resolved["record"]["cases"] if "donor" not in c and c["task_id"] in case["pool_sources"]]
    cfg["cohort_catalog"] = cohort_catalog(workspace, base, cfg, donors, ck["stages"])
    retries = len(plan["retry"]["wait_seconds"])
    prefix = "task-" + case["task"]
    evidence = {}
    eligible = {r["condition"] for r in bench["missing_conditions"] if r["reason"] == "HTTP_503"}
    require(eligible == set(policy["conditions"]) and len(eligible) == len(policy["conditions"]), "Recovery eligibility changed")
    for i in range(retries + 1):
        name = prefix + ("/solve" if not i else f"/solve-retry-{i}")
        folder_i = safe_path(base, name)
        _, _, rows = sealed_rows(folder_i, stages[name]["seal"])
        for row in rows:
            arm = row["arm"]
            if arm not in eligible:
                continue
            result = row["result"]
            require(row["status"] == "error" and result["official_success"] is None, "A graded reader cannot be replaced")
            err = provider_failure(folder_i, row)
            require(err["role"] == policy["required_error_role"], "Different original failing provider role")
            evidence.setdefault(arm, []).append(dict(run=row["run_id"], session=result["session_id"], index=row["index"],
                    first_worker=result["first_worker_request_sha256"], error=err, log=result["log_sha256"]))
    original, = adapter._retry_cases(base, cfg, [case], "solve", retries + 1, {"HTTP_503"})
    bindings = original["interrupted_readers"]
    jobs = study_jobs(case, "solve")
    latest = {}
    for job in jobs:
        arm = job["arm"]
        if arm not in eligible:
            continue
        history = evidence[arm]
        require(len(history) == retries + 1 and len({h["first_worker"] for h in history}) == 1
                and history[-1]["first_worker"] and all(h["index"] == job["index"] for h in history)
                and bindings[arm]["index"] == job["index"], "Original arm, first worker input or attempt number changed")
        limited = dict(case, interrupted_readers={arm: bindings[arm]})
        design = study_design(cfg, limited, "solve")
        bind_cohort_pools(design, {}, limited, cfg["cohort_catalog"])
        latest[arm] = dict(condition=arm, task=task, run_id=history[-1]["run"], attempt=retries, success=None,
                           missing_reason="HTTP_503", first_worker_sha256=history[-1]["first_worker"], error=history[-1]["error"])
    require(set(bindings) == set(latest) == eligible, "Native 503 slots differ from the released missing slots")
    # Starting a separate grant must never borrow another benchmark's share.
    prior = Decimal(receipt["reported_usd"]) + Decimal(receipt["held_usd"])
    limit = min(Decimal(part["max_usd"]) - prior, Decimal(batch["max_usd"]) - Decimal(release["reported_usd"]) - Decimal(release["held_usd"]))
    require(limit > 0, "No unused reader allowance")
    common = dict(workspace=str(workspace), cfg=cfg, conn=resolved["conn"],
                  tasks={case["opening"]["task"]: resolved["tasks"][case["opening"]["task"]]})
    scope = dict(format="agent-memory-reader-supplement-registration/v1", policy=file_ref(ROOT, POLICY),
                 parent_result_sha256=policy["result_sha256"], parent_registration_sha256=bench["batch_registration_sha256"],
                 parent_implementation=marker["implementation"],
                 plan_id=pid, task=task, batch_id=batch["id"], prior_reported_usd=receipt["reported_usd"],
                 prior_held_usd=receipt["held_usd"], max_new_covered_usd=str(limit),
                 original_readers=evidence, original_bindings=bindings)
    return base, folder, plan, adapter, common, case, bindings, latest, scope


def execute(workspace, policy, prepared):
    base, folder, plan, adapter, common, case, bindings, latest, scope = prepared
    latest = dict(latest)
    common = dict(common)
    identity = implementation_identity(ROOT)
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / "registration.json"
    if marker.is_file():
        frozen = read(marker)["implementation"]
        if frozen != identity:
            from agent_memory_eval.study.reader_stage import _compatible_code
            _compatible_code(ROOT, frozen, identity)
        identity = frozen
    else:
        require(not any(folder.iterdir()), "Unregistered recovery evidence")
        other_grants = [d for d in folder.parent.iterdir() if d != folder and (d / "registration.json").exists()]
        require(not other_grants, "Another supplement already uses the original reader allowance")
        parent_commit = scope["parent_implementation"]["implementation_commit"]
        subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", parent_commit, identity["implementation_commit"],
                        "--", "src", "uv.lock", "pyproject.toml"], check=True)
    fixed(marker, dict(scope, implementation=identity))
    common["identity"] = identity
    cap = Decimal(scope["max_new_covered_usd"])
    reported = held = Decimal(0)
    scheduled, calls, streak = [], 0, 0
    seen_runs = {h["run"] for old in scope["original_readers"].values() for h in old}
    seen_sessions = {h["session"] for old in scope["original_readers"].values() for h in old}
    gap, delays = policy["minimum_inter_episode_seconds"], policy["provider_backoff_seconds"]
    not_before = max(prev["error"]["at"] + max(delays[0], prev["error"]["retry_after"]) for prev in latest.values())
    existing = sorted(folder.glob("attempt-*"))
    require(all(d.name == f"attempt-{n:04}" for n, d in enumerate(existing, 1)), "Non-contiguous recovery attempts")
    final = folder / "result.json"
    sealed_final = read(final) if final.exists() else None
    if sealed_final:
        require(len(existing) == len(sealed_final["attempts"]), "Closed recovery lost or gained an attempt")
    while any(prev["missing_reason"] == "HTTP_503" for prev in latest.values()):
        arm = min((a for a, v in latest.items() if v["missing_reason"] == "HTTP_503"),
                  key=lambda a: (latest[a]["attempt"], bindings[a]["index"]))
        prev = latest[arm]
        remaining = (cap - reported - held).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        n = len(scheduled) + 1
        directory = folder / f"attempt-{n:04}"
        # A pre-existing dispatch is restored with its original grant. Native recovery does not resend a started request.
        if not directory.exists():
            require(not sealed_final, "A closed recovery would send a new request")
            reader_budget_check(adapter, plan, common["cfg"], [case], remaining)
            time.sleep(max(0, not_before - time.time()))
            directory.mkdir()
        dispatch = directory / "dispatch.json"
        info = dict(index=n, condition=arm, task=case["task_id"], attempt=prev["attempt"] + 1, parent_run=prev["run_id"],
                    grant_usd=str(remaining), registration_sha256=digest(marker.read_bytes()))
        if not dispatch.is_file():
            require(not sealed_final and not any(directory.iterdir()), "Attempt lost its dispatch record")
        fixed(dispatch, info)
        shard = directory / ("task-" + case["task"]) / "solve"
        record_path = directory / "receipt.json"
        if not (shard / "completion.json").is_file():
            require(not record_path.exists() and not sealed_final, "A previously settled native shard is missing")
            if "models" not in common:
                models, grader = study_models(common["cfg"], common["conn"])
                live = dict(models, grader=grader) if grader is not None else models
                require(endpoint_terms(live) == common["cfg"]["expected_endpoint_terms"]
                        and {r: m["canonical_slug"] for r, m in models.items()} == common["cfg"]["expected_model_revisions"]
                        and (grader is None or grader["canonical_slug"] == common["cfg"]["judge"]["canonical_slug"]),
                        "Reader endpoint terms or revision changed")
                common.update(models=models, judge=grader)
            limited = dict(case, interrupted_readers={arm: bindings[arm]})
            check, = study_jobs(limited, "solve")
            require(check["arm"] == arm and check["index"] == bindings[arm]["index"], "Changed single-arm schedule")
            print(f"Dispatch {n}: {arm}, original-slot attempt {info['attempt']}", flush=True)
            bank_shard((dict(common, output=str(directory)), limited, "solve", str(remaining)))
        close, ctl, (row,) = sealed_rows(shard)
        result = row.get("result") or {}
        use = shard_accounting(shard, remaining)
        expected = bindings[arm]
        require(ctl["profile"]["registration_sha256"] == common["cfg"]["registration_sha256"]
                and ctl["implementation"] == identity and ctl["design"]["interrupted_readers"] == {arm: expected}
                and row["arm"] == arm and row["task_id"] == row["source_task_id"] == case["task_id"]
                and row["index"] == expected["index"] and ctl["design"]["delivery_arms"][arm]["sources_sha256"] == expected["sources_sha256"]
                and (not result.get("first_worker_request_sha256")
                     or result["first_worker_request_sha256"] == scope["original_readers"][arm][-1]["first_worker"]),
                "Recovery arm, task, wire or writer pool changed")
        success = result.get("official_success")
        require((type(success) is bool) == (result.get("grading_status") == "graded")
                and (success is None or result.get("first_worker_request_sha256")), "Unverifiable native grade")
        run, session = row["run_id"], result.get("session_id")
        require(run not in seen_runs and (not session or session not in seen_sessions), "Reused reader run or session")
        seen_runs.add(run)
        seen_sessions.add(session)
        missing = None if success is not None else result.get("error_code") or row.get("reason") or row["status"]
        if result.get("log_sha256"):
            require(digest(safe_path(shard, row["directory"]).joinpath("log.jsonl").read_bytes()) == result["log_sha256"], "Changed native reader journal")
        failure = provider_failure(shard, row) if missing == "HTTP_503" else None
        view = dict(condition=arm, task=case["task_id"], attempt=info["attempt"], parent_run=prev["run_id"], run_id=run,
                    session_id=session, folder=str(shard.relative_to(workspace)), seal=use["seal"], log_sha256=result.get("log_sha256"),
                    success=success, missing_reason=missing, reported_usd=use["reported"], held_usd=use["held"], requests=use["http"],
                    first_worker_sha256=result.get("first_worker_request_sha256"), error=failure, ended=close["ended_at"])
        if sealed_final:
            require(scheduled == sealed_final["attempts"][:n-1] and record_path.is_file(), "Closed native attempt receipt is missing")
            asset_bytes(workspace, sealed_final["attempts"][n-1])
        fixed(record_path, view)
        scheduled.append(file_ref(workspace, record_path))
        latest[arm] = view
        reported += Decimal(view["reported_usd"])
        held += Decimal(view["held_usd"])
        calls += view["requests"]
        ended = datetime.fromisoformat(view["ended"]).timestamp()
        not_before = ended + gap
        if failure:
            not_before = max(not_before, failure["at"] + max(delays[min(streak, len(delays)-1)], failure["retry_after"]))
            streak += 1
        else:
            streak = 0
        if n > len(existing):
            print(f"{arm}: {missing or 'graded'}, new reported USD {reported}, held {held}", flush=True)
    require(reported + held <= cap and len(existing) <= len(scheduled), "Unaccounted recovery work")
    result = dict(format="agent-memory-reader-supplement-result/v1", registration_sha256=digest(marker.read_bytes()),
                  attempts=scheduled, effective={k: v["run_id"] for k, v in latest.items()}, requests=calls,
                  reported_usd=str(reported), held_usd=str(held), complete=all(type(v["success"]) is bool for v in latest.values()))
    fixed(final, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--run", action="store_true", help="Execute the authorized missing slots; default is read-only preflight")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    policy = read(POLICY)
    prepared = prepare(workspace, policy)
    if args.run:
        with stage_lease(prepared[0]):
            result = execute(workspace, policy, prepared)
        print(result, flush=True)
    else:
        scope = prepared[-1]
        print(dict(conditions=list(prepared[-2]), max_new_covered_usd=scope["max_new_covered_usd"],
                   verified_original_ungraded_attempts=sum(map(len, scope["original_readers"].values()))))


if __name__ == "__main__":
    main()
