"""Run a funded subset of the registered τ² own/mixed readers against their sealed first passes and notes."""

from decimal import Decimal
from itertools import chain, repeat
import os
from pathlib import Path
import time

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import endpoint_terms
from agent_memory_eval.recording.assets import asset_bytes
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.study.adapters.tau_banking import bank_shard, study_models
from agent_memory_eval.study.adapters.tau_cohort import cohort_catalog
from agent_memory_eval.study.cohort import condition_banks, is_cohort, pool_sources
from agent_memory_eval.study.followup_budget import approval_amount
from agent_memory_eval.study.orchestrator import run_stages, verify_stage_entries
from agent_memory_eval.study.plan import arm_name, conditions, plan_digest
from agent_memory_eval.study.reader_stage import reader_input_digest
from agent_memory_eval.study.records import fixed, read, sealed_rows


def _stage_order(scope, stage):
    if stage in scope["solve_stages"]:
        return scope["solve_stages"].index(stage)
    suffix = stage.removeprefix("solve-retry-")
    require(scope.get("operations") and stage != suffix and suffix.isdecimal() and str(int(suffix)) == suffix
            and int(suffix) >= len(scope["solve_stages"]), "Unregistered reader retry stage")
    return int(suffix)


def _retry_lineage(folder, name, stages, codes):
    task, stage = name.split("/")
    base, separator, number = stage.rpartition("-retry-")
    if not separator:
        return
    previous = f"{task}/{base}"
    prior = previous if number == "1" else f"{previous}-retry-{int(number) - 1}"
    require(prior in stages and stages[prior].get("seal"), "A reader retry has no sealed preceding stage")
    _, old_control, old = sealed_rows(safe_path(folder, prior), stages[prior]["seal"])
    eligible = {r["arm"]: r for r in old if r["status"] == "error" and r["result"].get("official_success") is None
                and r["result"].get("error_code") in codes}
    require(eligible, "A reader retry has no eligible provider failure")
    shard = safe_path(folder, name)
    if (shard / "completion.json").is_file():
        _, ctl, rows = sealed_rows(shard, stages.get(name, {}).get("seal"))
        require(len(rows) == len(eligible) and {r["arm"] for r in rows} == eligible.keys()
                and ctl["design"]["interrupted_readers"].keys() == eligible.keys(),
                "A retry includes an unregistered or settled reader")
        for row in rows:
            arm = row["arm"]
            prior_row = eligible[arm]
            old_result, result = prior_row["result"], row.get("result") or {}
            old_wire, wire = old_result.get("first_worker_request_sha256"), result.get("first_worker_request_sha256")
            require(row["index"] == prior_row["index"] and row["run_id"] != prior_row["run_id"]
                    and (not result.get("session_id") or result["session_id"] != old_result.get("session_id"))
                    and (not wire or not old_wire or old_wire == wire)
                    and ctl["design"]["delivery_arms"][arm]["sources_sha256"] ==
                    old_control["design"]["delivery_arms"][arm]["sources_sha256"], "Retry changed its assignment, input or session")
    elif stages.get(name, {}).get("status") == "not_started":
        require(set(stages[name]["arms"]) == eligible.keys(), "Unfunded retry differs from its eligible slots")


def _batch_evidence(folder, directory, scope, *, include_funded=False):
    """An existing solve shard must have a prior grant. Every settled wave binds its shard seals."""
    controller = directory / "controller"
    registration = controller / "registration.json"
    if registration.exists():
        require(read(registration) == scope, "Reader controller scope differs from the registered batch")
    slots, stages = set(), {}
    waves = []
    for grant in controller.glob("*-wave-*.json"):
        if grant.name.endswith(".settled.json"):
            require(grant.with_name(grant.name.removesuffix(".settled.json") + ".json").is_file(),
                    "A settled reader wave lost its grant")
            continue
        stage, _, number = grant.stem.rpartition("-wave-")
        require(registration.is_file() and number.isdecimal() and int(number) >= 1,
                "Unexpected reader wave or missing batch registration")
        wave = read(grant)
        cases = wave["tasks"]
        names = {f"task-{case}/{stage}" for case in cases}
        require(cases and set(cases) <= set(scope["cases"]) and len(names) == len(cases) and names.isdisjoint(slots),
                "A reader grant contains an unregistered or repeated shard")
        slots.update(names)
        receipt = grant.with_suffix(".settled.json")
        waves.append((_stage_order(scope, stage), int(number), receipt.is_file()))
        if receipt.is_file():
            saved = read(receipt)
            require(saved["registration_sha256"] == digest(registration.read_bytes()) and set(saved["stages"]) == names,
                    "A settled reader wave differs from its grant")
            stages.update(saved["stages"])
    present = {str(p.relative_to(folder)) for case in scope["cases"]
               for p in (folder / ("task-" + case)).glob("solve*")}
    require(present <= slots and all(settled for _, _, settled in sorted(waves)[:-1]),
            "A reader shard has no registered wave or a later grant has no settled predecessor")
    completion = controller / "completion.json"
    if completion.is_file():
        saved = read(completion)
        require(registration.is_file() and saved["registration_sha256"] == digest(registration.read_bytes())
                and set(stages) == slots and saved["stages"] == stages, "Reader completion differs from its wave receipts")
    verify_stage_entries(folder, stages)
    if "operations" in scope:
        codes = {f"HTTP_{code}" for code in scope["retry_http_codes"]}
        for name in slots:
            _retry_lineage(folder, name, stages, codes)
    return (dict.fromkeys(slots, {}) | stages) if include_funded else stages


def reader_batch_stages(folder, *, include_funded=False):
    """Sealed reader receipts, including exclusions for which there is deliberately no task shard."""
    runs = folder / "reader-batches"
    if not runs.is_dir():
        return {}
    stages, owners = {}, set()
    parent = folder / "registration.json"
    registration = read(parent)
    cases = {c["task_id"]: c["task"] for c in registration["preflight"]["cases"] if "donor" in c}
    for path in sorted(runs.glob("*/registration.json")):
        batch = read(path)
        targets = batch["tasks"]
        require(batch["id"] == path.parent.name and targets and len(targets) == len(set(targets))
                and set(targets) <= cases.keys() and owners.isdisjoint(targets),
                "A reader batch has unregistered targets or overlaps another batch")
        owners.update(targets)
        controller = path.parent / "controller" / "registration.json"
        if controller.is_file():
            scope = read(controller)
            require(scope["batch_registration_sha256"] == digest(path.read_bytes())
                    and scope["parent_registration_sha256"] == digest(parent.read_bytes())
                    and scope.get("operations") == batch.get("operations") and scope["cases"] == [cases[t] for t in targets], "Reader batch belongs to different tasks or releases")
            stages.update(_batch_evidence(folder, path.parent, scope, include_funded=include_funded))
        else:
            require(not any(p for task in targets for p in (folder / ("task-" + cases[task])).glob("solve*")),
                    "A registered target has reader shards without its batch controller")
    return stages


def _groups(adapter, folder, controller, cfg, plan, cases, sleep):
    yield "solve", "solve", cases
    codes = {f"HTTP_{code}" for code in plan["retry"]["http_codes"]}
    waits = plan["retry"]["wait_seconds"]
    schedule = chain(waits, repeat(0)) if cfg.get("reader_operations") else waits
    for attempt, wait in enumerate(schedule, 1):
        retry = adapter._retry_cases(folder, cfg, cases, "solve", attempt, codes)
        if not retry:
            break
        stage = f"solve-retry-{attempt}"
        cfg["stage_kinds"][stage] = "solve"
        # Publication of the first grant follows the registered wait, including on a resumed batch.
        if wait and not (controller / f"{stage}-wave-1.json").is_file():
            sleep(wait)
        yield stage, stage, retry


def reader_budget_check(adapter, plan, cfg, cases, cap):
    """Apply the registered first-wave minimum to the roles and input limits these readers can use."""
    terms = cfg["expected_endpoint_terms"]
    if not any(c.get("banking_nl_assertion_judge", True) for c in cases):
        terms = {role: endpoint for role, endpoint in terms.items() if role != "grader"}
    budget_cfg = dict(cfg, expected_endpoint_terms=terms,
                      writing=dict(proposed_limits=dict(input_bytes=cfg["limits"]["max_input_bytes"])))
    return adapter._budget_check(plan, budget_cfg, cases, approved=cap)


def _summary(folder, controller, cases, available, result):
    latest, attempted, calls = {}, 0, 0
    unfunded_retry = set()
    reported, held = Decimal(0), Decimal(0)
    for name, entry in result["stages"].items():
        reported += Decimal(entry["reported"])
        held += Decimal(entry["held"])
        calls += entry["http"]
        if entry.get("status") == "not_started":
            if entry["reason"] == "spend_limit" and "arms" in entry:
                case = name.partition("/")[0].removeprefix("task-")
                unfunded_retry.update((case, arm) for arm in entry["arms"])
            continue
        _, ctl, rows = sealed_rows(safe_path(folder, name), entry["seal"])
        for row in rows:
            slot = ctl["profile"]["case"], row["arm"]
            latest[slot] = row
            attempted += row["status"] != "not_started"
    funding_stops = {"spend_limit", "unknown_cost", "unknown_cost_stop"}
    def settled(row):
        result = row.get("result") or {}
        reason = (result.get("error_code") or row.get("reason") or "").lower().removeprefix("batch_")
        return reason not in funding_stops
    accepted = sum(type((r.get("result") or {}).get("official_success")) is bool for r in latest.values())
    count = sum(slot in available and slot not in unfunded_retry and settled(row) for slot, row in latest.items())
    assigned = sum(len(c["arms"]) for c in cases)
    complete = not result["budget_deferred"] and count == len(available)
    require(reported + held == Decimal(result["total_covered_usd"]), "Reader batch accounting does not reconcile")
    return dict(complete=complete, completed_through="solve" if complete else None, assigned_readers=assigned,
                eligible_readers=len(available), settled_readers=count, unavailable_readers=assigned - len(available),
                accepted=accepted, attempted=attempted, requests=calls, reported_usd=str(reported), held_usd=str(held),
                budget_deferred=result["budget_deferred"], controller_sha256=digest((controller / "completion.json").read_bytes()))


def run_readers(adapter, plan, workspace, root, *, resolved, registration, sources, batch, directory, progress=None,
                model_resolver=None, pool_factory=None, sleep=time.sleep):
    """The caller owns the parent study lease and seals the spending authority before this call."""
    workspace, root, directory = Path(workspace), Path(root), Path(directory)
    folder = workspace / "runs" / plan["id"]
    controller = directory / "controller"
    marker = directory / "registration.json"
    original = folder / "registration.json"
    cfg = dict(resolved["cfg"], effective_sources=sources, stage_kinds=dict(resolved["cfg"]["stage_kinds"]))
    operations = batch.get("operations")
    if operations:
        cfg.update(reader_operations=True, max_concurrent=min(operations["max_concurrent_tasks"], len(batch["tasks"])))
    record = resolved["record"]
    targets = batch["tasks"]
    require(directory == safe_path(folder / "reader-batches", batch["id"]) and read(marker) == batch
            and is_cohort(plan) and plan["first_pass"]["draws"] == plan["repeats"] == 1 and cfg["solve_stages"] == ["solve"]
            and record == registration["preflight"] == read(folder / "resolved-plan.json") and read(original) == registration
            and record["adapter"] == adapter.name and record["plan_sha256"] == plan_digest(plan)
            and registration["approval"].get("through_stage") == "source"
            and cfg["registration_sha256"] == registration["approval"]["resolved_sha256"] == digest(canonical(record))
            and cfg["expected_endpoint_terms"] == registration["endpoint_terms"],
            "Reader batch requires the original source-only cohort registration and registered controls")
    writer = read(folder / "write-continuation.json")
    checkpoint = read(folder / "through-write.json")
    require(writer["stage"] == "write" and digest(canonical(sources)) == writer["effective_sources_sha256"] == batch["effective_sources_sha256"]
            and checkpoint["registration_sha256"] == digest(original.read_bytes()) == batch["source_registration_sha256"]
            and digest((folder / "through-write.json").read_bytes()) == batch["writer_checkpoint_sha256"]
            and not (folder / "completion.json").exists(),
            "Sealed writer checkpoint or effective first-pass release changed")
    readers = {c["task_id"]: c for c in record["cases"] if "donor" in c}
    require(targets and len(set(targets)) == len(targets) and set(targets) <= readers.keys(),
            "Reader selection needs distinct registered target tasks")
    cases = [readers[t] for t in targets]
    arms = {a for rep, reader in conditions(plan) for _, a in condition_banks(plan, arm_name(rep, reader))}
    require(len(arms) == 12 and all(c["scheduled_stages"] == ["solve"] and len(c["arms"]) == len(arms)
            and set(c["arms"]) == arms and c["donor"] == c["opening"] and c["donor"]["task_id"] == c["task_id"]
            and c["pool_sources"] == pool_sources(plan, c["task_id"]) for c in cases),
            "Each selected target requires its twelve registered own/mixed conditions and original first draw")
    needed = {t for c in cases for t in c["pool_sources"]}
    donors = [c for c in record["cases"] if "donor" not in c and c["task_id"] in needed]
    require({c["task_id"] for c in donors} == needed and all(f"task-{c['task']}/write" in checkpoint["stages"] for c in donors),
            "Selected pools have no registered writer checkpoint")
    writer_stages = {f"task-{c['task']}/write" for c in donors}
    verify_stage_entries(folder, {stage: checkpoint["stages"][stage] for stage in writer_stages})
    cfg["cohort_catalog"] = catalog = cohort_catalog(workspace, folder, cfg, donors, checkpoint["stages"])
    def released_note(task, form):
        bank = catalog[task]["banks"].get(form)
        if bank is None:
            return None
        require(len(bank["items"]) == 1, "Expected a single stored cohort note")
        item, = bank["items"]
        return dict(source_run=bank["source_run_id"], writer_run=bank["run_id"], snapshot=bank["snapshot_id"],
                    title_sha256=digest(item["title"].encode()), body_sha256=digest(item["body"].encode()))

    require(reader_input_digest(plan, targets, released_note) == batch["reader_inputs_sha256"],
            "A native writer run, snapshot or note differs from the released reader inputs")
    available = {(c["task"], a) for c in cases for a in c["arms"] if all(
        c["arm_writer"][a] in catalog[t]["banks"]
        for t in ([c["task_id"]] if c["contents"][a] == "own" else c["pool_sources"]))}
    require((batch["assigned_readers"], batch["eligible_readers"]) == (len(cases) * len(arms), len(available)),
            "Reader assignments or available writer pools differ from this batch")
    cap = approval_amount(batch["max_usd"])
    revisions = dict(cfg["expected_model_revisions"])
    if "grader" in cfg["expected_endpoint_terms"]:
        revisions["grader"] = cfg["judge"]["canonical_slug"]
    scope = dict(format="agent-memory-tau-reader-batch/v1", parent_registration_sha256=digest(original.read_bytes()),
                 batch_registration_sha256=digest(marker.read_bytes()), cases=[c["task"] for c in cases],
                 solve_stages=[k for k, v in cfg["stage_kinds"].items() if v == "solve"],
                 endpoint_terms=cfg["expected_endpoint_terms"], model_revisions=revisions)
    if operations:
        scope.update(operations=operations, retry_http_codes=plan["retry"]["http_codes"])
    reader_batch_stages(folder)
    _batch_evidence(folder, directory, scope)
    models = read_json(asset_bytes(root, cfg["endpoints"]))["models"]
    require(endpoint_terms(models) == {r: cfg["expected_endpoint_terms"][r] for r in ("worker", "user_simulator")}
            and {r: m["canonical_slug"] for r, m in models.items()} == cfg["expected_model_revisions"],
            "Registered worker or customer endpoint snapshot changed")
    common = dict(workspace=str(workspace), output=str(folder), cfg=cfg, models=models, conn=resolved["conn"],
                  identity=batch["implementation"], tasks={c["opening"]["task"]: resolved["tasks"][c["opening"]["task"]] for c in cases})

    if operations:
        common["reader_operations"] = dict(directory=str(controller), http_codes=plan["retry"]["http_codes"],
                                           transient_backoff_seconds=operations["transient_backoff_seconds"])

    def prepare_models():
        if "judge" in common:
            return
        reader_budget_check(adapter, plan, cfg, cases, cap)
        require(model_resolver is not None or os.environ.get("OPENROUTER_API_KEY"), "Missing OpenRouter reader credentials")
        live, judge = (model_resolver or study_models)(cfg, resolved["conn"])
        terms = endpoint_terms(dict(live, grader=judge) if judge is not None else live)
        require(terms == cfg["expected_endpoint_terms"]
                and {r: m.get("canonical_slug") for r, m in live.items()} == cfg["expected_model_revisions"]
                and (judge is None or judge["canonical_slug"] == cfg["judge"]["canonical_slug"]),
                "A reader, customer or judge changed revision, route or price")
        common.update(models=live, judge=judge)

    def verify_sealed(case, stage, grant):
        bank_shard((common, case, stage, grant))

    result = run_stages(controller, scope, _groups(adapter, folder, controller, cfg, plan, cases, sleep), (bank_shard, common),
                        shard_root=folder, total_usd_limit=cap, max_concurrent=cfg["max_concurrent"],
                        admission=adapter._admission(cfg), verify_sealed=verify_sealed, before_dispatch=prepare_models,
                        pool_factory=pool_factory, progress=progress)
    if operations:
        _batch_evidence(folder, directory, scope)
    return _summary(folder, controller, cases, available, result)
