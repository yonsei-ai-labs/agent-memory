"""Compare archived airline first-pass inputs with their actual second-pass jobs, offline."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.journal import BlobStore, safe_path


# Continuations sometimes copy earlier jobs. The final new-customer ledger
# includes the parent; the three training ledgers contain disjoint attempts.
FAMILIES = {
    "training_forms": [
        ("tau2-two-pass-forms-luna-deepseek-01", "3528eed073199e62e38b2b30959f6b368c3a00e4e6a54021270de548e8b91381"),
        ("tau2-two-pass-forms-luna-deepseek-supplement-01", "40004aaf9f1d127940bf9782864abe1cb6080796a73fa7bd1141a0e4652a5756"),
        ("tau2-two-pass-forms-luna-deepseek-supplement-02", "44f67d55b2dcd4b5c75224a67bc2b30ab552e30bccaf0e81535679dad662a358")],
    "payment_recheck_writing": [
        ("tau2-fact-recheck-01", "6a2d33e388f422987fdebfc461271f70e36a26936d7568d4593d86649115b9c0")],
    "payment_recheck_delivery": [
        ("tau2-fact-recheck-delivery-01", "e84691add13b89fa26e10b833d5530fe51804b5eeafcd0a32855c5a892d166cd")],
    "new_customer_forms": [
        ("tau2-new-customer-forms-continuation-01", "bb3469992ca8da7f098e347b06d039266c3380b17ddbc89e4e6e756ee9f7f6ab")],
    "payment_allocation": [
        ("tau2-airline-allocation-forms-01", "88a104a2567016f93e31c413737e74d976ebce82b97a1b080e0efd0d7cb277de")],
}

# Protocol inventory at the time of review. A continuation or price-reservation
# amendment is listed once and does not add an independent experiment.
PROTOCOL_GROUPS = {
    "connection_baseline_writing_or_fixed_delivery": (
        "autonomous-memory-v1", "autonomous-memory-v1.1", "baseline-screening-v1",
        "delivery-development-v1", "memory-writing-10-v1"),
    "airline_forms_and_delivery": (
        "two-pass-memory-forms-v1", "two-pass-memory-forms-supplement-v1", "two-pass-memory-forms-followon-v1",
        "fact-recheck-v1", "fact-recheck-delivery-v1", "new-customer-forms-v1", "new-customer-forms-continuation-v1"),
    "airline_payment_variants": ("airline-allocation-forms-v1",),
    "airline_frozen_banks_and_workflows": ("fixed-bank-repeats-v1", "multi-workflow-v1", "scope-lessons-v1"),
    "decision_replays": ("decision-replay-120-v1", "written-replay-48-v1", "format-content-decisions-v1",
                         "return-date-calibration-v1", "multi-workflow-profile-step-v1", "same-customer-decisions-v1",
                         "retail-order-choice-replay-v1"),
    "cross_task_or_cross_customer_transfer": (
        "bank-transfer-v1", "same-customer-transfer-v1", "retail-related-transfer-v1",
        "retail-transfer-revision-v1", "retail-transfer-revision-pending-v1", "retail-transfer-revision-pending-maxima-v1"),
    "retail_repeat_and_bank_access": (
        "retail-recheck-two-rounds-v1", "retail-full-forms-v1", "retail-sample-expansion-v1",
        "retail-sample-supplement-v1", "retail-sample-recovery-v1", "retail-candidate-pools-v1"),
    "retail_return_visit": ("retail-two-visit-v1", "retail-two-visit-recovery-v1", "retail-conditional-retrieval-v1"),
}


def checked(path, sha):
    data = path.read_bytes()
    if digest(data) != sha:
        raise ContractError(f"Sealed evidence differs at {path}")
    return data


def ledger(folder, seal):
    closed = read_json(checked(folder / "completion.json", seal))
    control = read_json(checked(folder / "control.json", closed["control_sha256"]))
    rows = read_json(checked(folder / "allocations.json", closed["allocation_sha256"]))
    return control, rows


def sessions(folder, sha, run_ids):
    """Read first prepared wires, native seeds, and memory exposure destinations by run."""
    data = checked(folder / "log.jsonl", sha)
    blobs = BlobStore(folder)
    info = {run: dict(wires={}, customer_memory_spans=0) for run in run_ids}
    role_by_request = {}
    for line in data.splitlines():
        record = read_json(line)
        for e in record["events"]:
            run = e["run_id"]
            if run not in info:
                continue
            p, s = e["payload"], info[run]
            if e["type"] == "run.started":
                if "session" in s:
                    raise ContractError("Run started twice")
                s.update(session=e["session_id"], fresh=p["fresh_session"], bank=p["bank_id"], arm=e["condition_id"])
            elif e["type"] == "model.request_started":
                role = p["actor_role"]
                role_by_request[run, p["request_id"]] = role
                if role not in s["wires"]:
                    wire = blobs.get(p["request_ref"])
                    s["wires"][role] = wire
            elif e["type"] == "memory.input_included":
                s["customer_memory_spans"] += role_by_request[run, p["request_id"]] == "user_simulator"
            elif e["type"] == "business.transition" and p.get("origin") == "baseline.run_control":
                control = blobs.get(p["control_ref"])
                s.update(seed=control["native_resolved"]["seed"], task=control["config"]["benchmark"]["task_id"])
    last = blobs.get(record["state_ref"])
    for run, s in info.items():
        if "seed" not in s:
            cfg = last["assignments"][run]
            s.update(seed=blobs.get(cfg["native_config_ref"])["seed"], task=cfg["task_id"])
        if not s["fresh"] or "worker" in s["wires"] and s["wires"]["worker"].get("seed") != s["seed"]:
            raise ContractError("Session or recorded seed disagrees with the native job")
    return info


def sources(workspace, design, archives):
    src = design["source"]
    cohort = {c["task_id"]: c for c in src["cohort"]}
    if "root" in src:
        key = (src["root"], src["log_sha256"])
        # The first design names the entire training cohort. Smaller studies
        # can reuse that immutable log while each selects its own run IDs.
        selected = {c["run_id"] for c in cohort.values()}
        missing = selected - archives.get(key, {}).keys()
        if missing:
            archives.setdefault(key, {}).update(sessions(safe_path(workspace, key[0]), key[1], missing))
        result = {t: archives[key][c["run_id"]] for t, c in cohort.items()}
    else:
        pin = src["parent_completion"]
        folder = safe_path(workspace / "runs", pin["name"])
        _, allocation = ledger(folder, pin["sha256"])
        by_run = {r["run_id"]: r for r in allocation}
        result = {}
        for t, c in cohort.items():
            r = by_run[c["run_id"]]
            if (r["task_id"], r["arm"], r["result"]["log_sha256"]) != (t, "no_memory", c["source_log_sha256"]):
                raise ContractError("New-customer source differs from the sealed cohort")
            result[t] = sessions(safe_path(folder, r["directory"]), c["source_log_sha256"], [r["run_id"]])[r["run_id"]]
    if any(s["task"] != t or s["arm"] != "no_memory" or s["bank"] is not None
           or set(s["wires"]) != {"worker", "user_simulator"} for t, s in result.items()):
        raise ContractError("Expected a no-memory first pass with both roles observed")
    return result


def business_settings(wire):
    profile = {k: v for k, v in wire.items() if k not in {"messages", "seed", "tools"}}
    profile["tools"] = [t for t in wire.get("tools", []) if t["function"]["name"] not in {"memory_list", "memory_get"}]
    profile["business_policy"] = wire["messages"][0]["content"].partition("\n<memory_policy>\n")[0]
    return profile


def compare(jobs, originals):
    counts = Counter()
    seeds, arms, variants = Counter(), Counter(), Counter()
    openings = defaultdict(list)
    tasks = set()
    for row, current in jobs:
        t = row["task_id"]
        old = originals[t]
        tasks.add(t)
        variant = row.get("task_variant", "original_task")
        variants[variant] += 1
        arms[row["arm"]] += 1
        seeds[f"{old['seed']}->{current['seed']}"] += 1
        counts["attempted"] += 1
        counts["officially_graded"] += row["result"]["grading_status"] == "graded"
        counts["different_native_seed"] += old["seed"] != current["seed"]
        counts["customer_memory_spans"] += current["customer_memory_spans"]
        if "worker" in current["wires"]:
            w, ref = current["wires"]["worker"], old["wires"]["worker"]
            counts["worker_first_wires"] += 1
            counts["different_worker_wire_seed"] += w["seed"] != ref["seed"]
            counts["different_worker_business_settings_excluding_seed"] += business_settings(w) != business_settings(ref)
            notice = w["messages"][0]["content"].partition("\n<memory_policy>\n")[2]
            counts["worker_wires_with_extra_id_coaching"] += "Obtain the user ID from the current customer" in notice
        if "user_simulator" in current["wires"]:
            actual = digest(canonical(current["wires"]["user_simulator"]))
            expected = digest(canonical(old["wires"]["user_simulator"]))
            counts["customer_first_wires"] += 1
            counts["different_customer_opening_from_source"] += actual != expected
            openings[t, row["repetition"], variant].append(actual)
    return dict(counts=dict(counts), source_tasks=len(tasks), native_seed_pairs=dict(seeds),
                attempted_by_arm=dict(arms), attempted_by_variant=dict(variants), target_blocks_with_customer_wire=len(openings),
                target_blocks_with_multiple_customer_wires=sum(len(values) > 1 for values in openings.values()),
                target_blocks_with_divergent_customer_wire=sum(len(set(values)) > 1 for values in openings.values()))


def run(workspace):
    archives, families, seals = {}, {}, {}
    seen_sessions = set()
    for family, runs in FAMILIES.items():
        jobs = []
        cells, planned = set(), set()
        for i, (name, seal) in enumerate(runs):
            folder = workspace / "runs" / name
            control, rows = ledger(folder, seal)
            seals[name] = seal
            if i == 0:
                originals = sources(workspace, control["design"], archives)
            for row in rows:
                if row["phase"] != "solve":
                    continue
                cell = (row["task_id"], row["repetition"], row["arm"], row.get("task_variant"))
                planned.add(cell)
                if row["status"] == "not_started":
                    if "result" in row:
                        raise ContractError("Unstarted cell has a result")
                    continue
                run_id = row["run_id"]
                s = sessions(safe_path(folder, row["directory"]), row["result"]["log_sha256"], [run_id])[run_id]
                if ((s["task"], s["arm"], s["session"], run_id) !=
                        (row["task_id"], row["arm"], row["result"]["session_id"], row["result"]["run_id"])):
                    raise ContractError("Target journal does not belong to the selected cell")
                if s["session"] in seen_sessions or cell in cells or s["session"] == originals[row["task_id"]]["session"]:
                    raise ContractError("Repeated target cell or session identity")
                cells.add(cell)
                seen_sessions.add(s["session"])
                jobs.append((row, s))
        families[family] = compare(jobs, originals)
        families[family].update(planned_target_cells=len(planned), unstarted_target_cells=len(planned - cells))
    protocols = {}
    for group, names in PROTOCOL_GROUPS.items():
        protocols[group] = {}
        for name in names:
            rel = f"protocols/pilots/tau2-{name}.json"
            protocols[group][rel] = digest((ROOT / rel).read_bytes())
    return dict(scope="Archived airline source and target native seeds, first prepared role requests, "
                      "customer memory exposure events and target session IDs. No provider calls or independent DB replay.",
                reviewed_protocol_sha256=protocols, completion_sha256=seals, families=families,
                distinct_target_sessions=len(seen_sessions))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(run(args.workspace.resolve()), ensure_ascii=False, indent=2, sort_keys=True))
