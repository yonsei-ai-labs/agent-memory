"""Compile user-defined writing conditions against a pinned public two-pass cohort."""
from copy import deepcopy
import os
from pathlib import Path
import re

from jsonschema import Draft202012Validator, ValidationError

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT, SCHEMA_ROOT, leaves, pointer
from agent_memory_eval.experiments.first_pass_match import BASELINE_HTTP_SECONDS, BASELINE_INPUT_BYTES
from agent_memory_eval.experiments.forms_batch import BatchGuard, endpoint_terms, execute_prepared, live_models, prepare
from agent_memory_eval.memory.writing import REQUIRED_STORE_CONTRACT
from agent_memory_eval.policies.writer_pairs import writer_pairs
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity

FORMAT = "agent-memory-prompt-comparison/v1"
DESIGN_FORMAT = "agent-memory-prompt-comparison-design/v1"
FIRST_PASS_FORMAT = "agent-memory-prompt-comparison/v2"
FIRST_PASS_DESIGN = "agent-memory-prompt-comparison-design/v2"
PAIRED_FORMS_FORMAT = "agent-memory-prompt-comparison/v3"
PAIRED_FORMS_DESIGN = "agent-memory-prompt-comparison-design/v3"
CONTROLS = ("no_memory", "empty_preload")
SLUG = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def _keys(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ContractError(f"Unexpected or missing fields in {name}: expected {sorted(keys)}")


def _schedule(design):
    namespace = design["allocation"]["namespace"]
    conditions = [c["id"] for c in design["conditions"]]
    delivery = design.get("delivery_arms")
    if design["format"] == PAIRED_FORMS_DESIGN or "representation_pairs" in design:
        forms = {c["representation"]: c["prompt"] for c in design["conditions"] if c["id"] == c["representation"]}
        declared, pairs = writer_pairs(forms, design.get("writer_clause"))
        actual = [{k: v for k, v in c.items() if k != "writer_instruction_sha256"} for c in design["conditions"]]
        if (declared != actual or pairs != design.get("representation_pairs")
                or {c["id"]: c["prompt"] for c in declared} != design["writing"]["lines"]):
            raise ContractError("Representation pairs differ from their registered writer conditions")
    if design["format"] in (FIRST_PASS_DESIGN, PAIRED_FORMS_DESIGN):
        if (design["second_pass"].get("baseline") != "archived_first_pass" or design["second_pass"]["matched_controls"]
                or set(conditions).intersection(CONTROLS) or delivery):
            raise ContractError("The first-pass baseline requires a memory-only second-pass schedule")
        solve_arms = conditions
    else:
        solve_arms = delivery or [*conditions, *CONTROLS]
    blocks = []
    for seeds in design["model"]["seed_pairs"]:
        rep = seeds["repetition"]
        def rank(*parts):
            return digest(canonical([namespace, rep, *parts]))
        tasks = [t["task_id"] for t in design["source"]["cohort"]]
        for task in sorted(tasks, key=lambda t: rank(t, "block")):
            blocks.append(dict(task_id=task, repetition=rep,
                write=[] if delivery else sorted(conditions, key=lambda a: rank(task, "write", a)),
                solve=sorted(solve_arms, key=lambda a: rank(task, "solve", a))))
    sequence = [[phase, b["repetition"], b["task_id"], arm] for phase in ("write", "solve")
                for b in blocks for arm in b[phase]]
    return blocks, sequence


def comparison_jobs(design):
    blocks, sequence = _schedule(design)
    if blocks != design["allocation"]["blocks"] or digest(canonical(sequence)) != design["allocation"]["ordered_jobs_sha256"]:
        raise ContractError("Prompt comparison allocation changed")
    return [dict(index=i, phase=p, repetition=r, task_id=t, arm=a) for i, (p, r, t, a) in enumerate(sequence, 1)]


def compile_config(root, config):
    """Separate condition identity, representation, prompt text/version and delivery."""
    mode = config.get("format") if isinstance(config, dict) else None
    multi = mode == PAIRED_FORMS_FORMAT
    first_pass = mode in (FIRST_PASS_FORMAT, PAIRED_FORMS_FORMAT)
    if first_pass:
        schema = read_json((SCHEMA_ROOT / f"prompt-comparison-{mode.rsplit('/', 1)[1]}.schema.json").read_bytes())
        try:
            Draft202012Validator(schema).validate(config)
        except ValidationError as error:
            raise ContractError(f"Invalid first-pass comparison at {error.json_path}: {error.message}") from error
    else:
        _keys(config, ("format", "status", "name", "base_design_ref", "endpoint_ref", "conditions", "read_mode",
                       "task_sets", "seed_pairs", "guards", "http_timeout_seconds", "selection_reason"), "comparison")
    if (mode not in (FORMAT, FIRST_PASS_FORMAT, PAIRED_FORMS_FORMAT) or config["status"] not in ("draft", "ready")
            or config["read_mode"] != "preload" or not isinstance(config["name"], str) or not SLUG.fullmatch(config["name"])):
        raise ContractError("Expected a named, full-preload writing comparison")
    if not first_pass or config["guards"] is not None:
        _keys(config["guards"], ("max_calls", "max_usd", "max_seconds"), "batch limits")
        BatchGuard(**config["guards"])
    if type(config["http_timeout_seconds"]) is not int or config["http_timeout_seconds"] <= 0:
        raise ContractError("HTTP timeout must be a positive integer")
    base = read_json(asset_text(root, config["base_design_ref"]).encode())
    if base.get("format") != "agent-memory-two-pass-memory-forms-study-design/v1":
        raise ContractError("Expected the pinned first-pass forms design")
    writing = deepcopy(base["writing"])
    if multi:
        conditions, pairs = writer_pairs(config["forms"], config["writer_clause"])
        writing.update(common_instruction_ref=deepcopy(config["common_writer_ref"]), completion_contract=REQUIRED_STORE_CONTRACT,
                       bank_rule="Seal every required source and representation bank before opening its readers.")
    else:
        conditions = config["conditions"]
        if not isinstance(conditions, list) or len(conditions) != 2:
            raise ContractError("Declare the reference and comparison writing conditions in that order")
    common = asset_text(root, writing["common_instruction_ref"]).rstrip("\r\n")
    writing["lines"] = {}
    resolved = []
    for c in conditions:
        _keys(c, ("id", "representation", "prompt_version", "prompt"), "writing condition")
        if not all(isinstance(v, str) and v.strip() for v in c.values()) or not SLUG.fullmatch(c["id"]):
            raise ContractError("Each condition needs a distinct ID, representation, prompt version and text")
        if c["id"] in (*CONTROLS, "public_record") or c["id"] in writing["lines"]:
            raise ContractError("Duplicate or reserved writing condition")
        writing["lines"][c["id"]] = c["prompt"]
        resolved.append(dict(c, writer_instruction_sha256=digest((common + "\n" + c["prompt"]).encode())))
    if resolved[0]["writer_instruction_sha256"] == resolved[1]["writer_instruction_sha256"]:
        raise ContractError("The two assembled writer prompts are identical")
    sets = config["task_sets"]
    _keys(sets, ("development", "evaluation"), "task sets")
    if any(not isinstance(v, list) or not all(isinstance(t, str) for t in v) for v in sets.values()):
        raise ContractError("Task sets must be lists of task IDs")
    selected = [*sets["development"], *sets["evaluation"]]
    cohort = base["source"]["cohort"]
    by_task = {c["task_id"]: c for c in cohort}
    if not selected or len(set(selected)) != len(selected) or set(selected) - by_task.keys():
        raise ContractError("Task assignments must be distinct and present in the pinned baseline")
    dev_groups = {by_task[t]["source_group"] for t in sets["development"]}
    if any(by_task[t]["source_group"] in dev_groups for t in sets["evaluation"]):
        raise ContractError("Development and evaluation tasks share a customer group")
    seeds = config["seed_pairs"]
    if not isinstance(seeds, list) or not seeds:
        raise ContractError("At least one repetition is required")
    for rep, s in enumerate(seeds, 1):
        _keys(s, ("repetition", "writer", "native_and_solver"), "repetition")
        if s["repetition"] != rep or any(type(v) is not int or v < 0 for v in s.values()):
            raise ContractError("Use consecutive repetitions with explicit nonnegative seeds")
    source = deepcopy(base["source"])
    source["excluded_tasks"] = sorted(set(source["excluded_tasks"]) | (by_task.keys() - set(selected)), key=int)
    source["cohort"] = [by_task[t] for t in selected]
    source.update(selection=config["selection_reason"], exclusion_basis="Explicit task sets in the comparison config",
                  cohort_sha256=digest(canonical(source["cohort"])))
    solve = {k: deepcopy(base["second_pass"][k]) for k in ("solve_instruction_ref", "proposed_limits", "no_memory", "preload")}
    solve.update(primary_arms=list(writing["lines"]), diagnostic_task_ids=[], matched_controls=[] if first_pass else list(CONTROLS),
                 read_mode=config["read_mode"])
    if first_pass:
        solve["baseline"] = config["baseline"]
        # The archived client had one shared call limit for both actors.
        limits = solve["proposed_limits"]
        limits.update(worker_requests=limits["max_total_requests_per_episode"],
                      user_simulator_requests=limits["max_total_requests_per_episode"], max_input_bytes=BASELINE_INPUT_BYTES)
        if config["http_timeout_seconds"] != BASELINE_HTTP_SECONDS:
            raise ContractError("Same-task comparisons must use the archived HTTP timeout")
    design_format = {FORMAT: DESIGN_FORMAT, FIRST_PASS_FORMAT: FIRST_PASS_DESIGN, PAIRED_FORMS_FORMAT: PAIRED_FORMS_DESIGN}[mode]
    design = dict(format=design_format, conditions=resolved,
                  task_sets=deepcopy(sets), source=source, writing=writing, second_pass=solve,
                  model=dict(base["model"], seed_pairs=deepcopy(seeds)),
                  allocation=dict(namespace=config["name"], planned=dict(eligible_source_episodes=len(selected))))
    if first_pass:
        design["preload_notice_version"] = "memory-only-v1"
    if multi:
        design["representation_pairs"] = pairs
        design["writer_clause"] = config["writer_clause"]
    if "candidate_proposal" in config:
        proposal = config["candidate_proposal"]
        field, target = ("writer_clause", config["writer_clause"]) if multi else ("writer_prompt", resolved[1]["prompt"])
        if (proposal[field] != target
                or any(e["task_id"] not in sets["development"] for e in proposal["evidence"])):
            raise ContractError("The proposal must change the candidate writer and cite development tasks only")
        design["candidate_proposal"] = deepcopy(proposal)
    blocks, sequence = _schedule(design)
    design["allocation"].update(blocks=blocks, ordered_jobs_sha256=digest(canonical(sequence)))
    return design


def verify_proposal_quotes(proposal, public_messages):
    for citation in proposal["evidence"]:
        if citation["task_id"] not in public_messages:
            raise ContractError("Proposal citation is absent from the public development experience")
        public = {"messages": public_messages[citation["task_id"]]}
        location, quote = citation["pointer"], citation["quote"]
        if location not in leaves(public):
            raise ContractError("Proposal citation is absent from the public development experience")
        value = pointer(public, location)
        if not isinstance(value, str) or not quote.strip() or value.count(quote) != 1:
            raise ContractError("Proposal quote must match one excerpt in the cited public field")


def run_comparison(root, workspace, checkout, config_path, *, live=False):
    root, workspace = Path(root).resolve(), Path(workspace).resolve()
    config_data = Path(config_path).read_bytes()
    config = read_json(config_data)
    design = compile_config(root, config)
    if live and config["status"] != "ready":
        raise ContractError("Only a ready comparison can send model requests")
    plan = prepare(root, workspace, checkout, design)
    if design["format"] in (FIRST_PASS_DESIGN, PAIRED_FORMS_DESIGN):
        if "candidate_proposal" in design:
            messages = {t: read_json(plan["sources"][t].public_context.encode())["messages"] for t in design["task_sets"]["development"]}
            verify_proposal_quotes(design["candidate_proposal"], messages)
        episodes = [r for r in plan["jobs"] if r["phase"] == "solve"]
        plan["summary"].update(baseline=design["second_pass"]["baseline"],
                               second_pass_by_policy={c["id"]: sum(r["arm"] == c["id"] for r in episodes)
                                                      for c in design["conditions"]},
                               second_pass_no_memory=sum(r["arm"] == "no_memory" for r in episodes))
    if not live:
        return dict(preflight=plan["summary"], conditions=design["conditions"], task_sets=design["task_sets"],
                    **({"representation_pairs": design["representation_pairs"]} if "representation_pairs" in design else {}),
                    guards=config["guards"], config_sha256=digest(config_data))
    identity = implementation_identity(root)
    destination = safe_path(workspace, "runs/" + config["name"])
    if destination.exists() or not os.environ.get("OPENROUTER_API_KEY"):
        raise ContractError("Live comparison requires a new output directory and a configured OpenRouter key")
    expected = read_json(asset_text(root, config["endpoint_ref"]).encode())["models"]
    models = live_models(plan["connection"])
    if endpoint_terms(models) != endpoint_terms(expected) or implementation_identity(root) != identity:
        raise ContractError("Endpoint terms or frozen implementation changed before the first send")
    profile = dict(config, config_sha256=digest(config_data), implementation_commit=identity["implementation_commit"],
                   output_root="runs/" + config["name"])
    return execute_prepared(profile, root, destination, identity, design, plan, models, BatchGuard(**config["guards"]))
