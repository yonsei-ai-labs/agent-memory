"""Paired retail outcomes for a follow-up of previously registered first decisions."""

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import fcntl
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agent_memory_eval.analysis.claim_evidence import paired_counts
from agent_memory_eval.analysis.paired_policy import customer_contrast
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import endpoint_terms, live_models, write_json
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.recording.assets import asset_bytes, asset_text, publish_json
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.study.plan import model_profile, plan_digest
from agent_memory_eval.study.records import fixed
from scripts.analyze_full_retail import require
from scripts.analyze_retail_conditional_retrieval import analyze as score_decisions, charges_by_run
from scripts.analyze_retail_two_visit import grade_turns, public_turns
from scripts.audit_retail_two_visit_billing import audit as audit_billing
from scripts.code_retail_two_visit_turns import propose
from scripts.retail_two_visit_episode import restore_decision
from scripts.run_retail_conditional_retrieval import frozen_banks
from scripts.run_retail_two_visit import ExpenseLedger, execute, prepare, read, read_receipt, recover_finished


def approval_amount(value):
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise ContractError("Supply a positive finite spending ceiling") from error
    require(amount.is_finite() and amount > 0, "Supply a positive finite spending ceiling")
    return amount


def copy_checkpoint(original, dest, job, expected):
    """Continue from a private copy, publishing the whole original prefix before the first dispatch."""
    source = safe_path(original, job["run"])
    target = safe_path(dest, job["run"])
    if not target.exists():
        with TemporaryDirectory(dir=dest) as temp:
            staging = Path(temp) / "session"
            staging.mkdir()
            for filename in ("log.jsonl", "decision.json"):
                shutil.copyfile(safe_path(source, filename), staging / filename)
            shutil.copytree(safe_path(source, "blobs"), staging / "blobs")
            require(read_receipt(staging, "decision") == expected
                    and (staging / "log.jsonl").stat().st_size == expected["journal_bytes"],
                    "Checkpoint copy differs from the sealed original")
            staging.rename(target)
    require(read_receipt(target, "decision") == expected, "The follow-up session has a different first decision")


def paired_outcome(rows, comparison, field, uncertainty):
    memory, empty = comparison["memory"], comparison["empty"]
    cases = defaultdict(dict)
    for row in rows:
        if row["mode"] != comparison["primary_mode"] or row["arm"] not in (memory, empty):
            continue
        pair = cases[row["customer"], row["task"]]
        require(row["arm"] not in pair, "Duplicate assignment in a paired outcome")
        pair[row["arm"]] = row[field]
    estimate = customer_contrast(cases, {memory: 1, empty: -1}, uncertainty=uncertainty)
    return dict(estimate, pairs=paired_counts((case[memory], case[empty]) for case in cases.values()))


def validate_pairs(jobs, design, comparison, sample):
    """Match the real and empty banks before looking at the response to either bank."""
    arms = design["arms"]
    real, empty = (arms.get(comparison[k], {}) for k in ("memory", "empty"))
    require(real.get("native_arm") == "query" and empty.get("native_arm") == "empty_query"
            and real["instruction"] == empty["instruction"], "Compare real and empty banks with identical instructions")
    grouped = defaultdict(dict)
    for job in jobs:
        cells = grouped[job["customer"], job["task"]]
        require(job["arm"] not in cells and job["in_sample"] == (job["customer"] in sample),
                "Repeated assignment or changed checkpoint sample")
        cells[job["arm"]] = job
    matched = 0
    for cells in grouped.values():
        require(cells.keys() == arms.keys(), "A registered case lacks an arm")
        for name, job in cells.items():
            spec = arms[name]
            require(all(job[k] == spec[k] for k in ("instruction", "native_arm")), "Mislabeled decision condition")
            if spec["native_arm"] != "empty_query":
                continue
            require(not job["writer_runs"] and job["items_sha256"] == digest(canonical([])),
                    "Assigned empty bank contains a memory or writer")
            others = [j for j in cells.values() if j["instruction"] == job["instruction"] and j["native_arm"] == "query"]
            keys = ("source", "mode", "prefix", "position", "target", "history", "initial_sha256")
            require(len(others) == 1 and all(others[0][k] == job[k] for k in keys),
                    "Real and empty conditions differ in public state, target or first wire")
            matched += 1
    return matched


ENDPOINTS = ("consent_without_reasking", "asked_which_order", "wrong_proposal", "consent_qualified", "db_success")


def consultation_row(folder, job, first, previous, reviews, packets):
    row = {k: job[k] for k in ("customer", "source", "task", "run", "mode", "arm")}
    row.update(first_decision=first["status"], session=previous["session"], first_lookup=previous["first_lookup"],
               focused=previous["focused"], decision_request=previous["decision_request"],
               first_journal_sha256=first.get("journal_sha256"), input_exposure=previous["included"],
               title_seen=any(span["field"] == "title" for span in previous["included"]),
               body_seen=any(span["field"] == "body" for span in previous["included"]),
               first_confirmed_usd=previous["confirmed_usd"], first_held_usd=previous["held_usd"],
               review=None, **dict.fromkeys(ENDPOINTS))
    receipt = read_receipt(folder, "continuation") if folder else None
    if first["status"] != "checkpoint":
        require(receipt is None, "Continuation recorded after a terminal first decision")
        row["status"] = first["status"]
        if first["status"] == "private_read_limit":
            row.update(consent_without_reasking=False, consent_qualified=False, db_success=False)
        return row
    if folder and folder.exists():
        require(read_receipt(folder, "decision") == first, "Follow-up is linked to a different first decision")
    if receipt is None:
        row["status"] = "interrupted" if folder and (folder / "continuation.started").exists() else "unstarted"
        return row
    require((folder / "log.jsonl").stat().st_size == receipt["journal_bytes"], "Unsealed follow-up journal suffix")
    row.update(status=receipt["status"], reason=receipt.get("provider_error") or receipt.get("reason")
               or receipt.get("termination"), journal_sha256=receipt["journal_sha256"])
    if row["status"] != "completed":
        return row
    verified = recover_finished(folder, "continuation", job, True)
    require(verified and all(receipt[k] == v for k, v in verified.items()), "Full outcome differs from its native journal")
    turns = public_turns(BlobStore(folder).get(receipt["trajectory"]))
    label_id = digest(canonical(turns))
    packet = dict(turns_sha256=label_id, turns=turns)
    packets[label_id] = packet
    labels = reviews.get(label_id)
    if labels is None:
        labels, reason = propose(packet)
    else:
        require(labels.get("status") in {"reviewed", "unresolved"} and labels.get("turns_sha256") == label_id,
                "Manual review is not bound to this public transcript")
        reason = labels["status"]
    grade = grade_turns(turns, job["target"], receipt["db_reward"], labels)
    qualified, questions, wrong = (grade[k] for k in ("consent_qualified", "clarifications", "wrong_proposals"))
    success = receipt["db_reward"] == 1
    without_reasking = None
    if not success:
        without_reasking = False
    elif qualified is not None:
        without_reasking = qualified and questions == 0
    row.update(db_success=success, consent_qualified=qualified if success else False,
               asked_which_order=None if questions is None else questions > 0,
               wrong_proposal=None if wrong is None else wrong > 0,
               consent_without_reasking=without_reasking,
               review=reason, turns_sha256=label_id, review_evidence=labels)
    return row


def followup_scorecard(rows, resolved, comparison, billing):
    groups = defaultdict(list)
    for row in rows:
        groups[row["mode"], row["arm"]].append(row)
    summary = []
    for (mode, arm), group in sorted(groups.items()):
        summary.append(dict(mode=mode, arm=arm, assigned=len(group), statuses=dict(Counter(r["status"] for r in group)),
                            outcomes={k: dict(observed=sum(r[k] is not None for r in group),
                                              positive=sum(r[k] is True for r in group)) for k in ENDPOINTS},
                            **{k: str(sum(Decimal(r[k]) for r in group)) for k in
                               ("first_confirmed_usd", "first_held_usd", "continuation_confirmed_usd", "continuation_held_usd")}))
    contrasts = {k: paired_outcome(rows, comparison, k, resolved["uncertainty"]) for k in ENDPOINTS}
    unreviewed = sum(r["status"] == "completed" and r["wrong_proposal"] is None for r in rows)
    statuses = dict(Counter(r["status"] for r in rows))
    phase = "scored"
    if billing is None:
        phase = "awaiting_execution"
    elif statuses.get("unstarted"):
        phase = "partial"
    elif unreviewed:
        phase = "awaiting_transcript_review"
    estimate = contrasts[comparison["primary_outcome"]]
    verdict = "pending" if phase != "scored" else "inconclusive"
    if phase == "scored" and estimate["interval"]:
        if min(estimate["interval"][0], estimate["bounds"][0]) > 0:
            verdict = "improved_in_this_cohort"
        elif max(estimate["interval"][1], estimate["bounds"][1]) < 0:
            verdict = "worse_in_this_cohort"
    return dict(format="agent-memory-retail-followup-scorecard/v1", phase=phase,
                statuses=statuses, completed_without_process_labels=unreviewed, allocation=resolved["record"]["assigned"],
                calibration=resolved["record"]["calibration"], primary=comparison, primary_result=verdict, contrasts=contrasts,
                by_mode_arm=summary, continuation_billing=billing)


class RetailRevisit:
    name = "tau2-retail-revisit"

    def resolve(self, plan, workspace, root, *, restore_native=True):
        benchmark = plan["benchmark"]
        original = safe_path(Path(workspace) / "runs", benchmark["recording"])
        reg = read_json(asset_bytes(original, {"path": "registration.json", "sha256": benchmark["registration_sha256"]}))
        design, jobs = reg["design"], reg["jobs"]["decision"]
        profile = model_profile(plan, root)
        require(design["name"] == benchmark["recording"] and design["decision"]["worker"] == profile["endpoint_terms"]["worker"]["model"]
                and not reg["jobs"]["continuation"] and digest(canonical(reg["jobs"])) == reg["preflight"]["registered_jobs_sha256"],
                "Different study, worker or original decision schedule")
        for role, terms in reg["endpoints"].items():
            require(all(profile["endpoint_terms"][role][k] == terms[k] for k in ("model", "provider", "provider_name", "pricing")),
                    "Historical model route or price differs from the declared profile")
        published = read_json(asset_bytes(root, benchmark["decisions"]))
        require(published["registration_sha256"] == benchmark["registration_sha256"] and score_decisions(original, root=root) == published,
                "Original responses, exposures, billing or scores differ from the published decisions")
        previous = read_json(asset_bytes(root, design["inputs"]["previous_preflight"]))
        base = read_json(asset_bytes(root, design["inputs"]["base_design"]))
        native = prepare(root, base, workspace=workspace)
        require(native["record"] == previous and all(m["canonical_slug"] == profile["expected_revisions"][role]
                    for role, m in native["models"].items()), "Native tasks, prefixes or model revisions changed")
        sample = set(previous["continuation_sample"])
        comparison = plan["comparison"]
        matched = validate_pairs(jobs, design, comparison, sample)
        banks, hashes = frozen_banks(workspace, design, {j["source"] for j in jobs})
        for job in jobs:
            if job["native_arm"] == "query":
                require(job["items_sha256"] == hashes[job["source"]] and job["writer_runs"] == banks[job["source"]][1],
                        "Real bank does not match the sealed writers from this first visit")
        scores = {row["run"]: row for row in published["decisions"]}
        uncertainty = design["analysis"]["uncertainty"]
        selected = [job for job in jobs if job["in_sample"]]
        native["instructions"].update({k: asset_text(root, ref) for k, ref in design["instructions"].items()})
        # No provider clients or append method are available to the preflight.
        runtime = dict(native, clients={}, guard=None)
        upstreams = {}
        for job in selected:
            run = job["run"]
            folder = safe_path(original, run)
            receipt = read_receipt(folder, "decision")
            require(receipt and (folder / "log.jsonl").stat().st_size == receipt["journal_bytes"]
                    and scores[run]["status"] == receipt["status"], "Selected original is missing or was continued")
            if restore_native and receipt["status"] == "checkpoint":
                scan, state, experiment = read_experiment(folder)
                journal = SimpleNamespace(scan=scan, state=state, experiment_id=experiment, events=scan["events"],
                                          blobs=BlobStore(folder), root=folder)
                restore_decision(journal, job, native["tasks"][job["task"]], native["public"][job["prefix"]], runtime, receipt)
            upstreams[run] = receipt
        require(digest(canonical(upstreams)) == benchmark["checkpoint_receipts_sha256"],
                "Frozen first-decision receipts have changed")
        require(len(selected) == design["continuation"]["checkpoint_sessions"]
                and {j["customer"] for j in selected} == sample, "Incomplete registered follow-up allocation")
        dispositions = Counter(r["status"] for r in upstreams.values())
        calibration = paired_outcome(published["decisions"], comparison, "focused", uncertainty)
        reference = published["simple_contrasts"].get(f"{comparison['memory']}_minus_{comparison['empty']}")
        require(reference and all(calibration[k] == v for k, v in reference.items() if k != "interval")
                and published["recommend_continuation"],
                "Paired calibration or the original continuation criteria do not match the recorded decisions")
        record = dict(plan_id=plan["id"], plan_sha256=plan_digest(plan), registration_sha256=benchmark["registration_sha256"],
                      upstream_receipts_sha256=digest(canonical(upstreams)), stages=["continuation"],
                      assigned=dict(registered=len(selected), continuable=dispositions["checkpoint"],
                                    customers=len(sample), decision_statuses=dict(dispositions),
                                    primary_pairs=sum(j["mode"] == comparison["primary_mode"] and j["arm"] == comparison["memory"]
                                                      for j in selected)),
                      calibration=dict(registered=len(jobs), observed=published["observed"], first_visit_banks=len(banks),
                                       matched_openings=matched,
                                       first_order_choice=calibration, instruction_interaction=published["primary"],
                                       by_mode_arm=published["by_mode_arm"]),
                      model_requests_sent=0)
        return dict(record=record, original=original, registration=reg, jobs=selected, upstreams=upstreams,
                    prior_scores=scores, uncertainty=uncertainty, native=native)

    def run(self, plan, workspace, root, *, approval, progress=None):
        from agent_memory_eval.study.adapters.tau_banking import approval_for
        data = self.resolve(plan, workspace, root)
        record, native = data["record"], data["native"]
        amount = approval_amount(approval.get("max_usd"))
        require(isinstance(approval.get("user_authorization"), str) and approval["user_authorization"].strip()
                and approval == approval_for(plan, record, max_usd=approval["max_usd"],
                                            user_authorization=approval["user_authorization"]),
                "Spending is not authorized for this follow-up plan and checkpoint cohort")
        require(os.getenv("OPENROUTER_API_KEY"), "Missing OpenRouter credentials")
        freeze = implementation_identity(root)
        profile = model_profile(plan, root)
        models = live_models(native["connection"])
        terms = endpoint_terms(models)
        require(set(models) == {"worker", "user_simulator"}
                and all(terms[r] == profile["endpoint_terms"][r]
                        and m["canonical_slug"] == profile["expected_revisions"][r] for r, m in models.items()),
                "Live worker or customer model, route or price differs from the plan")
        dest = safe_path(Path(workspace) / "runs", plan["id"])
        dest.mkdir(exist_ok=True)
        jobs = [j for j in data["jobs"] if data["upstreams"][j["run"]]["status"] == "checkpoint"]
        limits = native["protocol"]["limits"]
        max_calls = len(jobs) * (limits["worker_requests"] + limits["user_simulator_requests"])
        with (dest / "controller.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fixed(dest / "registration.json", dict(format=plan["format"], plan=plan, resolved=record, freeze=freeze,
                                                    endpoints=terms, jobs=dict(source=[], writer=[], decision=jobs, continuation=jobs)))
            fixed(dest / "authorization.json", dict(approval, max_requests=max_calls))
            for job in jobs:
                copy_checkpoint(data["original"], dest, job, data["upstreams"][job["run"]])
            guard = ExpenseLedger(dest / "expense.jsonl", amount, max_calls, float("inf"))
            clients = {r: OpenRouterClient(m, guard, route=native["connection"][r]["provider"],
                       max_input_bytes=limits["max_input_bytes"], timeout_seconds=native["protocol"]["request_timeout_seconds"])
                       for r, m in models.items()}
            study = dict(native, jobs=dict(source=[], writer=[], decision=[], continuation=jobs))
            totals = execute(study, dict(study, guard=guard, clients=clients, models=models),
                             through="continuation", continuation="sample", destination=dest,
                             pause_after_transient=plan["execution"]["pause_after_consecutive_transient_errors"])
            summary = dict(stages=totals, confirmed_usd=str(guard.spent), held_usd=str(guard.held),
                           reserved_calls=guard.calls, stopped_at_limit=guard.denied,
                           pause_reason=getattr(guard, "pause_reason", None))
            write_json(dest / "summary.json", summary)
            return summary

    def analyze(self, plan, workspace, root, *, reviews=None, review_packets=None):
        # Scoring reads the sealed decision and continuation traces. No native simulation is needed here.
        data = self.resolve(plan, workspace, root, restore_native=False)
        dest = safe_path(Path(workspace) / "runs", plan["id"])
        if not dest.exists():
            return self._score(plan, data, None, reviews, review_packets)
        with (dest / "controller.lock").open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ContractError("The follow-up controller is active; score its closed batch") from error
            return self._score(plan, data, dest, reviews, review_packets)

    @staticmethod
    def _score(plan, data, dest, reviews, review_packets):
        billing = None
        if dest:
            reg = read(dest / "registration.json")
            eligible = [j for j in data["jobs"] if data["upstreams"][j["run"]]["status"] == "checkpoint"]
            require(reg["plan"] == plan and reg["resolved"] == data["record"]
                    and reg["jobs"] == dict(source=[], writer=[], decision=eligible, continuation=eligible),
                    "Follow-up registration does not match the frozen cohort")
            require((dest / "summary.json").is_file(), "Resume the interrupted controller to reconcile costs before analysis")
            billing = audit_billing(dest, allow_interrupted_decisions=True, decision_prefixes=data["upstreams"])
        amounts = charges_by_run(dest) if dest else {}
        labels = read(reviews) if reviews else {}
        rows, packets = [], {}
        for job in data["jobs"]:
            run = job["run"]
            folder = safe_path(dest, run) if dest else None
            row = consultation_row(folder, job, data["upstreams"][run], data["prior_scores"][run], labels, packets)
            calls, confirmed, held = amounts.get(run, (0, 0, 0))
            rows.append(dict(row, continuation_requests=calls, continuation_confirmed_usd=str(confirmed),
                             continuation_held_usd=str(held)))
        require(set(labels) <= packets.keys(), "Review includes an unknown public transcript")
        if review_packets:
            publish_json(review_packets, dict(sorted(packets.items())))
        return rows, followup_scorecard(rows, data, plan["comparison"], billing)
