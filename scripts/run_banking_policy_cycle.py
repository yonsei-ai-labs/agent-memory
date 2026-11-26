"""Compare clauses at four public decisions, then validate one writer policy on held-out banking tasks."""

import argparse
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import random
from tempfile import TemporaryDirectory

from agent_memory_eval.analysis.action_grounding import input_evidence, literal_matches
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.contracts.connection import provider_route
from agent_memory_eval.contracts.core import MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.clause_contrast import clause_variants, next_action, preload_at_decision
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.experiments.paired_repetitions import PairedRepetitions
from agent_memory_eval.memory.writing import writer_template
from agent_memory_eval.recording.assets import asset_text, publish_json
from agent_memory_eval.recording.journal import Journal, safe_path
from agent_memory_eval.runner import fixed_decision
from scripts import run_banking_memory as bank

PROFILE = "protocols/pilots/tau-banking-policy-cycle-v1.json"
PREFLIGHT = "artifacts/pilots/20261125-banking-policy-cycle-preflight.json"
APPROVAL = "artifacts/pilots/20261125-banking-policy-cycle-authorization.json"
CANDIDATE = "artifacts/pilots/20261125-banking-policy-cycle-candidate.json"
POLICY = MemoryPolicy(
    True,
    "none",
    False,
    page_size=4,
    read_mode="preload",
    max_items=4,
    max_payload_bytes=65536,
    max_return_bytes=131072,
)
ORIGIN = "fixed_decision.result"
SEARCH_TOOLS = {"grep"}
read = bank.matched.read


def save(path, value):
    try:
        publish_json(path, value)
    except FileExistsError:
        require(read(path) == value, f"Existing artifact differs at {path}")


def record_source_admission(directory, measured):
    from agent_memory_eval.analysis.trajectory import source_admission
    receipt = source_admission(measured)
    directory.mkdir(exist_ok=True)
    path = directory / "source-opportunity.json"
    present = {p.name for p in directory.iterdir()}
    require(not present or path.name in present, "Existing validation work has no source admission")
    require(receipt["decision"] != "stop" or present <= {path.name}, "Stopped study contains validation work")
    save(path, receipt)
    return receipt


def design(workspace):
    spec = read(ROOT / PROFILE)
    previous = read_json(asset_text(ROOT, spec["previous"]).encode())
    close = read_json(asset_text(workspace, spec["previous_close"]).encode())
    require(
        close["total_covered_usd"] == previous["total_covered_usd"] == spec["prior_usd"],
        "Different earlier experiment or ledger balance",
    )
    parent = read_json(asset_text(ROOT, spec["banking_source_protocol"]).encode())
    base = read_json(asset_text(ROOT, parent["base"]).encode())
    strict = read_json(asset_text(ROOT, spec["writing_protocol"]).encode())
    archived = read_json(asset_text(ROOT, spec["previous_allocation"]).encode())
    return spec, previous, base, strict, archived


def development(workspace):
    spec, previous, base, _, archived = design(workspace)
    instruction = asset_text(ROOT, base["delivery_instruction"])
    target_tasks = set(spec["decisions"])
    require(
        target_tasks == set(spec["clauses"]) and not target_tasks.intersection(spec["heldout"].values()),
        "Development and validation overlap or a note is missing",
    )
    forms, arms = spec["development_forms"], spec["development_arms"]
    cells, notes, sources = {}, {}, {}
    for task in sorted(target_tasks):
        rule = spec["decisions"][task]
        (first,) = [r for r in previous["sources"] if r["task"] == task]
        parent = safe_path(workspace, rule["source"]["path"]).parent
        source = RecordedDecisions(parent, rule["source_run"], rule["source"]["sha256"])
        require(
            first["log_sha256"] == rule["source"]["sha256"]
            and first["run"] == source.run
            and not source.start["source_run_ids"]
            and not source.start["start_snapshot_id"]
            and not any(e["type"].startswith("memory.") for e in source.events.values()),
            "Decision must come from the original, graded memory-free trajectory",
        )
        (score,) = source.of_type("outcome.scored")
        require(
            score["payload"]["grading_status"] == "graded"
            and score["payload"]["official_success"] is first["success"],
            "Ungraded or different task source",
        )
        worker = [r for r in source.requests if r["actor_role"] == "worker"]
        bank_root = workspace / "runs" / archived["series"] / ("task-" + task)
        available = bank.matched.sealed_rows(bank_root / "write")[2]
        old_readers = bank.matched.sealed_rows(bank_root / "solve")[2]
        for form in forms:
            ref = spec["clauses"][task][form]
            name = f"{task}/{form}"
            (writer,) = [r for r in available if r["arm"] == form]
            written = read_form_bank(
                bank_root / "write" / writer["directory"], expected_log_sha256=ref["writer_log_sha256"]
            )
            (original,) = written["items"]
            require(
                writer["run_id"] == ref["source_writer"] == written["run_id"]
                and (original["id"], original["version"]) == (ref["item_id"], ref["version"])
                and written["snapshot_id"] == ref["snapshot"]
                and written["source_run_id"] == source.run
                and written["feedback_level"] == spec["feedback"],
                "The immutable note or its source changed",
            )
            (identifier,) = rule["arguments"].values()
            variants, edits = clause_variants(original, ref["target"], ref["background"], identifier)
            notes[name] = dict(
                source_writer=written["run_id"],
                snapshot=written["snapshot_id"],
                source_item=original,
                source_log_sha256=ref["writer_log_sha256"],
                edits=edits,
                variants=variants,
            )
            # Match the original automatic-delivery convention against an actual reader, including its exact neutral notice.
            (prior,) = [r for r in old_readers if r["arm"] == form + "__preload"]
            logged = RecordedDecisions(
                bank_root / "solve" / prior["directory"], prior["run_id"], prior["result"]["log_sha256"]
            )
            prior_first = next(r for r in logged.requests if r["actor_role"] == "worker")
            require(
                preload_at_decision(source.blobs.get(worker[0]["request_ref"]), POLICY, instruction, original)
                == logged.blobs.get(prior_first["request_ref"]),
                "The public preload rendering changed",
            )
            for state in rule["states"]:
                case = task + "/" + str(state["ordinal"])
                req = worker[state["ordinal"] - 1]
                wire = source.blobs.get(req["request_ref"])
                require(
                    (req["request_id"], req["request_ref"]["sha256"])
                    == (state["request_id"], state["sha256"])
                    and not source.visible(req, wire)
                    and source.reply(req),
                    "Frozen public decision changed",
                )
                public = input_evidence(wire["messages"], wire["tools"], SEARCH_TOOLS)
                sources[case] = dict(
                    task=task,
                    ordinal=state["ordinal"],
                    source_request=req["request_id"],
                    source_log_sha256=first["log_sha256"],
                    source_wire_sha256=state["sha256"],
                    current_identifier_sources=literal_matches(identifier, public),
                    source_next_action=next_action(
                        source.reply(req),
                        wire["tools"],
                        rule["tool"],
                        rule["arguments"],
                        search_tools=SEARCH_TOOLS,
                    ),
                )
                common = None
                for arm in arms:
                    item = variants[arm]
                    new = preload_at_decision(wire, POLICY, instruction, item)
                    masked = deepcopy(new)
                    (public_item,) = json.loads(masked["messages"][1]["content"])["items"]
                    masked["messages"][1]["content"] = canonical(
                        dict(items=[dict(public_item, body="")])
                    ).decode()
                    common = common or masked
                    require(
                        common == masked and len(canonical(new)) <= spec["max_input_bytes"],
                        "Treatment changed something outside the note body or exceeds the input limit",
                    )
                    cells[case, form, arm] = (wire, POLICY, [item], new, [source.run, writer["run_id"]])
    rng = random.Random(spec["schedule_seed"])
    blocks = [
        (case, form, rep)
        for case in sorted(sources)
        for form in forms
        for rep in range(1, spec["repeats"] + 1)
    ]
    rng.shuffle(blocks)
    slots = []
    for case, form, rep in blocks:
        ordered = arms.copy()
        rng.shuffle(ordered)
        for arm in ordered:
            key = [spec["development_study"], case, form, rep, arm]
            cell = cells[case, form, arm]
            slots.append(
                dict(
                    slot=len(slots) + 1,
                    case=case,
                    task=sources[case]["task"],
                    form=form,
                    repeat=rep,
                    arm=arm,
                    run="r_" + digest(canonical(key))[:32],
                    initial_sha256=digest(canonical(cell[3])),
                    items_sha256=digest(canonical(cell[2])),
                )
            )
    require(
        len({s["run"] for s in slots}) == len(slots) == spec["validation"]["development_decisions"],
        "Incorrect or duplicate fixed-decision allocations",
    )
    return dict(sources=sources, notes=notes, slots=slots), cells


def preflight(workspace):
    spec, _, _, _, _ = design(workspace)
    probe, _ = development(workspace)
    _, _, _, cases, _, native = bank.prepare(workspace, heldout_tasks=spec["heldout"])
    original = read(ROOT / bank.PREFLIGHT)
    require(
        not {r["task"] for r in original["cases"]}.intersection(c["task"] for c in cases)
        and native["corpus_sha256"] == original["corpus_sha256"],
        "Holdout overlaps or uses another corpus",
    )
    return dict(
        protocol_sha256=digest((ROOT / PROFILE).read_bytes()),
        prior_usd=spec["prior_usd"],
        development=probe,
        validation=dict(tasks=cases, corpus_sha256=native["corpus_sha256"], assigned=spec["validation"]),
    )


def authorization(workspace, *, validation=False):
    cfg = read(ROOT / PROFILE)
    frozen = read(ROOT / PREFLIGHT)
    approval = read(ROOT / APPROVAL)
    require(
        frozen == preflight(workspace)
        and approval["preflight_sha256"] == digest(canonical(frozen))
        and approval["protocol_sha256"] == frozen["protocol_sha256"]
        and approval["prior_covered_usd"] == cfg["prior_usd"]
        and approval["user_authorization"]
        and approval["scope"] in {"development", "full"}
        and approval["dev_max_usd"] == cfg["dev_max_usd"]
        and (approval["scope"] != "full" or approval["total_usd"] == cfg["total_ceiling_usd"])
        and (
            approval["scope"] != "development"
            or Decimal(approval["total_usd"]) == Decimal(cfg["prior_usd"]) + Decimal(cfg["dev_max_usd"])
        )
        and (not validation or approval["scope"] == "full"),
        "Unapproved scope or changed preregistration",
    )
    return approval


def cell_for(cells, slot):
    return cells[slot["case"], slot["form"], slot["arm"]]


def classification(cells, spec, slot, choice):
    rule = spec["decisions"][slot["task"]]
    return next_action(
        choice, cell_for(cells, slot)[0]["tools"], rule["tool"], rule["arguments"], search_tools=SEARCH_TOOLS
    )


def smoke(workspace):
    """Exercise every distinct wire with the already recorded source reply, without a transport."""
    spec, _, _, _, _ = design(workspace)
    plan, cells = development(workspace)
    replies = {}
    for task, rule in spec["decisions"].items():
        log = RecordedDecisions(
            safe_path(workspace, rule["source"]["path"]).parent, rule["source_run"], rule["source"]["sha256"]
        )
        for state in rule["states"]:
            req = log.starts[state["request_id"]][0]["payload"]
            replies[task + "/" + str(state["ordinal"])] = log.reply(req)

    class RecordedReply:
        offline = True

        def __init__(self, expected, choice):
            self.expected, self.choice, self.calls = expected, choice, 0

        def respond(self, request):
            require(request == self.expected and self.calls == 0, "Unplanned mock submission")
            self.calls += 1
            return dict(
                id="offline-replay",
                choices=[self.choice],
                usage=dict(prompt_tokens=0, completion_tokens=0, total_tokens=0, cost=0),
            )

    checked = []
    for s in (s for s in plan["slots"] if s["repeat"] == 1):
        cell = cell_for(cells, s)
        require(digest(canonical(cell[3])) == s["initial_sha256"], "Registered live wire changed")
        # The illustrative transport requires its own model identity. Everything else is replayed verbatim.
        wire = dict(cell[3], model="synthetic-offline")
        mock_cell = (dict(cell[0], model="synthetic-offline"), cell[1], cell[2], wire, cell[4])
        mock_slot = dict(s, initial_sha256=digest(canonical(wire)))
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            client = RecordedReply(wire, replies[s["case"]])
            with Journal(folder, "clause_smoke", contract_version="1.2") as journal:
                fixed_decision.execute(
                    journal,
                    mock_slot,
                    mock_cell,
                    client,
                    ExecutionLimits(1, 0, 0, spec["max_input_bytes"]),
                    None,
                    result_origin=ORIGIN,
                )
            rec = fixed_decision.receipt(
                folder,
                mock_slot,
                {"pricing": {"prompt": 0, "completion": 0}},
                lambda slot, choice: classification(cells, spec, slot, choice),
                result_origin=ORIGIN,
                full_audit=True,
            )
            require(
                client.calls == 1
                and rec["complete"]
                and rec["confirmed_input_fields"] == ["body", "title"]
                and rec["behavior"] == plan["sources"][s["case"]]["source_next_action"],
                "Offline transport or rubric differs",
            )
            checked.append(
                dict(
                    case=s["case"],
                    form=s["form"],
                    arm=s["arm"],
                    wire=s["initial_sha256"],
                    original_response_class=rec["behavior"],
                )
            )
    return dict(
        transport="offline_recorded_source_reply",
        model_calls=0,
        transport_model="synthetic-offline",
        unique_wires=len(checked),
        checked=checked,
    )


def decisions(workspace):
    approval = authorization(workspace)
    spec, _, base, _, _ = design(workspace)
    plan, cells = development(workspace)
    endpoint_record = read_json(asset_text(ROOT, base["endpoints"]).encode())
    cfg = dict(
        name=spec["development_study"],
        status="ready",
        max_usd=approval["dev_max_usd"],
        max_seconds=4800,
        timeout_seconds=base["http_timeout_seconds"],
        max_model_requests=len(plan["slots"]),
        query_worker_limit=1,
        max_input_bytes=spec["max_input_bytes"],
        expected_canonical_slug=endpoint_record["models"]["worker"]["canonical_slug"],
        authorization=approval,
    )
    return fixed_decision.run_slots(
        workspace,
        cfg,
        plan["slots"],
        lambda s: cell_for(cells, s),
        base["endpoint_terms"]["worker"],
        lambda s, choice: classification(cells, spec, s, choice),
        result_origin=ORIGIN,
        journal_prefix="clause",
    )


def candidate_binding(workspace, spec, strict):
    proposal = read(ROOT / CANDIDATE)
    result = read_json(asset_text(ROOT, proposal["development_results"]).encode())
    dev = workspace / "runs" / spec["development_study"]
    seal = read(dev / "completion.json")
    require(
        seal == result["completion"]
        and digest((dev / "registration.json").read_bytes()) == seal["registration_sha256"]
        and result["protocol_sha256"] == digest((ROOT / PROFILE).read_bytes())
        and proposal["base_instruction"] == strict["common_instruction_ref"],
        "Proposal or development evidence changed",
    )
    citations = {(r["slot"], r["journal_sha256"]) for r in result["rows"] if r["behavior"] != "unobserved"}
    require(
        proposal["observations"]
        and all((c["slot"], c["journal_sha256"]) in citations for c in proposal["observations"])
        and proposal["hypothesis"]
        and proposal["falsifier"],
        "A candidate needs observed development evidence",
    )
    old = asset_text(ROOT, proposal["base_instruction"]).rstrip("\r\n")
    new = asset_text(ROOT, proposal["candidate_instruction"]).rstrip("\r\n")
    extra = proposal["add"]
    require(
        extra.strip() == extra
        and extra
        and "\n" not in extra
        and new == old + "\n" + extra
        and not any(v in extra for d in spec["decisions"].values() for v in d["arguments"].values())
        and not any(t in extra for t in [*spec["decisions"], *spec["heldout"].values()]),
        "Candidate must append exactly one domain-neutral instruction",
    )
    covered = Decimal(spec["prior_usd"]) + Decimal(seal["confirmed_usd"]) + Decimal(seal["held_usd"])
    return proposal, covered


def writer_hashes(conn, cfg, source, instruction):
    model = dict(
        conn["worker"]["parameters"],
        model=conn["worker"]["model_id"],
        provider=provider_route(conn, "worker"),
        stream=False,
        seed=cfg["writer_seed"],
    )
    inputs = {}
    for form, line in cfg["writing"]["lines"].items():
        wire = writer_template(
            model, instruction + "\n" + line, completion_contract=cfg["writing"]["completion_contract"]
        )
        wire["messages"] = [
            wire.pop("system"),
            dict(role="user", content=source.public_context),
            dict(
                role="user", content=canonical(source.feedback_message(cfg["writer_feedback_level"])).decode()
            ),
        ]
        inputs[form] = digest(canonical(wire))
    return inputs


def policy_stage_job(args):
    workspace, root, cfg, base, conn, case, task, stage, identity, models, judge, grant = args
    if "writer_instruction" in case:
        cfg = deepcopy(cfg)
        cfg["writing"]["common_instruction_ref"] = case["writer_instruction"]
    return bank.matched.stage_job(
        (workspace, root, cfg, base, conn, case, task, stage, identity, models, judge, grant)
    )


def registered_models(cfg, conn):
    models, judge = bank.matched.study_models(cfg, conn)
    require(
        {role: m.get("canonical_slug") for role, m in models.items()} == cfg["expected_model_revisions"],
        "Validation model revisions differ from the original worker or customer",
    )
    return models, judge


def validation_run(workspace, phase):
    approval = authorization(workspace, validation=True)
    spec, _, _, strict, _ = design(workspace)
    proposal, covered = candidate_binding(workspace, spec, strict)
    _, base, conn, cases, tasks, _ = bank.prepare(workspace, heldout_tasks=spec["heldout"])
    cfg = bank.required_design(base, strict)
    endpoints = read_json(asset_text(ROOT, base["endpoints"]).encode())["models"]
    cfg.update(
        writer_feedback_level=spec["feedback"],
        total_usd_limit=approval["total_usd"],
        record_unfunded_shards=True,
        expected_endpoint_terms={r: cfg["endpoint_terms"][r] for r in ("worker", "user_simulator")},
        expected_model_revisions={r: endpoints[r]["canonical_slug"] for r in ("worker", "user_simulator")},
    )
    common = dict(
        cycle_sha256=digest((ROOT / PROFILE).read_bytes()),
        candidate_sha256=digest((ROOT / CANDIDATE).read_bytes()),
        preflight_sha256=approval["preflight_sha256"],
        approval=approval,
    )
    return run_paired_validation(workspace, phase, spec, proposal, covered, cfg, conn, cases, tasks, common)


def run_paired_validation(workspace, phase, spec, proposal, covered, cfg, conn, cases, tasks, common, *, source_check=None):
    """Share one untouched first pass between registered writers, then run their full consultations."""
    policy_root = workspace if spec.get("policy_registration") else ROOT
    require(phase in {"sources", "validation"} and spec["validation"]["writer_policies"] == ["existing", "candidate"],
            "Unknown paired validation phase or writer assignment")
    repeats = PairedRepetitions.from_spec(spec)
    if spec.get("format") == "banking-registered-writer-pairs/v3":
        require(spec["reader_admission"] == "sealed_bank" and spec["interruption_policy"] == bank.matched.RESUME_UNSTARTED,
                "Unknown admission or interruption policy")
        cfg.update(reader_admission=spec["reader_admission"], interruption_policy=spec["interruption_policy"])
    stages = repeats.stages()
    source_root = workspace / "runs" / spec["source_study"]
    for case in cases:
        case["reader_modes"] = [spec.get("reader", "preload")]
    if phase == "sources":
        cfg.update(name=spec["source_study"], stages=["source"])
        record = dict(**common, cases=cases, phase=phase, assigned=len(cases), prior_covered_usd=str(covered))
    else:
        if "source_opportunity_requirement" in spec:
            require(source_check and source_check["assessment"]["met"]
                    and all(source_check["assessment"].get(k) == v for k, v in spec["source_opportunity_requirement"].items()),
                    "Registered source opportunity requirement was not met")
        source_reg, source_close = (
            read(source_root / "registration.json"),
            read(source_root / "completion.json"),
        )
        require(
            source_reg["preflight"]
            == dict(
                **common, cases=cases, phase="sources", assigned=len(cases), prior_covered_usd=str(covered)
            )
            and source_close["registration_sha256"]
            == digest((source_root / "registration.json").read_bytes()),
            "Unregistered validation first passes",
        )
        source_rid = digest(canonical(source_reg["preflight"]))
        covered = Decimal(source_close["total_covered_usd"])
        paired, task_map = [], {}
        for case in cases:
            tid = case["task_id"]
            folder = source_root / ("task-" + tid) / "source"
            seal = source_close["stages"][str(folder.relative_to(source_root))]
            source = None
            if seal.get("status") != "not_started":
                require(
                    digest((folder / "completion.json").read_bytes()) == seal["seal"],
                    "Changed first-pass shard",
                )
                source = bank.matched.source_from(folder, tid)
            for _, cell in repeats.writer_cells(case, spec["validation"]["writer_policies"]):
                instruction = proposal[
                    "candidate_instruction" if cell["writer_policy"] == "candidate" else "base_instruction"
                ]
                cell["writer_instruction"] = instruction
                if source is None:
                    cell.update(scheduled_stages=[], missing="first_pass_ungraded_or_unstarted")
                else:
                    cell["imported_first_pass"] = dict(
                        directory=str(folder.relative_to(workspace / "runs")),
                        registration_sha256=source_rid,
                        run_id=source.source_run_id,
                        log_sha256=source.source_log_sha256,
                        public_context_sha256=source.public_context_sha256,
                    )
                    cell["first_writer_inputs"] = writer_hashes(
                        conn, cfg, source, asset_text(policy_root, instruction).rstrip("\r\n")
                    )
                paired.append(cell)
                task_map[cell["task"]] = tasks[tid]
        cases, tasks = paired, task_map
        cfg.update(name=spec["validation_study"], stages=[name for name, _, _ in stages])
        if repeats.readers_per_bank > 1:
            cfg["stage_kinds"] = {name: operation for name, operation, _ in stages}
        schedule = {r["task"]: {name: bank.matched.jobs_for(r, kind) for name, kind, _ in stages} for r in cases}
        counts = {operation: sum(len(row[name]) for row in schedule.values() for name, kind, _ in stages
                                 if kind == operation) for operation in ("write", "solve")}
        require(all(counts[s] == spec["validation"][s] for s in counts), "Validation allocation changed")
        record = dict(
            **common,
            cases=cases,
            phase=phase,
            assigned=counts,
            prior_covered_usd=str(covered),
            source_registration_sha256=source_close["registration_sha256"],
            schedule_sha256=digest(canonical(schedule)),
        )
        if source_check is not None:
            require(source_check["source"]["completion_sha256"] == digest((source_root / "completion.json").read_bytes()),
                    "Source opportunity check used another cohort")
            record["source_opportunity_check"] = source_check
    cfg.update(prior_covered_usd=str(covered), registration_sha256=digest(canonical(record)))
    bank.matched.run_study(
        workspace,
        cfg,
        {},
        conn,
        cases,
        tasks,
        record,
        stage_runner=policy_stage_job,
        model_resolver=registered_models,
    )
    return read(workspace / "runs" / cfg["name"] / "completion.json")


def registered_pairs(workspace, profile, mode):
    """Use the existing paired full-consultation runner with a fixed multi-customer cohort."""
    from agent_memory_eval.adapters.tau_banking_opening import grader_controls, target_accounts
    from agent_memory_eval.adapters.tau_banking_results import known_customer_groups
    from agent_memory_eval.policies.writer_pairs import writer_pairs

    spec = read(ROOT / profile)
    if "source_opportunity_requirement" in spec:
        from agent_memory_eval.adapters.banking_trajectories import CUES
        from agent_memory_eval.analysis.trajectory import validate_opportunity_requirement
        validate_opportunity_requirement(spec["source_opportunity_requirement"], CUES)
    census = spec["format"] in {"banking-registered-writer-pairs/v2", "banking-registered-writer-pairs/v3"}
    repeats = PairedRepetitions.from_spec(spec)
    require(census == ("repetitions" in spec), "The cohort protocol must register its repetition axes")
    portable = spec.get("policy_registration")
    if portable is not None:
        from agent_memory_eval.runner.policy_round import verify_native_registration
        verify_native_registration(ROOT / profile, spec, "banking-opening")
    supported_readers = {"preload", "on_demand"} if portable else {"preload"}
    require((census or spec["format"] == "banking-registered-writer-pairs/v1") and spec["adapter"] == "account_opening"
            and mode in {"preflight", "sources", "validation"} and spec["reader"] in supported_readers
            and spec["feedback"] == "result_only", "Unknown paired consultation protocol")
    require(spec["banking_profile"]["path"] == bank.PROFILE, "Different banking base")
    asset_text(ROOT, spec["banking_profile"])
    policy_root = workspace if portable else ROOT
    strict = read_json(asset_text(policy_root, spec["required_writer"]).encode())
    require(strict["common_instruction_ref"] == spec["base_instruction"], "Changed baseline author")
    old = asset_text(policy_root, spec["base_instruction"]).rstrip("\r\n")
    new = asset_text(policy_root, spec["candidate_instruction"]).rstrip("\r\n")
    clause = asset_text(policy_root, spec["clause"]).rstrip("\r\n")
    conditions, pairs = writer_pairs(strict["lines"], clause)
    instructions = {c["id"]: c["prompt"] for c in conditions}
    for form, pair in pairs.items():
        require(new + "\n" + strict["lines"][form] == old + "\n" + instructions[pair["candidate"]],
                "Writer policies differ beyond the registered common clause")

    _, base, conn, _, _, cohort = bank.prepare(workspace)
    from tau2.domains.banking_knowledge.environment import get_tasks
    modules = bank.matched.modules_for(workspace)
    native = {task.id: task for task in get_tasks()}
    cfg = bank.required_design(base, strict)
    if portable:
        cfg["writing"]["proposed_limits"]["bank_items"] = 1
        cfg["policy_registration"] = portable
        cfg["delivery_instruction"] = portable["reader_instruction"]
    endpoints = read_json(asset_text(ROOT, cfg["endpoints"]).encode())["models"]
    roles = ("worker", "user_simulator")
    cfg.update(writer_feedback_level=spec["feedback"], max_concurrent=spec.get("max_concurrent", 2), record_unfunded_shards=True,
               expected_endpoint_terms={r: cfg["endpoint_terms"][r] for r in roles},
               expected_model_revisions={r: endpoints[r]["canonical_slug"] for r in roles})
    domain = workspace / "data/tau2-bench/data/tau2/domains/banking_knowledge"
    groups = known_customer_groups(domain, spec["previous_cases"], spec["tasks"])
    cases, tasks, controls = [], {}, {}
    if census:
        from agent_memory_eval.adapters.tau_banking_opening import account_opening_population

        require(account_opening_population(native.values()) == set(spec["tasks"]), "Account-opening census changed")
    customers = set()
    for tid, product in spec["tasks"].items():
        task = native[tid]
        targets = target_accounts(task)
        owners = {target["user_id"] for target in targets}
        require(task.evaluation_criteria.reward_basis == ["DB"] and not task.evaluation_criteria.nl_assertions
                and len(owners) == 1 and targets[0]["account_type"] == product
                and groups[tid]["identified"] and not groups[tid]["overlapping_development"]
                and (census or not customers.intersection(owners)), "Changed native task, product or customer overlap")
        owner, = owners
        verification = {a.arguments.get("user_id") for a in task.evaluation_criteria.actions
                        if a.name == "log_verification"}
        require(verification == owners, "Reference openings do not belong to the verified customer")
        customers.add(owner)
        case, tasks[tid] = bank.native_case(cfg, conn, modules, task, product)
        case.update(customer=digest(owner.encode()), reader_modes=[spec["reader"]])
        if census:
            case["scheduled_stages"] = ["source"] + [stage for stage, _, _ in repeats.stages()]
        cases.append(case)
        controls[tid] = grader_controls(task)

    write_count = len(cases) * len(pairs) * 2 * repeats.writer_draws
    solve_count = write_count * repeats.readers_per_bank
    expected_validation = dict(writer_policies=["existing", "candidate"], write=write_count,
                               solve=solve_count, second_pass_no_memory=0)
    require(len(cases) == spec["source"] and spec["validation"] == expected_validation, "Changed allocation")
    schedule = {}
    for case in cases:
        schedule[case["task"]] = {"source": bank.matched.jobs_for(case, "source")}
        for label, cell in repeats.writer_cells(case, spec["validation"]["writer_policies"]):
            schedule[case["task"]][label] = {stage: bank.matched.jobs_for(cell, kind)
                                             for stage, kind, _ in repeats.stages()}
    check = dict(protocol_sha256=digest((ROOT / profile).read_bytes()), cases=cases, customers=len(customers),
                 customer_overlap=groups, corpus_sha256=cohort["corpus_sha256"],
                 native_revision=cohort["native_revision"], expected_models=cfg["expected_model_revisions"],
                 schedule_sha256=digest(canonical(schedule)), writer_conditions=conditions, grader_controls=controls,
                 assigned=dict(source=len(cases), write=write_count, solve=solve_count, second_pass_no_memory=0),
                 model_requests=0)
    if "source_opportunity_requirement" in spec:
        check["source_opportunity_requirement"] = spec["source_opportunity_requirement"]
    if spec["format"] == "banking-registered-writer-pairs/v3":
        require(spec["reader_admission"] == "sealed_bank" and spec["interruption_policy"] == bank.matched.RESUME_UNSTARTED,
                "Unknown admission or interruption policy")
        asset_text(ROOT, spec["ratings_schema"])
        check.update(reader_admission=spec["reader_admission"], interruption_policy=spec["interruption_policy"],
                     ratings_schema_sha256=spec["ratings_schema"]["sha256"])
    if census:
        from agent_memory_eval.adapters.tau_banking_opening import initial_record_audit
        require(len(customers) == spec["expected_customers"], "Changed independent customer count")
        check.update(native_tasks_screened=len(native), repetitions=spec["repetitions"],
                     initial_records=initial_record_audit(domain, native, spec["tasks"]))
    if mode == "preflight":
        return check
    control_root = workspace if portable else ROOT
    require(check == read(control_root / spec["preflight_file"]), "Registered offline preflight differs")
    approval = read(control_root / spec["authorization_file"])
    expected = dict(check["assigned"])
    if approval["scope"] == "sources":
        expected.update(write=0, solve=0)
    cap = Decimal(approval["maximum_usd"])
    require(approval["scope"] in {"sources", "full"} and (mode == "sources" or approval["scope"] == "full")
            and approval["assigned"] == expected and approval["preflight_sha256"] == digest(canonical(check))
            and approval["protocol_sha256"] == check["protocol_sha256"] and approval["user_authorization"]
            and cap.is_finite() and cap > 0, "No matching authorization for this phase")
    cfg["total_usd_limit"] = str(cap)
    common = dict(protocol_sha256=check["protocol_sha256"], preflight_sha256=approval["preflight_sha256"],
                  approval=approval)
    if mode == "validation" and "source_opportunity_requirement" in spec:
        from agent_memory_eval.adapters.banking_trajectories import banking_report
        measured = banking_report(ROOT / profile, workspace, workspace / "data/tau2-bench", sources_only=True)
        directory = safe_path(workspace / "runs", spec["validation_study"])
        source_check = record_source_admission(directory, measured)
        if source_check["decision"] == "stop":
            return source_check
        return run_paired_validation(workspace, mode, spec, spec, Decimal(0), cfg, conn, cases, tasks, common,
                                     source_check=source_check)
    return run_paired_validation(workspace, mode, spec, spec, Decimal(0), cfg, conn, cases, tasks, common)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "smoke", "decisions", "sources", "validation"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--profile", type=Path, help="Registered multi-customer writer comparison")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if args.profile:
        result = registered_pairs(workspace, args.profile, args.mode)
    elif args.mode == "preflight":
        result = preflight(workspace)
    elif args.mode == "smoke":
        result = smoke(workspace)
    elif args.mode == "decisions":
        result = decisions(workspace)
    else:
        result = validation_run(workspace, args.mode)
    if args.output:
        save(args.output, result)
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
