"""Run the approved no-memory HTTP 503 replacements against sealed first-pass checkpoints."""
import argparse
from datetime import datetime
from decimal import Decimal
import os
from pathlib import Path
import time

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.batch_recovery import stage_lease
from agent_memory_eval.experiments.forms_batch import endpoint_terms
from agent_memory_eval.experiments.forms_supplement import cooldown_seconds
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.study.adapters import adapter_for
from agent_memory_eval.study.adapters.bfcl_transfer import cohort_checkpoint, run_events, run_key, terminal
from agent_memory_eval.study.adapters.tau_banking import bank_shard, study_models
from agent_memory_eval.study.orchestrator import run_stages, shard_accounting, verify_stage_checkpoints
from agent_memory_eval.study.plan import load_plan, plan_digest, require_declared_models
from agent_memory_eval.study.records import fixed, read, sealed_rows, source_from

POLICY = "artifacts/approvals/own-mixed-source-http503-01.json"
WALLET = "source-http503-recovery"


def retry_receipt(folder, row):
    result = row["result"]
    journal = safe_path(folder, row["directory"])
    require(digest((journal / "log.jsonl").read_bytes()) == result["log_sha256"], "Source journal changed")
    scan, _, _ = read_experiment(journal)
    blobs = BlobStore(journal)
    for event in reversed(run_events(scan, row["run_id"])):
        p = event["payload"]
        if (event["type"] != "model.request_finished" or p["status"] != "error"
                or p["delivery_status"] != "response_received" or not p.get("response_ref")):
            continue
        error = blobs.get(p["response_ref"]).get("error", {})
        if str(error.get("code")).removeprefix("HTTP_") == "503":
            at = datetime.fromisoformat(event["timestamp_utc"])
            return dict(request_id=p["request_id"], sha256=p["response_ref"]["sha256"], at=at.isoformat(),
                        retry_after_seconds=cooldown_seconds(error, 0, at))
    raise ValueError("HTTP_503 has no matching submitted provider receipt")


def source_state(workspace, folder, seal, task, registration_hash):
    _, ctl, (row,) = sealed_rows(folder, seal)
    require(ctl["profile"]["registration_sha256"] == registration_hash and ctl["design"]["stage"] == "source"
            and row["arm"] == "no_memory" and row["task_id"] == task, "Source allocation changed")
    r = row.get("result") or {}
    success = r.get("official_success")
    require((type(success) is bool) == (r.get("grading_status") == "graded"), "Inconsistent native source grade")
    state = dict(task=task, folder=str(folder.relative_to(workspace)), seal=seal, run_id=row.get("run_id"),
                 success=success, missing=None if success is not None else r.get("error_code") or row.get("reason") or row["status"])
    if state["missing"] == "HTTP_503":
        state["receipt"] = retry_receipt(folder, row)
    return state


def confirmed_opening(folder, seal, expected):
    _, _, (row,) = sealed_rows(folder, seal)
    r = row.get("result") or {}
    if not r.get("http_calls"):
        return None
    journal = safe_path(folder, row["directory"])
    require(digest((journal / "log.jsonl").read_bytes()) == r["log_sha256"], "Retry journal changed")
    scan, _, _ = read_experiment(journal)
    opening = next(e["payload"] for e in run_events(scan, row["run_id"]) if e["type"] == "model.request_started")
    require(opening["actor_role"] == "user_simulator" and opening["request_ref"]["sha256"] == expected,
            "Retry did not start from the registered no-memory customer request")
    return expected


def check_originals(workspace, policy):
    originals = []
    total = Decimal(0)
    for item in policy["plans"]:
        plan = load_plan(ROOT / item["plan"], ROOT)
        require_declared_models(plan)
        base = workspace / "runs" / plan["id"]
        reg = read(base / "registration.json")
        grant_file = ROOT / item["approval"]
        grant = read(grant_file)
        require(digest(grant_file.read_bytes()) == item["approval_sha256"] and grant == reg["approval"]
                and grant["through_stage"] == "source" and grant["plan_sha256"] == plan_digest(plan)
                and digest(canonical(reg["preflight"])) == grant["resolved_sha256"],
                "The source plan, resolved allocation or spending grant changed")
        require((base / "through-source.json").is_file() and not (base / "through-write.json").exists()
                and not (base / "completion.json").exists(), "Finish the four source phases before replacing HTTP 503s")
        total += Decimal(grant["max_usd"])
        if plan["benchmark"]["adapter"] == "bfcl-relevance":
            # This run had already finished cleanly when the user approved reattempting 503s.
            require(digest((base / "through-source.json").read_bytes()) == item["completed_checkpoint_sha256"],
                    "Registered BFCL source checkpoint changed")
            journals = {j["directory"]: j["tasks"] for j in reg["preflight"]["cohort_journals"]}
            cohort_checkpoint(base, journals, "source")
            graded = []
            for name, tasks in journals.items():
                scan, _, _ = read_experiment(base / name)
                for task in tasks:
                    run = run_key(plan["id"], task, 1, "source")
                    events = run_events(scan, run)
                    require(terminal(events)["grading_status"] == "graded" and
                            any(e["type"] == "outcome.scored" and type(e["payload"]["official_success"]) is bool
                                for e in events), "BFCL source needs recovery review")
                    graded.append(task)
            require(sorted(graded) == sorted(plan["benchmark"]["tasks"])
                    and len(graded) == item["verified_graded_sources"], "BFCL task census changed")
            continue
        verify_stage_checkpoints(base, digest((base / "registration.json").read_bytes()))
        originals.append((plan, base, reg))
    require(total == Decimal(policy["cumulative_max_usd"]) and len({p["id"] for p, _, _ in originals}) == len(originals),
            "Source grants do not match the approved combined ceiling")
    return originals


def recover(workspace, policy, plan, base, reg, identity):
    source_ck = base / "through-source.json"
    ck = read(source_ck)
    record = reg["preflight"]
    cases = {f"task-{c['task']}/source": c for c in record["cases"] if "source" in c["scheduled_stages"]}
    require(set(cases) == set(ck["stages"]) and len(cases) == len(plan["benchmark"]["tasks"]),
            "The original first pass is incomplete")
    registration_hash = reg["approval"]["resolved_sha256"]
    current = {}
    prior = Decimal(0)
    for key, case in cases.items():
        entry = ck["stages"][key]
        if entry.get("status") == "not_started":
            require(entry["http"] == 0 and Decimal(entry["spent"]) == 0, "Unstarted task has spending")
            continue
        folder = safe_path(base, key)
        _, control, _ = sealed_rows(folder, entry["seal"])
        cost = shard_accounting(folder, str(control["profile"]["guards"]["max_usd"]))
        require(cost["seal"] == entry["seal"] and cost["http"] == entry["http"]
                and all(Decimal(cost[k]) == Decimal(entry[k]) for k in ("reported", "held", "spent")),
                "Source checkpoint costs do not match the sealed ledger")
        current[key] = source_state(workspace, folder, entry["seal"], case["task_id"], registration_hash)
        prior += Decimal(cost["spent"])
    require(prior == Decimal(ck["total_covered_usd"]), "Original first-pass totals changed")
    ceiling = Decimal(reg["approval"]["max_usd"])
    require(prior <= ceiling, "Original spending exceeds its approval")
    rounds = []
    models_ready = False
    wallet = base / WALLET
    existing = sorted(wallet.glob("pass-*"))
    for n, directory in enumerate(existing, 1):
        require(directory.name == f"pass-{n:04}", "Source recovery pass is missing")
        if n < len(existing):
            require((directory / "through-source.json").is_file(), "A later pass needs a sealed predecessor")
    published = wallet / "effective-sources.json"
    if published.exists():
        saved = read(published)
        require(saved["original_checkpoint_sha256"] == digest(source_ck.read_bytes())
                and [str(p.relative_to(workspace)) for p in existing] == [r["directory"] for r in saved["passes"]],
                "Completed source recovery has different passes")
        for p, ref in zip(existing, saved["passes"], strict=True):
            require(digest((p / "through-source.json").read_bytes()) == ref["checkpoint_sha256"],
                    "Sealed source recovery checkpoint changed")
            verify_stage_checkpoints(p, digest((p / "registration.json").read_bytes()))
    delays = policy["provider_backoff_seconds"]
    round_number = 1
    consecutive = 0
    while True:
        eligible = {key: s for key, s in current.items() if s["missing"] == "HTTP_503"}
        if not eligible:
            require(round_number > len(existing), "A recovery pass has no eligible predecessor")
            if rounds:
                fixed(wallet / "effective-sources.json", dict(original_checkpoint_sha256=digest(source_ck.read_bytes()),
                      cumulative_covered_usd=str(prior), passes=rounds, sources=list(current.values())))
            return
        if not models_ready:
            adapter = adapter_for(plan)
            resolved = adapter.resolve(plan, workspace, ROOT)
            require(resolved["record"] == record, "The native tasks or source controls changed")
            run_cases = {f"task-{c['task']}/source": c for c in resolved["cases"] if "source" in c["scheduled_stages"]}
            require(run_cases.keys() == cases.keys(), "Native retry cases differ from the source registration")
            cfg = resolved["cfg"]
            require(os.environ.get("OPENROUTER_API_KEY"), "OpenRouter credentials are missing")
            models, judge = study_models(cfg, resolved["conn"])
            terms = endpoint_terms(dict(models, grader=judge) if judge is not None else models)
            require(terms == cfg["expected_endpoint_terms"] == reg["endpoint_terms"]
                    and {k: m["canonical_slug"] for k, m in models.items()} == cfg["expected_model_revisions"]
                    and (judge is None or judge["canonical_slug"] == cfg["judge"]["canonical_slug"]),
                    "The registered models, provider or pricing changed")
            models_ready = True
        remaining = ceiling - prior
        require(remaining > 0, "No source budget remains for 503s")
        directory = wallet / f"pass-{round_number:04}"
        directory.mkdir(parents=True, exist_ok=True)
        selected = list(eligible)
        registered = dict(scope="source", cause="HTTP_503", plan_sha256=plan_digest(plan),
                          parent_registration_sha256=digest((base / "registration.json").read_bytes()),
                          source_checkpoint_sha256=digest(source_ck.read_bytes()), policy_sha256=digest(canonical(policy)),
                          cumulative_limit_usd=str(ceiling), prior_covered_usd=str(prior), implementation=identity,
                          superseded_attempts=list(eligible.values()))
        common = dict(workspace=str(workspace), output=str(directory), cfg=cfg, conn=resolved["conn"], identity=identity,
                      models=models, judge=judge, tasks=resolved["tasks"])
        if not (directory / "through-source.json").exists():
            earliest = max(datetime.fromisoformat(s["receipt"]["at"]).timestamp()
                           + max(delays[0], s["receipt"]["retry_after_seconds"]) for s in eligible.values())
            time.sleep(max(0, earliest - time.time()))

        def pace(label, done, total, covered):
            nonlocal consecutive
            key = selected[done - 1]
            shard = safe_path(directory, key)
            state = source_state(workspace, shard, digest((shard / "completion.json").read_bytes()),
                                 cases[key]["task_id"], registration_hash)
            now = time.time()
            until = (shard / "completion.json").stat().st_mtime + policy["minimum_inter_episode_seconds"]
            if state.get("receipt"):
                receipt = state["receipt"]
                wait = max(delays[min(consecutive, len(delays) - 1)], receipt["retry_after_seconds"])
                until = max(until, datetime.fromisoformat(receipt["at"]).timestamp() + wait)
                consecutive += 1
            else:
                consecutive = 0
            print(plan["id"], round_number, done, "of", total, state["missing"] or "graded", "covered USD", covered, flush=True)
            time.sleep(max(0, until - now))

        # Each failed attempt keeps its billed and unpriced requests. Only new child shards can run.
        result = run_stages(directory, registered, [("source", "source", [run_cases[k] for k in selected])],
                            (bank_shard, common), total_usd_limit=ceiling, prior_covered_usd=prior, max_concurrent=1,
                            record_unfunded_shards=False, through_stage="source", progress=pace)
        require(result["completed_through"] == "source" and not result["budget_deferred"],
                "Source recovery stopped at the approved budget boundary")
        checkpoint = directory / "through-source.json"
        verify_stage_checkpoints(directory, digest((directory / "registration.json").read_bytes()))
        require(set(result["stages"]) == set(selected), "Source retry dispatched an unregistered task")
        for key in selected:
            folder = safe_path(directory, key)
            entry = result["stages"][key]
            state = source_state(workspace, folder, entry["seal"], cases[key]["task_id"], registration_hash)
            state["opening_sha256"] = confirmed_opening(folder, entry["seal"], cases[key]["customer_opening_sha256"])
            if state["success"] is not None:
                public = source_from(folder, state["task"])
                require(public and public.source_run_id == state["run_id"], "Recovered source has no public experience")
                state["public_context_sha256"] = public.public_context_sha256
            current[key] = state
        prior += sum((Decimal(v["spent"]) for v in result["stages"].values()), Decimal(0))
        require(prior == Decimal(result["total_covered_usd"]) and prior <= ceiling, "Retry spending is inconsistent")
        rounds.append(dict(directory=str(directory.relative_to(workspace)), checkpoint_sha256=digest(checkpoint.read_bytes())))
        round_number += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    policy = read(ROOT / POLICY)
    require(policy["scope"] == "source" and policy["http_status"] == 503 and policy["max_concurrent"] == 1,
            "Unsupported recovery authorization")
    identity = implementation_identity(ROOT)
    originals = check_originals(workspace, policy)
    wallet = workspace / "runs" / "own-mixed-source-http503-01"
    with stage_lease(wallet):
        fixed(wallet / "authorization.json", policy)
        for plan, base, reg in originals:
            with stage_lease(base):
                recover(workspace, policy, plan, base, reg, identity)


if __name__ == "__main__":
    main()
