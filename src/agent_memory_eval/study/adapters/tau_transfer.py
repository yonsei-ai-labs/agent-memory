"""Transfer frozen τ² writer banks between native tasks, sharing only the target's public opening."""

from copy import deepcopy
from pathlib import Path

from agent_memory_eval.adapters.banking_memory_cohort import observation
from agent_memory_eval.analysis.efficiency import token_usage_by_role
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.recording.journal import BlobStore, safe_path
from agent_memory_eval.study.adapters.tau_banking import TARGET_NO_MEMORY, Tau2Study, outcome_fields
from agent_memory_eval.study.plan import plan_digest
from agent_memory_eval.study.records import read, sealed_rows
from agent_memory_eval.study.transfer import allocation_rows, reader_arms


def baseline_stages(plan):
    count = plan["transfer"].get("target_no_memory_repeats", 1)
    require(type(count) is int and count >= 1, "Target no-memory repeats must include the original opening")
    return ["source", *(f"target-no-memory-{n}" for n in range(2, count + 1))]


def registered_rows(shard, case, registration, kind, stage_record):
    _, control, rows = sealed_rows(shard, stage_record.get("seal"))
    profile = control["profile"]
    require(profile["registration_sha256"] == registration and profile["case"] == case["task"]
            and profile["stage"] == control["design"]["stage"] == kind, "Transfer shard belongs to another allocation")
    return rows


def check_opening(observed, first, *, no_memory):
    opening = observed.get("opening_sha256")
    worker = observed.get("first_worker_sha256")
    require(not worker or opening, "Target worker request has no sealed public opening")
    if opening and first["run"]:
        require(observed["opening_source_run"] == first["run"] and opening == first["opening_sha256"],
                "Target episode did not restore its registered opening")
    if no_memory and worker and first["first_worker_sha256"]:
        require(worker == first["first_worker_sha256"], "No-memory controls received different first requests")


def dialogue_counts(observed):
    """The official trajectory supplies the public customer turns and questions asked by the worker."""
    effort = observed.get("effort", {})
    return dict(status="observed" if "customer_questions" in observed else "unavailable",
                customer_turns=effort.get("customer_turns"), question_turns=effort.get("question_turns"),
                repeated_question_turns=effort.get("repeated_question_turns"))


def baseline_observation(shard, entry, case, index, workspace, rule, registration):
    reason = entry.get("reason") or ("unsealed" if shard.is_dir() else "not_started")
    item = dict(index=index, run=None, status="missing", success=None, missing_reason=reason, opening_sha256=None,
                opening_source_run=None, first_worker_sha256=None, effort_from_first_worker=None, decision=None,
                dialogue=dialogue_counts({}), token_usage_by_role=None, state_diagnostics=None, exposure=None)
    if not (shard / "completion.json").is_file():
        return item
    kind = "source" if index == 1 else TARGET_NO_MEMORY
    row, = registered_rows(shard, case, registration, kind, entry)
    require(row["task_id"] == case["task_id"] and row["arm"] == "no_memory" and "source_task_id" not in row
            and row.get("target_no_memory_index") == (index if index > 1 else None),
            "Expected the target's own pure no-memory consultation at this repeat index")
    transfer = dict(rule=rule, bank="empty", domain=case["domain"], workspace=workspace, native_sha256=case["native_sha256"])
    observed = observation(shard, row, transfer=transfer)
    status, success, missing, _ = outcome_fields(observed)
    result = row.get("result") or {}
    item.update(status=status, success=success, missing_reason=missing, dialogue=dialogue_counts(observed),
                billing=result.get("billing_by_role", {}), state_diagnostics=observed.get("state_diagnostics"))
    if observed["journal"]:
        require(result["bank_id"] is None and result["start_snapshot_id"] is None and not observed["returns"]
                and result["memory_input_spans"] == 0, "No-memory target received a memory assignment")
        item.update(run=observed["run"], session_id=result["session_id"], journal=observed["journal"])
        for key in ("opening_sha256", "opening_source_run", "first_worker_sha256", "effort_from_first_worker",
                    "token_usage_by_role", "decision", "termination_reason", "official_components"):
            item[key] = observed.get(key)
        item["exposure"] = {key: observed.get(key) for key in (
            "reads", "returns", "returned_titles", "returned_bodies", "dispatched_worker",
            "title_requests", "body_requests", "first_fields",
        )}
    elif result.get("partial_log_sha256"):
        item.update(run=observed["run"], partial_journal=result["partial_log_sha256"])
    return item


def observed_baselines(plan, folder, workspace, cases, registration, stages):
    rules = {p["target"]: p["decision"] for p in plan["transfer"]["pairs"]}
    baselines, sessions = {}, set()
    scheduled = baseline_stages(plan)
    for case in cases:
        task, draw = case["task_id"], case["source_draw"]
        if "donor" in case or task not in rules:
            continue
        require((task, draw) not in baselines and case["scheduled_stages"] == scheduled,
                "Missing or duplicate target no-memory allocation")
        repeats = []
        for index, stage in enumerate(scheduled, 1):
            name = f"task-{case['task']}/{stage}"
            item = baseline_observation(folder / name, stages.get(name, {}), case, index, workspace, rules[task], registration)
            if item.get("session_id"):
                require(item["session_id"] not in sessions, "A no-memory target session was reused")
                sessions.add(item["session_id"])
            if index == 1:
                require(item["opening_source_run"] is None, "Original target opening was imported")
            else:
                check_opening(item, repeats[0], no_memory=True)
            repeats.append(item)
        baselines[task, draw] = repeats
    require(set(baselines) == {(task, draw) for task in rules for draw in range(1, plan["first_pass"]["draws"] + 1)},
            "Target no-memory schedule differs from the plan")
    return baselines


def donor_tokens(workspace, folder, donor, registration, stages):
    imported = donor.get("imported_first_pass")
    name = f"task-{donor['task']}/source"
    source = safe_path(workspace / "runs", imported["directory"]) if imported else folder / name
    if not (source / "completion.json").is_file():
        return None
    _, control, (row,) = sealed_rows(source, None if imported else stages.get(name, {}).get("seal"))
    require(control["profile"]["registration_sha256"] == (imported["registration_sha256"] if imported else registration)
            and control["design"]["stage"] == "source" and row["arm"] == "no_memory" and row["task_id"] == donor["task_id"],
            "Wrong no-memory donor for the writer's token usage")
    checksum = (row.get("result") or {}).get("log_sha256")
    if not checksum:
        return None
    if imported:
        require(row["run_id"] == imported["run_id"] and checksum == imported["log_sha256"], "Imported donor changed")
    log = RecordedDecisions(safe_path(source, row["directory"]), row["run_id"], checksum)
    return dict(run=row["run_id"], journal=checksum, token_usage_by_role=token_usage_by_role(log.events.values()))


def writer_banks(workspace, folder, cases, registration, stages):
    by_donor, notes = {}, {}
    for donor in cases:
        if "write" not in donor["scheduled_stages"]:
            continue
        name = f"task-{donor['task']}/write"
        shard = folder / name
        if not (shard / "completion.json").is_file():
            continue
        banks = {}
        source = donor_tokens(workspace, folder, donor, registration, stages)
        for row in registered_rows(shard, donor, registration, "write", stages.get(name, {})):
            if row["status"] != "completed":
                continue
            job = safe_path(shard, row["directory"])
            log_hash = row["result"]["log_sha256"]
            bank = read_form_bank(job, expected_log_sha256=log_hash)
            form = row["arm"]
            require(bank["task_id"] == donor["task_id"] == row["task_id"] and bank["arm"] == form
                    and bank["run_id"] == row["run_id"] and form not in banks, "Writer belongs to another donor")
            if source:
                require(bank["source_run_id"] == source["run"] and bank["source_log_sha256"] == source["journal"],
                        "Writer source and donor token receipt differ")
            checksum = bank["public_context_sha256"]
            public = dict(path=f"blobs/sha256/{checksum}", sha256=checksum)
            require(BlobStore(job).get(public)["kind"] == "past_public_experience", "Expected public writer experience")
            public["path"] = str((job / public["path"]).relative_to(workspace))
            log = RecordedDecisions(job, bank["run_id"], log_hash)
            banks[form] = bank
            notes[donor["task_id"], donor["source_draw"], form] = dict(
                form=form, source_run=bank["source_run_id"], writer_run=bank["run_id"],
                snapshot=bank["snapshot_id"], feedback_success=bank.get("result_feedback", {}).get("success"),
                items=bank["items"], public_experience_ref=public, token_usage_by_role=token_usage_by_role(log.events.values()),
                source_token_usage_by_role=source["token_usage_by_role"] if source else None,
            )
        by_donor[donor["task"]] = banks
    return by_donor, notes


class TauTransfer(Tau2Study):
    cross_task = True

    def __init__(self, name):
        self.name = name

    def _configure(self, plan, workspace, root):
        setup, cfg, conn, policies, forms = super()._configure(plan, workspace, root)
        cfg["transfer_tools"] = {p["target"]: p["decision"]["tools"] for p in plan["transfer"]["pairs"]}
        if "target_no_memory_repeats" in plan["transfer"]:
            cfg["target_no_memory_stages"] = baseline_stages(plan)[1:]
            cfg["stage_kinds"].update(dict.fromkeys(cfg["target_no_memory_stages"], TARGET_NO_MEMORY))
        return setup, cfg, conn, policies, forms

    def _reused(self, workspace, plan, name, case):
        if any(p["target"] == case["task_id"] for p in plan["transfer"]["pairs"]):
            return {}  # A target opening is collected independently of earlier scores.
        return super()._reused(workspace, plan, name, case)

    def resolve(self, plan, workspace, root):
        native_plan = deepcopy(plan)
        pinned = native_plan["first_pass"].pop("required_sources_sha256", None)
        resolved = super().resolve(native_plan, workspace, root)
        cfg, record = resolved["cfg"], resolved["record"]
        pairs = plan["transfer"]["pairs"]
        source_tasks = {p["source"] for p in pairs}
        target_tasks = {p["target"] for p in pairs}
        cases = resolved["cases"]
        lookup = {(c["task_id"], c["source_draw"]): c for c in cases}
        donors = [c for c in cases if c["task_id"] in source_tasks]
        if pinned:
            require(all("imported_first_pass" in c for c in donors), "Missing a pinned transfer donor")
            identities = [
                [c["task"], c["imported_first_pass"]["run_id"], c["imported_first_pass"]["log_sha256"]]
                for c in donors
            ]
            require(digest(canonical(identities)) == pinned, "The transfer donors differ from the registered cohort")
        for case in cases:
            case["scheduled_stages"] = [
                s
                for s in case["scheduled_stages"]
                if s == "source" or s == "write" and case["task_id"] in source_tasks
            ]
            if case["task_id"] in target_tasks:
                case["scheduled_stages"].extend(cfg.get("target_no_memory_stages", ()))
        readers = []
        for pair in pairs:
            for draw in range(1, plan["first_pass"]["draws"] + 1):
                donor, opening = (lookup[pair[k], draw] for k in ("source", "target"))
                relation = pair["customer_relation"]
                same_customer = donor["customer"] == opening["customer"]
                require(
                    not any(c["customer"].startswith("unresolved-") for c in (donor, opening))
                    and same_customer == (relation == "same_customer"),
                    "Unverified transfer customer relationship",
                )
                contents, writers, modes = {}, {}, {}
                for name in donor["arms"]:
                    for bank, arm in zip(("real", "empty"), reader_arms(name)):
                        contents[arm] = bank
                        writers[arm] = donor["arm_writer"][name]
                        modes[arm] = donor["arm_mode"][name]
                        if name in cfg.get("query_tools_by_arm", {}):
                            cfg["query_tools_by_arm"][arm] = cfg["query_tools_by_arm"][name]
                readers.append(
                    dict(
                        opening,
                        task=f"{donor['task']}--to--{opening['task_id']}",
                        donor=donor,
                        opening=opening,
                        decision=pair["decision"],
                        customer_relation=relation,
                        arms=list(contents),
                        arm_writer=writers,
                        arm_mode=modes,
                        contents=contents,
                        scheduled_stages=cfg["solve_stages"],
                    )
                )
        cases.extend(readers)
        record.update(
            format="agent-memory-transfer-resolved/v1",
            plan_sha256=plan_digest(plan),
            assigned=dict(
                source=sum("source" in c["scheduled_stages"] for c in donors),
                reused_first_pass=sum("imported_first_pass" in c for c in donors),
                target_openings=sum(
                    "source" in c["scheduled_stages"] for c in cases if c["task_id"] not in source_tasks
                ),
                reused_target_openings=sum(
                    "imported_first_pass" in c for c in lookup.values() if c["task_id"] not in source_tasks
                ),
                write=sum(len(c["representations"]) for c in donors),
                solve=sum(len(c["arms"]) for c in readers) * plan["repeats"],
            ),
        )
        if "target_no_memory_stages" in cfg:
            extra = cfg["target_no_memory_stages"]
            record["stages"][1:1] = extra
            openings = record["assigned"]["target_openings"]
            record["assigned"].update(target_no_memory=openings * (1 + len(extra)),
                                      additional_target_no_memory=openings * len(extra))
        record["budget_check"] = self._budget_check(plan, cfg, cases)
        cfg["registration_sha256"] = digest(canonical(record))
        return resolved

    def _admission(self, cfg):
        normal = super()._admission(cfg)

        def opening_status(root, opening):
            folder = root / ("task-" + opening["task"]) / "source"
            if not (folder / "completion.json").is_file():
                return "opening_not_started"
            _, ctl, (row,) = sealed_rows(folder)
            require(ctl["profile"]["case"] == opening["task"] and ctl["profile"]["registration_sha256"] == cfg["registration_sha256"]
                    and row["arm"] == "no_memory" and row["task_id"] == opening["task_id"],
                    "Wrong independent target opening")
            result = row.get("result") or {}
            if not result.get("log_sha256"):
                return "opening_unavailable"
            log = RecordedDecisions(safe_path(folder, row["directory"]), row["run_id"], result["log_sha256"])
            if not any(
                e["payload"].get("origin") == "task_start.first_decision" for e in log.of_type("business.transition")
            ):
                return "opening_unavailable"
            if [r["actor_role"] for r in log.requests[:2]] != ["user_simulator", "worker"]:
                return "opening_unavailable"
            return None

        def admit(root, stage, case):
            if cfg["stage_kinds"][stage] == TARGET_NO_MEMORY:
                return opening_status(root, case)
            if "donor" not in case:
                return normal(root, stage, case)
            donor = case["donor"]
            if "first_pass_missing" in donor:
                return "donor_ungraded"
            reason = normal(root, "write", donor)
            if reason:
                return "donor_" + reason
            reason = opening_status(root, case["opening"])
            if reason:
                return reason
            if not (root / ("task-" + donor["task"]) / "write/completion.json").is_file():
                return "writer_not_started"
            return None

        return admit

    def outcomes(self, plan, workspace, root):
        workspace = Path(workspace)
        folder = workspace / "runs" / plan["id"]
        saved = folder / "resolved-plan.json"
        if not saved.exists():
            return allocation_rows(plan, [])
        record = read(saved)
        require(record["plan_sha256"] == plan_digest(plan), "Transfer recording belongs to another plan")
        if (folder / "registration.json").exists():
            require(read(folder / "registration.json")["preflight"] == record, "Changed transfer registration")
        stages = read(folder / "completion.json")["stages"] if (folder / "completion.json").is_file() else {}
        registration = digest(canonical(record))
        baselines = observed_baselines(plan, folder, workspace, record["cases"], registration, stages)
        donors, written_memories = writer_banks(workspace, folder, record["cases"], registration, stages)
        attempts = []
        for case in (c for c in record["cases"] if "donor" in c):
            donor = case["donor"]
            require(case["opening"]["task_id"] == case["task_id"] and
                    case["opening"]["source_draw"] == case["source_draw"] == donor["source_draw"],
                    "Transfer reader target or source draw changed")
            banks = donors.get(donor["task"], {})
            first = baselines[case["task_id"], case["source_draw"]][0]
            task_dir = folder / ("task-" + case["task"])
            for n, base in enumerate(case["scheduled_stages"], 1):
                for k in range(len(plan["retry"]["wait_seconds"]) + 1):
                    stage = base if k == 0 else f"{base}-retry-{k}"
                    shard = task_dir / stage
                    entry = stages.get(f"{task_dir.name}/{stage}", {})
                    reason = entry.get("reason") if entry.get("status") == "not_started" else None
                    if reason:
                        rows = [dict(arm=a, status="not_started", reason=reason) for a in case["arms"]]
                    elif (shard / "completion.json").is_file():
                        rows = registered_rows(shard, case, registration, "solve", entry)
                    else:
                        continue
                    for row in rows:
                        arm = row["arm"]
                        if row.get("result"):
                            require(
                                row["task_id"] == case["task_id"] and row["source_task_id"] == donor["task_id"],
                                "Target outcome belongs to another transfer pair",
                            )
                        bank = banks.get(case["arm_writer"][arm])
                        observed = observation(
                            shard, row, writer=bank, transfer=dict(rule=case["decision"], bank=case["contents"][arm],
                                domain=case["domain"], workspace=workspace, native_sha256=case["native_sha256"])
                        )
                        status, success, missing, _ = outcome_fields(observed)
                        check_opening(observed, first, no_memory=False)
                        attempts.append(
                            dict(
                                source=donor["task_id"],
                                task=case["task_id"],
                                draw=donor["source_draw"],
                                repeat=n,
                                origin=plan["id"],
                                condition=arm,
                                attempt=k,
                                status=status,
                                success=success,
                                missing_reason=missing,
                                run=observed.get("run") if observed.get("journal") else None,
                                opening_sha256=observed.get("opening_sha256"),
                                opening_source_run=observed.get("opening_source_run"),
                                first_worker_sha256=observed.get("first_worker_sha256"),
                                decision=observed.get("decision"),
                                dialogue=dialogue_counts(observed),
                                token_usage_by_role=observed.pop("token_usage_by_role", None),
                                state_diagnostics=observed.pop("state_diagnostics", None),
                                termination_reason=observed.pop("termination_reason", None),
                                official_components=observed.pop("official_components", None),
                                effort_from_first_worker=observed.pop("effort_from_first_worker", None),
                                reads=observed["reads"],
                                billing=(row.get("result") or {}).get("billing_by_role", {}),
                                evidence=observed,
                                source_run=bank["source_run_id"] if bank else None,
                                writer_run=bank["run_id"] if bank else None,
                                donor_snapshot=bank["snapshot_id"] if bank else None,
                            )
                        )
        return allocation_rows(plan, attempts, written_memories=written_memories, target_baselines=baselines)
