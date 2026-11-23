"""Register and run the four optional-retrieval arms at frozen retail decision states."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.assets import asset_text
from scripts.analyze_full_retail import require
from scripts.audit_retail_order_choice import publish
from scripts.retail_two_visit_episode import build_episode, first_wire, policy_for
from scripts.run_retail_two_visit import (
    STAGES, key, live, prepare as prepare_visits, read, read_receipt, sealed_bank,
)

DRAFT = "protocols/drafts/tau2-retail-conditional-retrieval-v1.json"
READY = "protocols/pilots/tau2-retail-conditional-retrieval-v1.json"
PREFLIGHT = "artifacts/analyses/20261123-retail-conditional-retrieval-preflight.json"


def frozen_banks(root, design, sources):
    writers = {}
    for original in design["banks"]["writer_roots"]:
        folder = root / original["path"]
        registration = asset_text(folder, {"path": "registration.json", "sha256": original["registration_sha256"]})
        for job in read_json(registration.encode())["jobs"]["writer"]:
            path = folder / job["run"]
            receipt = read_receipt(path, "writer")
            if not receipt or receipt["status"] not in {"completed", "empty"}:
                continue
            slot = (job["task"], job["arm"])
            require(slot not in writers and (path / "log.jsonl").stat().st_size == receipt["journal_bytes"],
                    "Ambiguous or changed writer journal")
            writers[slot] = (sealed_bank(path, receipt, job["run"]), job["run"])
    forms = design["banks"]["form_order"]
    require(set(writers) == {(s, f) for s in sources for f in forms}, "Incomplete frozen writer cohort")
    banks = {}
    for source in sources:
        parts = [writers[source, form] for form in forms]
        banks[source] = ([item for items, _ in parts for item in items], [run for _, run in parts])
    hashes = {s: digest(canonical(items)) for s, (items, _) in banks.items()}
    require(digest(canonical(sorted(hashes.items()))) == design["banks"]["source_bank_pairs_sha256"],
            "Sealed source banks differ from design")
    return banks, hashes


def prepare(root, profile):
    design = read(root / DRAFT)
    require(dict(profile, status="draft", authorization=None) == design, "Changed conditional-retrieval design")
    inputs = {k: read_json(asset_text(root, ref).encode()) for k, ref in design["inputs"].items()}
    instructions = {k: asset_text(root, ref) for k, ref in design["instructions"].items()}
    base = inputs["base_design"]
    prior = prepare_visits(root, base)
    require(prior["record"] == inputs["previous_preflight"], "Prior cohort could not be reproduced")
    cases = [(u["customer"], c) for u in inputs["cohort"]["reserved"] for c in u["cases"]]
    sources = {c["source"] for _, c in cases}
    banks, bank_hashes = frozen_banks(root, design, sources)
    archive = {(r["task"], r["arm"]): r for r in inputs["previous_results"]["decisions"]}
    checkpoint_customers = set(prior["record"]["continuation_sample"])
    alloc = design["allocation"]
    salt = alloc["salt"]
    jobs = {s: [] for s in STAGES}
    ordered = []
    # All cells share the native worker policy and tool schema. Each current wire
    # must still match its own independent archived real and empty submissions.
    sample = cases[0][1]
    cfg, orch, _ = build_episode(base, prior["connection"], prior["tasks"][sample["task"]],
                                 prefix=prior["public"][sample["public_prefix_sha256"]])
    dec = design["decision"]
    require(cfg["worker"]["model_id"] == dec["worker"] and cfg["worker"]["parameters"] == {
        "seed": dec["seed"], "reasoning": {"effort": dec["reasoning_effort"]},
        "max_completion_tokens": dec["max_completion_tokens"],
    } and base["first_worker_requests"] == dec["worker_requests_per_session"], "Changed worker parameters")
    first_hashes = {}
    empty_hash = digest(canonical([]))
    for customer, case in sorted(cases, key=lambda pair: key(salt, "case", pair[1]["task"])):
        task, src, prefix = case["task"], case["source"], case["public_prefix_sha256"]
        for prompt, instruction in instructions.items():
            if (prefix, prompt) not in first_hashes:
                wire = first_wire(cfg, orch, prior["public"][prefix], policy_for("query"), instruction, [])
                first_hashes[prefix, prompt] = digest(canonical(wire))
        real, empty = archive[task, "query"], archive[task, "empty_query"]
        require(real["initial_sha256"] == empty["initial_sha256"] == first_hashes[prefix, "current"]
                and real["items_sha256"] == bank_hashes[src] and empty["items_sha256"] == empty_hash,
                "Native first input or bank differs from the archived pair")
        for arm in sorted(design["arms"], key=lambda a: key(salt, "arm", task, a)):
            interface = design["arms"][arm]
            has_bank = interface["native_arm"] == "query"
            keep = customer in checkpoint_customers
            run = "r_" + key(salt, "decision", task, arm)[:32]
            ordered.append([customer, task, src, case["mode"], prefix, arm, run, keep])
            jobs["decision"].append(dict(
                stage="decision", customer=customer, task=task, source=src, mode=case["mode"], prefix=prefix,
                target=case["target"], history=case["history"], position=real["position"], arm=arm, run=run,
                in_sample=keep, **interface, initial_sha256=first_hashes[prefix, interface["instruction"]],
                items_sha256=bank_hashes[src] if has_bank else empty_hash,
                writer_runs=banks[src][1] if has_bank else [], score_private_limit=True,
            ))
    require(len(ordered) == len({row[6] for row in ordered}) == alloc["decision_sessions"]
            and digest(canonical(ordered)) == alloc["schedule_sha256"]
            and sum(row[7] for row in ordered) == design["continuation"]["checkpoint_sessions"],
            "Allocation or checkpoint population differs from design")
    record = dict(
        study=design["name"], design_sha256=digest(canonical(design)), schedule_sha256=alloc["schedule_sha256"],
        registered_jobs_sha256=digest(canonical(jobs)),
        source_bank_pairs_sha256=design["banks"]["source_bank_pairs_sha256"],
        native_tasks_sha256=prior["record"]["native_tasks_sha256"],
        public_prefixes_sha256=prior["record"]["public_prefixes_sha256"],
        cases=len(cases), sources=len(banks), decisions=len(ordered), checkpoint_candidates=sum(r[7] for r in ordered),
        original_wires_reconstructed=len(cases)*2, model_requests_sent=0,
    )
    return dict(prior, protocol=profile, episode_protocol=base, record=record, jobs=jobs,
                source_banks=banks, instructions=instructions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "live"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.mode == "live":
        require((ROOT / READY).is_file(), "A funded decision-only profile is required before dispatch")
    profile = read(ROOT / (READY if args.mode == "live" else DRAFT))
    if args.mode == "live":
        auth = profile.get("authorization") or {}
        require(profile["status"] == "ready" and auth.get("through") == "decision"
                and auth.get("continuation") == "sample"
                and auth.get("max_requests") == profile["decision"]["max_worker_requests_for_decisions"],
                "Only the funded first decisions with the registered checkpoints may run")
    study = prepare(ROOT, profile)
    result = study["record"]
    if args.mode == "live":
        result = live(ROOT, study, design_path=DRAFT, preflight_path=PREFLIGHT)
    if args.output:
        publish(args.output, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
