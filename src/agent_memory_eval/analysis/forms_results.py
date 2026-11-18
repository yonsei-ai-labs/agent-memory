"""Paired, task-weighted contrasts and independent evidence checks for the forms batch."""

from collections import Counter, defaultdict
from decimal import Decimal
from itertools import combinations
from math import ceil, floor
from pathlib import Path
from random import Random

from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, digest, read_json
from agent_memory_eval.experiments.forms_batch import FORMS, ordered_jobs
from agent_memory_eval.memory.context import memory_system_suffix, past_memory_instruction, snapshot_message
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path


def contrast(rows, treatment, control, groups, *, resamples=5000, seed=20261118):
    paired, assignments = defaultdict(list), defaultdict(list)
    counts = Counter()
    examples = defaultdict(list)
    for task, rep in sorted({(r["task_id"], r["repetition"]) for r in rows if r["arm"] == treatment}):
        cells = {r["arm"]: r for r in rows if r["task_id"] == task and r["repetition"] == rep}
        if control not in cells:
            continue
        t, c = [cells[arm].get("result", {}).get("official_success") for arm in (treatment, control)]
        # A known half-pair narrows the possible difference over the planned population.
        assignments[task].append(((0 if t is None else int(t)) - (1 if c is None else int(c)),
                                  (1 if t is None else int(t)) - (0 if c is None else int(c))))
        if type(t) is not bool or type(c) is not bool:
            counts["ungraded"] += 1
            continue
        label = {(True, False): "gain", (False, True): "loss", (True, True): "both_success",
                 (False, False): "both_failure"}[t, c]
        counts[label] += 1
        examples[label].append(dict(task_id=task, repetition=rep, treatment_job=cells[treatment]["index"],
                                    control_job=cells[control]["index"]))
        paired[task].append(int(t) - int(c))
    means = {task: sum(ds) / len(ds) for task, ds in paired.items()}
    available_groups = defaultdict(list)
    for task, mean in means.items():
        available_groups[groups[task]].append(mean)
    def average(values):
        return sum(values) / len(values) if values else None
    def estimate(sample):
        return average([v for group in sample for v in available_groups[group]])
    def pct(x):
        return round(100 * x, 6) if x is not None else None
    bounds = [pct(average([average([pair[index] for pair in bounds]) for bounds in assignments.values()]))
              for index in range(2)]
    keys = sorted(available_groups)
    interval, leave_one = None, {}
    if len(keys) >= 2:
        rng = Random(seed)
        draws = sorted(estimate(rng.choices(keys, k=len(keys))) for _ in range(resamples))
        def percentile(frac):
            pos = (resamples - 1) * frac
            return pct(draws[floor(pos)] + (pos - floor(pos)) * (draws[ceil(pos)] - draws[floor(pos)]))
        interval = [percentile(.025), percentile(.975)]
        leave_one = {group: pct(estimate([g for g in keys if g != group])) for group in keys}
    for value in examples.values():
        value.sort(key=lambda pair: pair["treatment_job"])
    return dict(treatment=treatment, control=control, planned_pairs=sum(map(len, assignments.values())),
        jointly_graded_pairs=sum(map(len, paired.values())), tasks_with_pairs=len(means), customer_groups_with_pairs=len(keys),
        counts={key: counts[key] for key in ("gain", "loss", "both_success", "both_failure", "ungraded")},
        task_equal_difference_pp=pct(average(list(means.values()))), possible_difference_pp=bounds,
        descriptive_cluster_interval_pp=interval, leave_one_group_out_pp=leave_one,
        task_difference_pp={task: pct(mean) for task, mean in means.items()},
        review_cases={key: examples[key] if key in {"gain", "loss"} else examples[key][:1]
                      for key in ("gain", "loss", "both_success", "both_failure")})


def checked_rows(output, *, observe_job=None, observe_parent=None):
    output = Path(output).resolve()
    read = lambda name: read_json(safe_path(output, name).read_bytes())
    control, completion, rows = read("control.json"), read("completion.json"), read("allocations.json")
    design = control["design"]
    if (digest((output / "allocations.json").read_bytes()) != completion["allocation_sha256"]
            or digest((output / "control.json").read_bytes()) != completion["control_sha256"]):
        raise ContractError("Allocation or control bytes differ from the completion record")
    expected = ordered_jobs(design)
    if (len(rows) != len(expected) or len({r["run_id"] for r in rows}) != len(rows)
            or any(any(e[k] != r[k] for k in e) or r["directory"] != f'jobs/{r["index"]:04d}'
                   for e, r in zip(expected, rows))):
        raise ContractError("The final ledger differs from the frozen allocation")
    usage = defaultdict(lambda: dict(http_calls=0, reported_usd=Decimal(0), total_tokens=0, unknown_count=0))
    sources = {r["task_id"]: r for r in design["source"]["cohort"]}
    prompt_hashes = (design.get("writer_prompt_hashes")
                     or {c["id"]: c["writer_instruction_sha256"] for c in design.get("conditions", [])})
    written = {}
    delivery_arms = design.get("delivery_arms", {})
    if delivery_arms:
        from agent_memory_eval.experiments.prompt_delivery import source_banks
        pin = design["writer_parent"]
        parent = safe_path(output.parent, pin["name"])
        _, _, original_writers, banks = source_banks(parent, pin)
        for (rep, task, arm), bank in banks.items():
            written[task, rep, arm] = bank["run_id"], bank["snapshot_id"], bank["items"]
            if bank["public_context_sha256"] != sources[task]["public_context_sha256"]:
                raise ContractError("Imported writer refers to a different public experience")
        if observe_parent:
            for row in original_writers:
                if row["status"] in {"completed", "empty"}:
                    folder = safe_path(parent, row["directory"])
                    scan, state, _ = read_experiment(folder)
                    observe_parent(row, folder, scan, state, BlobStore(folder))
    for row in rows:
        folder = safe_path(output, row["directory"])
        if row["status"] == "not_started":
            if folder.exists() or "result" in row or not row.get("reason"):
                raise ContractError("An unstarted row has effects or lacks a reason")
            continue
        result = row["result"]
        if (row["status"] != result["status"] or row["run_id"] != result["run_id"] or
                digest((folder / "log.jsonl").read_bytes()) != result["log_sha256"]):
            raise ContractError("The recorded job differs from the closed run")
        evidence = read_experiment(folder)
        audit = audit_requests(folder, evidence=evidence)
        scan, state, _ = evidence
        blobs = BlobStore(folder)
        key = row["task_id"], row["repetition"], row["arm"]
        ends = [e["payload"] for e in scan["events"].values() if e["type"] == "run.ended"]
        if len(ends) != 1 or len(scan["starts"]) != 1 or row["run_id"] not in scan["starts"]:
            raise ContractError("Allocation does not have exactly one closed run")
        run = row["run_id"]
        start = scan["starts"][run]["payload"]
        if row["phase"] == "write":
            cfg = state["memory_writes"][run]
            if prompt_hashes and digest(cfg["instruction"].encode()) != prompt_hashes[row["arm"]]:
                raise ContractError("The complete writer instruction differs from its configured version")
            if (start["source_run_ids"] != [sources[row["task_id"]]["run_id"]]
                    or cfg["feedback"]["level"] != "none" or cfg["completion_contract"] != "writer-finish/v1"
                    or not cfg["instruction"].endswith("\n" + design["writing"]["lines"][row["arm"]])):
                raise ContractError("Archived writer differs from its first-pass source or instruction")
            if row["status"] in {"completed", "empty"}:
                items = snapshot_items(blobs, state, result["snapshot_id"])
                written[key] = (run, result["snapshot_id"], items)
        else:
            cfg = state["assignments"][run]
            arm = row["arm"]
            delivery = delivery_arms.get(arm)
            if (cfg["task_id"], cfg["repetition"], cfg["arm"]) != key:
                raise ContractError("Native assignment differs from the planned cell")
            if design.get("format") == "agent-memory-new-customer-design/v1":
                native = blobs.get(cfg["native_config_ref"])
                connection = blobs.get(cfg["connection_ref"])
                if (native["task_split_name"] != design["source"]["split"] or native["task_ids"] != [row["task_id"]]
                        or connection["benchmark"]["split"] != native["task_split_name"]):
                    raise ContractError("Fresh-customer episode was not run on its official split")
            items = snapshot_items(blobs, state, start["start_snapshot_id"]) if arm != "no_memory" else []
            policy = MemoryPolicy(**cfg["policy"])
            if delivery and (cfg.get("fixed_bank_delivery") != delivery or policy.read_mode != delivery["read_mode"]
                             or policy.write_timing != "none" or not policy.enabled):
                raise ContractError("The observed read policy differs from the frozen-bank comparison")
            if delivery or arm in design["writing"]["lines"]:
                writer_key = (key[0], key[1], delivery["writer"]) if delivery else key
                if written.get(writer_key) != (cfg["source_writer_run_id"], cfg["source_writer_snapshot_id"], items):
                    raise ContractError("Delivered memory differs from the assigned sealed writer snapshot")
            elif arm == "public_record":
                if (len(items) != 1 or items[0]["title"] != "Earlier public interaction"
                        or digest(items[0]["body"].encode()) != sources[row["task_id"]]["public_context_sha256"]):
                    raise ContractError("Raw record differs from the archived public source")
            elif items or (start["bank_id"] is None) != (arm == "no_memory"):
                raise ContractError("Pure no-memory or empty preload has an incorrect bank")
            system = blobs.get(start["task_ref"])["content"]
            if arm != "no_memory":
                instruction = blobs.get(cfg["instruction_ref"])
                if digest(instruction.encode()) != design["second_pass"]["solve_instruction_ref"]["sha256"]:
                    raise ContractError("Solve instruction changed")
                system += memory_system_suffix(policy, instruction if delivery else past_memory_instruction(instruction))
            for e in scan["events"].values():
                if delivery and e["type"] == "memory.returned":
                    p = e["payload"]
                    heads = {item["id"]: item["version"] for item in items}
                    if (p["bank_id"] != start["bank_id"] or p["operation"] not in {"list", "get"}
                            or policy.read_mode != "on_demand"
                            or any(heads.get(v["item_id"]) != v["version_id"] for v in p["items"])):
                        raise ContractError("A memory lookup escaped the assigned bank or read mode")
                if delivery and e["type"] == "memory.preloaded" and not policy.preloads:
                    raise ContractError("The query condition received an automatic preload")
                if e["type"] != "model.request_started":
                    continue
                payload = e["payload"]
                wire = blobs.get(payload["request_ref"])
                allowed = memory_tools(policy.tools("solve")) if delivery and payload["actor_role"] == "worker" else []
                if [t for t in (wire.get("tools") or ()) if t["function"]["name"].startswith("memory_")] != allowed:
                    raise ContractError("Memory tools differ from the declared retrieval policy")
                if payload["actor_role"] == "worker":
                    if wire["messages"][0] != dict(role="system", content=system) or (
                            policy.preloads and wire["messages"][1] != snapshot_message(items)):
                        raise ContractError("Worker wire differs from the assigned policy and full snapshot")
                    if delivery and not policy.preloads and snapshot_message(items) in wire["messages"][1:]:
                        raise ContractError("An entire snapshot leaked into a query-only worker input")
                elif payload["actor_role"] != "user_simulator" or "seed" in wire:
                    raise ContractError("Unexpected role or simulator wire seed")
            if result["memory_input_spans"] != audit["memory_input_spans"]:
                raise ContractError("Published exposure differs from request manifests")
            scored = [e["payload"] for e in scan["events"].values() if e["type"] == "outcome.scored"]
            if (len(scored) != 1 or any(scored[0][k] != result[k] for k in ("grading_status", "official_success", "official_score"))
                    or scored[0]["official_artifact_ref"] != result["report_ref"]):
                raise ContractError("Published score differs from the scored event")
            if result["grading_status"] == "graded":
                report = BlobStore(folder).get(result["report_ref"])
                if (report["task_id"] != row["task_id"] or report["termination_reason"] not in {"user_stop", "agent_stop"}
                        or report["reward_info"]["reward"] != result["official_score"]
                        or (result["official_score"] == 1.0) != result["official_success"]):
                    raise ContractError("Published official result differs from native scoring")
            elif result["official_success"] is not None or result["official_score"] is not None:
                raise ContractError("An ungraded native episode received an official outcome")
        billed = result["billing_by_role"]
        if (sum(v["requests"] for v in billed.values()) != audit["requests"]
                or sum(v["unknown_count"] for v in billed.values()) != audit["unknown_cost_requests"]
                or sum(Decimal(v["reported_usd"]) for v in billed.values()) != Decimal(audit["observed_cost_usd_decimal"])):
            raise ContractError("Published usage differs from the audited responses")
        for actor, values in billed.items():
            total = usage[actor]
            for key in total:
                total[key] += Decimal(values[key]) if key == "reported_usd" else values[key]
        if observe_job is not None:
            observe_job(row, folder, scan, state, blobs)
    known = sum(v["reported_usd"] for v in usage.values())
    if (sum(v["http_calls"] for v in usage.values()) != completion["http_calls"]
            or abs(known - Decimal(str(completion["reported_usd"]))) > Decimal("0.00000001")
            or any(v["unknown_count"] for v in usage.values()) != completion["unknown_cost"]):
        raise ContractError("Batch guard and independent journal totals differ")
    for values in usage.values():
        values["reported_usd"] = str(values["reported_usd"])
    return design, completion, rows, dict(usage)


def summarize(output):
    design, completion, rows, usage = checked_rows(output)
    source_outcomes = read_json((Path(output) / "control.json").read_bytes())["source_outcomes"]
    return summarize_checked(design, completion, rows, usage, source_outcomes)


def summarize_checked(design, completion, rows, usage, source_outcomes):
    """Summarize already verified rows, including a separately audited allocation subset."""
    cohort = {r["task_id"]: r for r in design["source"]["cohort"]}
    groups = {t: r["source_group"] for t, r in cohort.items()}
    solves = [r for r in rows if r["phase"] == "solve"]
    methods = [*[(form, "no_memory") for form in FORMS], *[(form, "empty_preload") for form in FORMS],
               *combinations(FORMS, 2), ("no_memory", "empty_preload"),
               *[(arm, ref) for ref in ("summary", "public_record") for arm in (*FORMS, "no_memory", "empty_preload")],
               ("summary", "public_record")]
    contrasts = [contrast(solves, arm, ctrl, groups) for arm, ctrl in methods]
    strata = {}
    for variable, flags in (("reference_db_write", {k: v["reference_has_db_write"] for k, v in cohort.items()}),
                            ("source_official_success", source_outcomes)):
        for flag in (True, False):
            subset = [r for r in solves if flags[r["task_id"]] is flag]
            strata[f"{variable}={flag}"] = [contrast(subset, arm, "no_memory", groups) for arm in FORMS]
    baseline_by_task = defaultdict(dict)
    for cell in solves:
        if cell["arm"] == "no_memory":
            baseline_by_task[cell["task_id"]][cell["repetition"]] = cell.get("result", {}).get("official_success")
    complete = [pair for pair in baseline_by_task.values() if all(type(pair.get(i)) is bool for i in (1, 2))]
    baseline_stability = dict(planned_tasks=len(baseline_by_task), tasks_with_both_scores=len(complete),
                              flipped=sum(pair[1] != pair[2] for pair in complete))
    arm_stats = []
    for phase, arm in sorted({(r["phase"], r["arm"]) for r in rows}):
        subset = [r for r in rows if (r["phase"], r["arm"]) == (phase, arm)]
        statuses = Counter(r["status"] for r in subset)
        billing = defaultdict(Decimal)
        for r in subset:
            for values in r.get("result", {}).get("billing_by_role", {}).values():
                for k in ("http_calls", "reported_usd", "total_tokens", "unknown_count"):
                    billing[k] += Decimal(values[k])
        arm_stats.append(dict(phase=phase, arm=arm, planned=len(subset), statuses=dict(statuses),
            graded=sum(r.get("result", {}).get("grading_status") == "graded" for r in subset),
            successful=sum(r.get("result", {}).get("official_success") is True for r in subset),
            http_calls=int(billing["http_calls"]), reported_tokens=int(billing["total_tokens"]),
            known_cost_usd=str(billing["reported_usd"]), unknown_cost_requests=int(billing["unknown_count"])))
    return dict(format="agent-memory-two-pass-memory-forms-results/v1", completion=completion, usage_by_role=usage,
                by_arm=arm_stats, paired_contrasts=contrasts, strata=strata, baseline_repetition=baseline_stability,
                assignment_statuses=dict(Counter(r["status"] for r in rows)))
