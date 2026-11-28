"""Two-pass BFCL transfer on the registered hosted worker, with paired native target decisions."""

from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
import time

from agent_memory_eval.adapters.bfcl_task_start import REVISION, open_relevance
from agent_memory_eval.analysis.efficiency import first_decision_effort, token_usage_by_role
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.analysis.selection import worker_choices
from agent_memory_eval.contracts.core import MemoryPolicy, RunContext, canonical, digest, read_json
from agent_memory_eval.experiments.batch_recovery import stage_lease
from agent_memory_eval.experiments.forms_batch import endpoint_terms
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.memory.writing import (
    REQUIRED_STORE_CONTRACT,
    WriterFeedback,
    freeze_public_experience,
    read_public_experience,
    writer_inputs,
    writer_template,
    write_from_experience,
)
from agent_memory_eval.models.expense_ledger import ExpenseLedger
from agent_memory_eval.models.openrouter import (
    OpenRouterClient,
    ProviderError,
    model_catalog,
    model_endpoints,
    resolve_endpoint,
    resolve_model,
)
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.batch import usage_from_events
from agent_memory_eval.recording.journal import BlobStore, Journal, read_experiment
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.runner.bfcl_relevance import grade_reply, hosted_tools, load_inputs, normalized_first, solve
from agent_memory_eval.study.adapters.tau_banking import approval_for
from agent_memory_eval.study.followup_budget import approval_amount, register_grants, registration_freeze
from agent_memory_eval.study.plan import (
    arm_name,
    conditions,
    content_text,
    model_profile,
    plan_digest,
    require_declared_models,
)
from agent_memory_eval.study.records import fixed, read
from agent_memory_eval.study.transfer import allocation_rows, reader_arms


def hosted_model(worker):
    model = resolve_model(model_catalog(), worker["model_id"], worker["parameters"])
    params = dict(worker["parameters"], tools=[], tool_choice="auto")
    return resolve_endpoint(model, model_endpoints(model["id"]), worker["provider"], params)


def schedule(plan):
    donors = defaultdict(list)
    for pair in plan["transfer"]["pairs"]:
        donors[pair["source"]].append(pair)
    policies = conditions(plan)
    for source, pairs in donors.items():
        for draw in range(1, plan["first_pass"]["draws"] + 1):
            tasks = []
            for pair in pairs:
                for rep, reader in policies:
                    for n in range(1, plan["repeats"] + 1):
                        for bank, arm in zip(("real", "empty"), reader_arms(arm_name(rep, reader))):
                            tasks.append(
                                dict(
                                    source=source,
                                    target=pair["target"],
                                    draw=draw,
                                    repeat=n,
                                    form=rep,
                                    reader=reader,
                                    bank=bank,
                                    condition=arm,
                                )
                            )
            yield source, draw, sorted(tasks, key=lambda t: digest(canonical([plan["id"], t])))


def run_key(study, *parts):
    return "r_" + digest(canonical([study, *parts]))[:32]


def run_events(scan, run):
    return [e for e in scan["events"].values() if e["run_id"] == run]


def terminal(events):
    return next((e["payload"] for e in events if e["type"] == "run.ended"), None)


def last_run(scan, study, key):
    """Follow only deterministic, append-only replacements for unsubmitted interruptions."""
    if key not in scan["starts"]:
        return None
    while (later := run_key(study, key, "unsubmitted")) in scan["starts"]:
        key = later
    return key


def completion(dest, groups, summary):
    files = ["registration.json", "expenses.jsonl", *[f"{s}--{d}/log.jsonl" for s, d, _ in groups]]
    return dict(summary=summary, files={f: digest((dest / f).read_bytes()) for f in files if (dest / f).is_file()})


def writer_instruction(plan, root, form):
    writing = plan["writing"]
    return content_text(root, writing["instruction"]) + "\n" + content_text(root, writing["representations"][form])


def reader_config(cfg, plan, job, root):
    reader = plan["reading"][job["reader"]]
    mode = reader["read_mode"]
    notice = content_text(root, reader["notice"]) if "notice" in reader else ""
    return dict(
        cfg, memory_notice_by_delivery={mode: notice}, read_tool_descriptions=reader.get("tool_descriptions")
    ), mode


def target_outcome(folder, evidence, run, cfg, mode, items, case, is_empty, expected_start, opening_ref):
    scan, _, _ = evidence
    events = run_events(scan, run)
    end = terminal(events)
    blobs = BlobStore(folder)
    public = read_json(expected_start.public_input)
    tools = hosted_tools(public["functions"])
    require(blobs.get(scan["starts"][run]["payload"]["task_ref"]) == public["messages"][0],
            "BFCL run started on a different target question")
    choices = worker_choices(folder, run_id=run, evidence=evidence)["choices"]
    require(opening_ref or (not choices and not end), "Missing BFCL target opening")
    if opening_ref:
        require(blobs.get(opening_ref) == expected_start.record(), "Changed registered BFCL target opening")
    if choices:
        first = blobs.get(choices[0]["request_ref"])
        require(normalized_first(first, cfg, mode, items) == dict(
            model=cfg["model"], messages=[dict(role="system", content=cfg["worker_instruction"]), *public["messages"]],
            tools=tools, **cfg["wire_parameters"],
        ), "BFCL reader did not receive the registered fresh target input")
    sent = [s for s in choices if s["delivery"] == "response_received"]
    first_worker = sent[0]["request_ref"]["sha256"] if sent else None
    reads = Counter(r["name"] for s in choices for r in s["reads"])
    returns = [e["payload"] for e in events if e["type"] == "memory.returned"]
    returned_fields = Counter(f for p in returns for item in p["items"] for f in item["fields"])
    included = Counter()
    answered_fields = set()
    for step in sent:
        fields = {f["field"] for f in step["visible_memory"]}
        included.update(fields)
        if step["status"] == "response":
            answered_fields.update(fields)
            included["body_with_response"] += "body" in fields
    grade, decision, success = None, None, None
    reason = end["termination_reason"] if end else "interrupted"
    if end:
        (score,) = [e["payload"] for e in events if e["type"] == "outcome.scored"]
        success = score["official_success"]
    if success is not None:
        grade = grade_reply(choices[-1]["response"], case[0], is_empty, tools)
        require(
            grade == blobs.get(score["official_artifact_ref"]) and grade["valid"] is success,
            "Target action disagrees with native BFCL grade",
        )
        decision = dict(correct=success, call=grade["any_call"])
    elif choices and reason in {
        "private_read_limit", "malformed_business_call", "mixed_private_business_response",
        "unavailable_memory_tool", "WRITER_OUTPUT_TRUNCATED",
    }:
        decision = dict(correct=False, call=None)
    if decision is not None:
        decision.update(request=choices[-1]["request_id"], body_seen="body" in answered_fields,
                        title_seen="title" in answered_fields)
    return dict(
        success=success,
        status="graded" if success is not None else "missing",
        decision=decision,
        missing_reason=None if success is not None else reason,
        termination_reason=reason,
        official_components=grade,
        opening_sha256=digest(expected_start.public_input) if opening_ref else None,
        first_worker_sha256=first_worker,
        effort_from_first_worker=first_decision_effort(events, token_breakdown=True),
        token_usage_by_role=token_usage_by_role(events),
        run=run,
        reads=dict(reads),
        evidence=dict(
            status="completed" if end else "interrupted", first_worker=first_worker, dispatched_worker=len(sent),
            title_requests=included["title"], body_requests=included["body"],
            body_requests_with_response=included["body_with_response"], reads=dict(reads),
            returns=dict(Counter(p["operation"] for p in returns)), returned_titles=returned_fields["title"],
            returned_bodies=returned_fields["body"],
            unresolved_worker=sum(s["delivery"] in {"pending", "unknown"} for s in choices),
        ),
        billing=usage_from_events(events),
    )


def audited_reader(folder, evidence, run, job, cfg, mode, source_run, writer_run, case, is_empty, target_start):
    scan, state, _ = evidence
    head = scan["starts"][run]["payload"]
    require(head["fresh_session"] and head["source_run_ids"] == [source_run, writer_run], "Wrong transfer session")
    write_end = terminal(run_events(scan, writer_run))
    require(write_end and write_end["termination_reason"] == "memory_write_completed", "Unsealed donor writer")
    events = run_events(scan, run)
    assignments = [e["payload"] for e in events if e["payload"].get("origin") == "bfcl.transfer_assignment"]
    opening_ref = None
    if assignments:
        (assignment,) = assignments
        require(
            assignment["bank"] == job["bank"] and assignment["source_task"] == job["source"]
            and assignment["donor_snapshot"] == write_end["end_snapshot_id"], "Changed donor or control",
        )
        opening_ref = assignment["target_start_ref"]
    blobs = BlobStore(folder)
    items = snapshot_items(blobs, state, head["start_snapshot_id"])
    expected = snapshot_items(blobs, state, write_end["end_snapshot_id"]) if job["bank"] == "real" else []
    require(items == expected and (job["bank"] == "empty" or items), "Changed reader snapshot contents")
    result = target_outcome(folder, evidence, run, cfg, mode, items, case, is_empty, target_start, opening_ref)
    return dict(result, source_run=source_run, writer_run=writer_run, donor_snapshot=write_end["end_snapshot_id"])


def audited_baseline(folder, evidence, run, target, draw, index, p):
    scan, _, _ = evidence
    head = scan["starts"][run]
    events = run_events(scan, run)
    require(
        head["condition_id"] == "no_memory" and head["payload"]["fresh_session"]
        and head["payload"]["bank_id"] is None and head["payload"]["start_snapshot_id"] is None
        and head["payload"]["source_run_ids"] == [] and not any(e["type"].startswith("memory.") for e in events),
        "Target no-memory run received a prior session or memory",
    )
    roles = [e["payload"]["actor_role"] for e in events if e["type"] == "model.request_started"]
    require(roles in ([], ["worker"]), "Unexpected participant or extra decision in a BFCL no-memory control")
    (assignment,) = [e["payload"] for e in events if e["payload"].get("origin") == "bfcl.target_no_memory"]
    require(assignment["index"] == index and assignment["draw"] == draw, "Wrong target no-memory repetition")
    result = target_outcome(folder, evidence, run, p["cfg"], None, [], p["cases"][target], p["is_empty"],
                            p["starts"][target], assignment["target_start_ref"])
    return dict(index=index, **result)


def target_rows(plan, attempts, **receipts):
    rows = allocation_rows(plan, attempts, **receipts)
    for row in rows:
        for solve in [row, *row["target_baselines"]]:
            solve.setdefault("opening_sha256", None)
            solve.setdefault("first_worker_sha256", None)
            solve["state_diagnostics"] = dict(status="not_applicable", reason="bfcl_relevance_has_no_database")
            solve["dialogue"] = dict(status="not_applicable", reason="single_native_question_without_customer_simulator")
    return rows


class BfclTransfer:
    name = "bfcl-relevance"
    execution_mode = "hosted"

    def resolve(self, plan, workspace, root, *, checkout=None):
        require_declared_models(plan)
        require(not plan["first_pass"].get("reuse"), "Hosted BFCL collects its own no-memory donors")
        profile = model_profile(plan, root)
        worker = read_json(asset_text(root, profile["connection"]).encode())["worker"]
        terms = profile["endpoint_terms"]["worker"]
        require(
            worker["model_id"] == terms["model"] and worker["provider"]["only"] == [terms["provider"]],
            "Worker profile differs from the hosted transport",
        )
        setup = read_json(asset_text(root, plan["benchmark"]["setup"]).encode())
        require(
            setup["format"] == "bfcl-hosted-transfer-setup/v1" and setup["revision"] == REVISION, "Wrong BFCL setup"
        )
        checkout = Path(checkout or Path(workspace) / ".local/benchmark-portability/bfcl-6ea5797")
        categories = defaultdict(list)
        for task in plan["benchmark"]["tasks"]:
            cat = task.rsplit("_", 1)[0]
            require(cat in {"live_relevance", "irrelevance"}, "Unsupported BFCL task category")
            categories[cat].append(task)
        cases, is_empty = load_inputs(checkout, dict(setup, tasks=categories))
        params = dict(worker["parameters"], stream=False, provider=worker["provider"])
        gen = dict(seed=params["seed"], worker_tokens=params["max_completion_tokens"])
        cfg = dict(
            setup,
            model=terms["model"],
            model_revision=profile["expected_revisions"]["worker"],
            generation=gen,
            wire_parameters=params,
        )
        starts, by_id = {}, {}
        for cat, row in cases:
            native = open_relevance(
                row, cat, dict(model=cfg["model"], revision=cfg["model_revision"], generation=gen)
            )
            hosted_tools(row["function"])
            starts[row["id"]], by_id[row["id"]] = native, (cat, row)
        source_inputs = {starts[p["source"]].public_input for p in plan["transfer"]["pairs"]}
        for p in plan["transfer"]["pairs"]:
            require(starts[p["target"]].public_input not in source_inputs, "Duplicate source and target public input")
            require(
                p["decision"]["expected"] == (by_id[p["target"]][0] == "live_relevance"), "Incorrect target rubric"
            )
        groups = list(schedule(plan))
        forms = list(dict.fromkeys(rep for rep, _ in conditions(plan)))
        assigned = dict(
            source=len(groups), write=len(groups) * len(forms), solve=sum(len(jobs) for _, _, jobs in groups)
        )
        record = dict(
            format="agent-memory-transfer-resolved/v1",
            adapter=self.name,
            plan_sha256=plan_digest(plan),
            revision=REVISION,
            worker=cfg["model"],
            model_revision=cfg["model_revision"],
            cases=[dict(task=t, public_input_sha256=digest(s.public_input)) for t, s in starts.items()],
            stages=["source", "write", "solve"],
            assigned=assigned,
        )
        if "target_no_memory_repeats" in plan["transfer"]:
            repeats = plan["transfer"]["target_no_memory_repeats"]
            require(type(repeats) is int and repeats >= 1, "Register a positive number of target no-memory solves")
            targets = dict.fromkeys(pair["target"] for pair in plan["transfer"]["pairs"])
            controls = [
                dict(target=t, draw=d, repeats=repeats)
                for t in targets for d in range(1, plan["first_pass"]["draws"] + 1)
            ]
            record["target_no_memory"] = controls
            assigned["target_no_memory"] = len(controls) * repeats
            record["stages"].insert(0, "target_no_memory")
            groups = [(c["target"], c["draw"], None) for c in controls] + groups
        return dict(
            record=record,
            cfg=cfg,
            cases=by_id,
            starts=starts,
            groups=groups,
            forms=forms,
            is_empty=is_empty,
            worker=worker,
            terms=terms,
        )

    def run(
        self,
        plan,
        workspace,
        root,
        *,
        approval,
        increases=(),
        checkout=None,
        progress=None,
        model_resolver=hosted_model,
        client_factory=OpenRouterClient,
        identity=None,
        sleep=time.sleep,
    ):
        prepared = self.resolve(plan, workspace, root, checkout=checkout)
        record, cfg = prepared["record"], prepared["cfg"]
        grants = [approval, *increases]
        require(
            all(
                g == approval_for(plan, record, max_usd=g["max_usd"], user_authorization=g["user_authorization"])
                and g["user_authorization"].strip()
                and approval_amount(g["max_usd"]) <= Decimal(plan["budget"]["max_usd"])
                for g in grants
            ),
            "Spending is not authorized for this transfer",
        )
        dest = Path(workspace) / "runs" / plan["id"]
        dest.mkdir(parents=True, exist_ok=True)
        assigned = record["assigned"]
        limits = cfg["limits"]
        attempts = 1 + len(plan["retry"]["wait_seconds"])
        max_calls = (
            (assigned["source"] + assigned.get("target_no_memory", 0)) * limits["source_calls"]
            + assigned["write"] * limits["writer_calls"]
            + assigned["solve"] * max(limits["preload_calls"], limits["on_demand_calls"]) * attempts
        )
        with stage_lease(dest):
            implementation = identity or implementation_identity(root)
            if (dest / "registration.json").is_file():
                implementation = registration_freeze(
                    root, plan["id"], read(dest / "registration.json")["implementation"], implementation
                )
            fixed(
                dest / "registration.json",
                dict(preflight=record, approval=approval, implementation=implementation, worker=prepared["terms"]),
            )
            guard = ExpenseLedger(dest / "expenses.jsonl", grants[-1]["max_usd"], max_calls, float("inf"))
            register_grants(dest, grants, max_calls, guard.calls)
            closed = dest / "completion.json"
            if closed.is_file():
                summary = read(closed)["summary"]
                require(
                    read(closed) == completion(dest, prepared["groups"], summary), "Changed completed BFCL recording"
                )
                return summary
            model = model_resolver(prepared["worker"])
            require(
                endpoint_terms(dict(worker=model))["worker"] == prepared["terms"]
                and model["canonical_slug"] == cfg["model_revision"],
                "Hosted worker revision or endpoint terms changed",
            )
            client = client_factory(
                model,
                guard,
                route=prepared["worker"]["provider"],
                max_input_bytes=plan.get("limits", {}).get("max_input_bytes", 128000),
            )
            writer = client_factory(
                model,
                guard,
                route=prepared["worker"]["provider"],
                max_input_bytes=plan.get("limits", {}).get("writer_max_input_bytes", client.max_input_bytes),
            )
            for index, (task, draw, jobs) in enumerate(prepared["groups"], 1):
                self._group(plan, root, dest, prepared, task, draw, jobs, guard, client, writer, sleep)
                if progress:
                    progress("transfer", index, len(prepared["groups"]), guard.spent)
                if guard.denied:
                    break
            summary = dict(
                reported_usd=str(guard.spent),
                reserved_usd=str(guard.held),
                requests=guard.calls,
                budget_deferred=guard.denied,
            )
            if not guard.denied:
                fixed(closed, completion(dest, prepared["groups"], summary))
            return summary

    def _group(self, plan, root, dest, p, task, draw, jobs, guard, client, writer_client, sleep):
        study, cfg = plan["id"], p["cfg"]
        folder = dest / f"{task}--{draw}"
        with Journal(folder, study, contract_version="1.2") as journal:

            def once(key, stage, action):
                # Retain any possibly submitted attempt. A provably unsubmitted interruption uses a fresh run id.
                run = key
                while run in journal.scan["starts"]:
                    events = run_events(journal.scan, run)
                    ended = terminal(events)
                    deferred = (ended and ended["termination_reason"] == "STUDY_LIMIT") or any(
                        e["type"] == "runtime.error" and e["payload"].get("code") == "STUDY_LIMIT" for e in events
                    )
                    if run in guard.reserved_runs or (ended and not deferred):
                        return run, ended
                    run = run_key(study, run, "unsubmitted")
                guard.stage, guard.run_id = stage, run
                guard.unknown_cost = False  # Unreconciled charges remain fully reserved in the persistent ledger.
                try:
                    action(run)
                except ProviderError as exc:
                    if run in journal.scan["starts"] and not terminal(run_events(journal.scan, run)):
                        start = journal.scan["starts"][run]
                        ctx = RunContext(
                            study, run, start["session_id"], start["condition_id"], "diagnostic", "solve", "observed"
                        )
                        journal.append(
                            [
                                ctx.at("grade").event(
                                    "outcome.scored",
                                    dict(
                                        grading_status="not_available",
                                        official_success=None,
                                        official_score=None,
                                        official_artifact_ref=None,
                                    ),
                                )
                            ]
                        )
                        journal.append(
                            [
                                ctx.at("seal").event(
                                    "run.ended",
                                    dict(
                                        termination_reason=exc.code,
                                        grading_status="not_available",
                                        end_snapshot_id=None,
                                    ),
                                )
                            ]
                        )
                return run, terminal(run_events(journal.scan, run))

            if jobs is None:
                # Each target/draw has its own journal, shared by all its donor comparisons.
                for index in range(1, plan["transfer"]["target_no_memory_repeats"] + 1):
                    if guard.denied:
                        break
                    once(
                        run_key(study, task, draw, "target_no_memory", index),
                        "target_no_memory",
                        lambda run: solve(
                            journal, cfg, p["cases"][task], client, p["is_empty"], run_id=run,
                            target_baseline=dict(target_start=p["starts"][task], draw=draw, index=index),
                        ),
                    )
                return
            source = task
            source_run, end = once(
                run_key(study, source, draw, "source"),
                "source",
                lambda run: solve(journal, cfg, p["cases"][source], client, p["is_empty"], run_id=run),
            )
            if not end or end["grading_status"] != "graded":
                return
            experience = freeze_public_experience(journal, source_run)
            writing = plan["writing"]
            writers = {}
            for form in p["forms"]:
                if guard.denied:
                    return
                instruction = writer_instruction(plan, root, form)
                key = run_key(study, source, draw, "write", form)
                run, end = once(
                    key,
                    "write",
                    lambda run: write_from_experience(
                        journal,
                        experience,
                        WriterFeedback(writing["feedback"]),
                        instruction=instruction,
                        policy=MemoryPolicy(True, "post_task", False, 1, read_mode="none", max_items=1),
                        client=writer_client,
                        model_config=dict(model=cfg["model"], **cfg["wire_parameters"]),
                        max_requests=cfg["limits"]["writer_calls"],
                        condition_id=form,
                        run_id=run,
                        completion_contract=REQUIRED_STORE_CONTRACT,
                    ),
                )
                if end and end["termination_reason"] == "memory_write_completed":
                    writers[form] = dict(run_id=run, snapshot_id=end["end_snapshot_id"])
            for job in jobs:
                if guard.denied or job["form"] not in writers:
                    continue
                reader, mode = reader_config(cfg, plan, job, root)
                target = job["target"]
                for attempt in range(len(plan["retry"]["wait_seconds"]) + 1):
                    key = run_key(study, job, attempt)
                    if attempt and key not in journal.scan["starts"]:
                        sleep(max(plan["retry"]["wait_seconds"][attempt - 1], guard.retry_after))
                    run, end = once(
                        key,
                        "solve",
                        lambda run: solve(
                            journal,
                            reader,
                            p["cases"][target],
                            client,
                            p["is_empty"],
                            run_id=run,
                            writer=writers[job["form"]],
                            form=job["form"],
                            mode=mode,
                            transfer=dict(
                                source_task=source,
                                source_run=source_run,
                                target_start=p["starts"][target],
                                bank=job["bank"],
                            ),
                        ),
                    )
                    if not end or end["termination_reason"] not in {
                        f"HTTP_{code}" for code in plan["retry"]["http_codes"]
                    }:
                        break

    def outcomes(self, plan, workspace, root, *, checkout=None):
        p = self.resolve(plan, workspace, root, checkout=checkout)
        dest = Path(workspace) / "runs" / plan["id"]
        target_baselines = {}
        if not (dest / "registration.json").is_file():
            return target_rows(plan, [], target_baselines=target_baselines)
        require(read(dest / "registration.json")["preflight"] == p["record"], "BFCL transfer registration changed")
        closed = dest / "completion.json"
        if closed.exists():
            seal = read(closed)
            require(seal == completion(dest, p["groups"], seal["summary"]), "Changed completed BFCL recording")
        rows = []
        written_memories = {}
        known_sessions = set()
        for task, draw, jobs in p["groups"]:
            folder = dest / f"{task}--{draw}"
            if not (folder / "log.jsonl").exists():
                continue
            evidence = read_experiment(folder)
            scan, state, experiment = evidence
            require(experiment in {None, plan["id"]}, "Journal belongs to another transfer study")
            sessions = [e["session_id"] for e in scan["starts"].values()]
            require(len(set(sessions)) == len(sessions) and known_sessions.isdisjoint(sessions),
                    "BFCL transfer sessions must be independent")
            known_sessions.update(sessions)
            if not any(scan[k] for k in ("tail_bytes", "incomplete_runs", "pending_requests")):
                audit_requests(folder, evidence=evidence)
            study, blobs = plan["id"], BlobStore(folder)
            if jobs is None:
                controls = target_baselines[task, draw] = []
                for i in range(1, plan["transfer"]["target_no_memory_repeats"] + 1):
                    run = last_run(scan, study, run_key(study, task, draw, "target_no_memory", i))
                    if run is not None:
                        controls.append(audited_baseline(folder, evidence, run, task, draw, i, p))
                continue
            source = task
            source_run = last_run(scan, study, run_key(study, source, draw, "source"))
            source_end = terminal(run_events(scan, source_run))
            if source_end and source_end["grading_status"] == "graded":
                source_start = scan["starts"][source_run]["payload"]
                public = read_json(p["starts"][source].public_input)
                (req,) = [e["payload"] for e in run_events(scan, source_run) if e["type"] == "model.request_started"]
                require(
                    source_start["fresh_session"]
                    and source_start["bank_id"] is None
                    and not source_start["source_run_ids"]
                    and blobs.get(req["request_ref"])
                    == dict(
                        model=p["cfg"]["model"],
                        messages=[dict(role="system", content=p["cfg"]["worker_instruction"]), *public["messages"]],
                        tools=hosted_tools(public["functions"]),
                        **p["cfg"]["wire_parameters"],
                    ),
                    "Unexpected BFCL donor input",
                )
            writers = {}
            for form in p["forms"]:
                writer = last_run(scan, study, run_key(study, source, draw, "write", form))
                ended = terminal(run_events(scan, writer))
                if not ended or ended["termination_reason"] != "memory_write_completed":
                    continue
                stored = state["memory_writes"][writer]
                exp = read_public_experience(blobs, scan, stored["experience_ref"])
                feedback = WriterFeedback(plan["writing"]["feedback"])
                require(
                    exp["source_run_id"] == source_run
                    and scan["starts"][writer]["condition_id"] == form
                    and stored["feedback"]["level"] == feedback.level,
                    "Wrong transfer writer",
                )
                template = writer_template(
                    dict(model=p["cfg"]["model"], **p["cfg"]["wire_parameters"]),
                    writer_instruction(plan, root, form),
                    completion_contract=REQUIRED_STORE_CONTRACT,
                )
                template["messages"] = [template.pop("system"), *writer_inputs(blobs, scan, exp, feedback)]
                req = next(e["payload"] for e in run_events(scan, writer) if e["type"] == "model.request_started")
                require(blobs.get(req["request_ref"]) == template, "Writer saw inputs outside its registered donor")
                writers[form] = writer
                snapshot = ended["end_snapshot_id"]
                bit = None
                if feedback.level == "result_only":
                    bit = scan["events"][exp["outcome_event_id"]]["payload"]["official_success"]
                written_memories[source, draw, form] = dict(
                    form=form, source_run=source_run, writer_run=writer, snapshot=snapshot,
                    feedback_success=bit, items=snapshot_items(blobs, state, snapshot),
                    public_experience_ref=dict(
                        path=str((folder / stored["experience_ref"]["path"]).relative_to(workspace)),
                        sha256=stored["experience_ref"]["sha256"],
                    ),
                    token_usage_by_role=token_usage_by_role(run_events(scan, writer)),
                    source_token_usage_by_role=token_usage_by_role(run_events(scan, source_run)),
                )
            for job in jobs:
                reader, mode = reader_config(p["cfg"], plan, job, root)
                slot = dict(
                    task=job["target"],
                    source=source,
                    draw=draw,
                    repeat=job["repeat"],
                    condition=job["condition"],
                    origin=study,
                )
                for attempt in range(1 + len(plan["retry"]["wait_seconds"])):
                    run = last_run(scan, study, run_key(study, job, attempt))
                    if run is None:
                        if attempt == 0 and job["form"] not in writers and closed.exists():
                            reason = (
                                "donor_ungraded"
                                if not source_end or source_end["grading_status"] != "graded"
                                else "writer_unavailable"
                            )
                            rows.append(
                                dict(
                                    slot,
                                    attempt=0,
                                    success=None,
                                    status="missing",
                                    decision=None,
                                    missing_reason=reason,
                                )
                            )
                        break
                    require(job["form"] in writers, "Reader lacks its registered donor writer")
                    result = audited_reader(
                        folder,
                        evidence,
                        run,
                        job,
                        reader,
                        mode,
                        source_run,
                        writers[job["form"]],
                        p["cases"][job["target"]],
                        p["is_empty"],
                        p["starts"][job["target"]],
                    )
                    rows.append(dict(slot, attempt=attempt, **result))
        return target_rows(plan, rows, written_memories=written_memories, target_baselines=target_baselines)
