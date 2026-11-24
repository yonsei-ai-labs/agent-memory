"""Reproduce the fixed-choice, free-dialogue and static-function controls without model calls."""

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from agent_memory_eval.adapters.bfcl_task_start import open_simple_python, simple_python_tasks
from agent_memory_eval.analysis.bfcl_decisions import decoded_choice
from agent_memory_eval.analysis.claim_evidence import paired_counts
from agent_memory_eval.analysis.decision_attribution import blinded_comparison, compare_episodes, compare_steps, fingerprint
from agent_memory_eval.analysis.efficiency import paired_effort
from agent_memory_eval.analysis.identifier_evidence import trace_identifier, writer_lineage
from agent_memory_eval.analysis.recorded_decisions import require, sealed_bytes
from agent_memory_eval.analysis.tau2_attribution import fixed_choice, graded_episode, listed_orders
from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.assets import publish_json
from agent_memory_eval.recording.journal import safe_path


def fixed(root, details):
    folder = root / "runs/tau2-retail-conditional-retrieval-01"
    original = read_json((root / "artifacts/analyses/20261123-retail-conditional-retrieval-results.json").read_bytes())
    reg = read_json(sealed_bytes(folder / "registration.json", original["registration_sha256"]))
    jobs = reg["jobs"]["decision"]
    require(fingerprint(reg["jobs"]) == reg["preflight"]["registered_jobs_sha256"], "Unregistered fixed-choice schedule")
    published = {r["run"]: r for r in original["decisions"]}
    require(len(jobs) == len(published) and {j["run"] for j in jobs} == published.keys(), "Incomplete fixed-choice ledger")
    roots = [safe_path(root, b["path"]) for b in reg["design"]["banks"]["writer_roots"]]
    for path, ref in zip(roots, reg["design"]["banks"]["writer_roots"], strict=True):
        sealed_bytes(path / "registration.json", ref["registration_sha256"])
    writer = writer_lineage(roots)
    pairs = defaultdict(dict)
    for job in jobs:
        key = tuple(job[k] for k in ("customer", "source", "task", "mode", "instruction"))
        bank = "empty" if job["native_arm"] == "empty_query" else "real"
        require(bank not in pairs[key], "Duplicate fixed-choice cell")
        pairs[key][bank] = job
    statuses, contexts, primary_contexts, changes, chain, effort = (Counter() for _ in range(6))
    primary, initial_equal = [], 0
    for key, arms in sorted(pairs.items()):
        require(arms.keys() == {"real", "empty"}, "Unpaired fixed-choice allocation")
        values = {}
        for arm, job in arms.items():
            d = fixed_choice(safe_path(folder, job["run"]), job)
            known = published[job["run"]]
            require((d["status"], d["focused"]) == (known["status"], known["focused"]), "Original endpoint mismatch")
            values[arm] = d
            statuses[d["status"]] += 1
        real, empty = values["real"], values["empty"]
        require(real["initial"] == empty["initial"], "Same-instruction banks differ at the initial decision")
        initial_equal += 1
        contrast = compare_steps(empty["step"], real["step"])
        if contrast["status"].startswith("matched_") and not all(v["at_registered_input"] for v in values.values()):
            contrast["status"] = "downstream"
        contexts[contrast["status"]] += 1
        if key[-2:] != ("previous_only", "conditional"):
            continue
        job = arms["real"]
        primary.append((real["focused"], empty["focused"]))
        primary_contexts[contrast["status"]] += 1
        changes[contrast["change"] or "unobserved"] += 1
        for arm, value in values.items():
            effort[arm] += value["requests"]
        require(re.fullmatch(r"#W[0-9]{7}", job["target"]), "Unexpected target order format")
        # The original order scorer accepts #W1234567, W1234567 and bare seven-digit tokens.
        pattern = re.compile(r"(?<![A-Za-z0-9])#?W?" + job["target"][2:] + r"(?![A-Za-z0-9])", re.I)
        evidence = trace_identifier(job, real, writer, pattern)
        present = any(evidence["sources"].values())
        shown = bool(evidence["exposed"])
        current = evidence["current"] or []
        alternatives = [orders for orders in listed_orders(real["public"]) if job["target"] in orders]
        for metric, value in dict(in_public_source=present,
                in_past_customer=any(c["role"] == "user" for hits in evidence["sources"].values() for c in hits),
                stored=bool(evidence["stored"]), exposed=shown,
                current_user=any(c["role"] == "user" for c in current),
                current_tool=any(c["role"] == "tool" for c in current),
                current_profile_with_other_orders=real["step"] and any(len(set(orders)) > 1 for orders in alternatives),
                observed_business_reply=real["step"] is not None, focused=real["focused"],
                focused_after_body=real["focused"] and any(s["field"] == "body" for s in real["visible"]),
                focused_with_chain=real["focused"] and any(s["identifier_in_own_source"] for s in evidence["exposed"])).items():
            chain[metric] += bool(value)
        if details is not None:
            details.append(dict(control="fixed_order", reference_run=arms["empty"]["run"], candidate_run=job["run"],
                                customer=job["customer"], source=job["source"], task=job["task"], target=job["target"],
                                contrast=contrast, reference_status=empty["status"], candidate_status=real["status"],
                                reference_focused=empty["focused"], **evidence))
    return dict(registered=len(jobs), status=statuses, paired_openings_equal=initial_equal, decision_inputs=contexts,
                previous_only_conditional=dict(customers=len({k[0] for k in pairs if k[-2:] == ("previous_only", "conditional")}),
                    decision_inputs=primary_contexts, outcome="focused_target_first_business_action",
                    pairs=paired_counts(primary), changes=changes, worker_requests=effort, identifier=chain))


def full(root, details, reviews=None):
    folder = root / "runs/tau2-fresh-matched-01-continuation"
    closure = read_json((folder / "completion.json").read_bytes())
    reg = read_json(sealed_bytes(folder / "registration.json", closure["registration_sha256"]))["preflight"]
    statuses, first, exposure, bank_delivery, ordered, decisions, worker_inputs = (Counter() for _ in range(7))
    early, efforts, policies = defaultdict(Counter), defaultdict(list), defaultdict(list)
    graded_sources = 0
    for case in reg["cases"]:
        task = "task-" + case["task"]
        source_folder, _, solve_folder = [folder / task / name for name in case["scheduled_stages"]]

        def allocations(path):
            close = read_json(sealed_bytes(path / "completion.json", closure["stages"][str(path.relative_to(folder))]["seal"]))
            return read_json(sealed_bytes(path / "allocations.json", close["allocation_sha256"]))

        src, = allocations(source_folder)
        rows = allocations(solve_folder)
        observed = []
        for row in rows:
            statuses[row["status"]] += 1
            success = (row.get("result") or {}).get("official_success") if row["status"] == "completed" else None
            policies[case["domain"], row["arm"]].append((success, (src.get("result") or {}).get("official_success")))
            if row["status"] == "completed":
                observed.append(row)
            elif details is not None:
                details.append(dict(control="fresh_episode", task=case["task"], arm=row["arm"], status=row["status"],
                                    source_run=src.get("run_id"), run=row.get("run_id"), group=None, first_public=None))
        if not observed:
            continue
        graded_sources += 1
        public_inputs = {} if reviews is not None else None
        reference, effort0 = graded_episode(source_folder, src, case, reg["read_instruction_sha256"], public_inputs=public_inputs)
        for row in observed:
            steps, effort = graded_episode(solve_folder, row, case, reg["read_instruction_sha256"], src)
            visible = set().union(*(s.fields for s in steps if s.actor == "worker"))
            count = row["result"]["bank_sizes"]["items"]
            mode = row["arm"].split("__")[1]
            if "body" in visible:
                group = "body"
            elif visible:
                group = "title_only"
            elif not count:
                group = "empty"
            elif mode == "on_demand" and effort["memory_read_calls"] == 0:
                group = "nonempty_unopened"
            else:
                group = "other_unexposed"
            exposure[group] += 1
            bank_delivery[group + "/" + mode] += 1
            c = compare_episodes(reference, steps, include_choices=details is not None)
            worker_inputs.update(c["worker_inputs"])
            div = c["first_public"]
            actor = div["actor"] if div else "none"
            first[actor] += 1
            b = c["first_business_response"]
            decisions[b["status"] if b else "sequence_same"] += 1
            call = c["first_ordered_call"]
            ordered[call["status"] if call else "sequence_same"] += 1
            if actor == "worker":
                i = div["index"]
                at_choice = steps[i].fields
                early_group = group
                if group in {"body", "title_only"}:
                    early_group = "body" if "body" in at_choice else "title_only" if at_choice else "not_visible_at_divergence"
                e = early[early_group]
                e["pairs"] += 1
                e["first_worker_reply"] += not any(s.actor == "worker" for s in steps[:i])
                e["both_speech"] += not reference[i].actions and not steps[i].actions
                e["immediate_calls_differ"] += reference[i].actions != steps[i].actions
                e["matched_input"] += div["status"].startswith("matched_")
                e["next_customer_observed"] += c["next_customer_changed"] is not None
                e["next_customer_changed"] += c["next_customer_changed"] is True
                e["adjacent_customer_observed"] += c["adjacent_customer_changed"] is not None
                e["adjacent_customer_changed"] += c["adjacent_customer_changed"] is True
                e["whole_call_sequence_differs"] += call is not None
                if call and call["status"].startswith("matched_"):
                    e["first_different_ordered_call_at_matched_input"] += 1
                if reviews is not None and div["status"].startswith("matched_"):
                    packet, key = blinded_comparison(public_inputs[reference[i].request], reference[i], steps[i])
                    reviews.append(packet)
                    c["review_key"] = key
            ok = row["result"]["official_success"]
            base_ok = src["result"]["official_success"]
            efforts[case["domain"], group].append((ok, base_ok, effort, effort0))
            if details is not None:
                details.append(dict(control="fresh_episode", task=case["task"], arm=row["arm"], status=row["status"], group=group,
                                    source_run=src["run_id"], run=row["run_id"], official=[base_ok, ok], **c))
    require(sum(statuses.values()) == reg["assigned"]["solve"], "Missing registered second-pass assignments")
    return dict(assigned=reg["assigned"], statuses=statuses, graded_sources=graded_sources,
                first_public_actor=first, exposure=exposure, all_worker_inputs=worker_inputs,
                outcome_by_assigned_policy={"/".join(k): paired_counts(v) for k, v in sorted(policies.items())},
                delivery=bank_delivery, first_business_response=decisions, first_ordered_call=ordered,
                worker_first=early, outcome_effort_by_domain_and_exposure={
                    "/".join(k): paired_effort(v) for k, v in sorted(efforts.items())})


def bfcl(root):
    checkout = root / ".local/benchmark-portability/bfcl-6ea5797"
    row = simple_python_tasks(checkout)["simple_python_0"]
    start = open_simple_python(row, {"worker": "offline_fixture"})
    name = row["function"][0]["name"]
    def choice(label, call):
        fixed_start = open_simple_python(row, {"worker": "offline_fixture"}, reference=start)
        return decoded_choice(fixed_start, label, [call], visible_fields=())
    base = choice("reference", {name: {"base": 10, "height": 5}})
    return dict(task=row["id"], revision=start.task.revision, kind="scripted_decoded_calls",
                public_sha256=fingerprint(start.record()["public_input"]),
                reordered=compare_steps(base, choice("reordered", {name: {"height": 5, "base": 10}})),
                argument=compare_steps(base, choice("argument", {name: {"base": 10, "height": 6}})),
                function=compare_steps(base, choice("undeclared-function", {"unlisted_function": {"base": 10, "height": 5}})))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--control", choices=["fixed", "full", "bfcl", "all"], default="all")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--details", type=Path, help="Optional request and citation evidence for the two primary cohorts")
    parser.add_argument("--blinded-review", type=Path, help="Unlabelled public worker-first pairs from full episodes")
    args = parser.parse_args()
    if args.blinded_review and (args.control not in {"all", "full"} or not args.details):
        parser.error("--blinded-review requires full episodes and --details for the separate review key")
    details = [] if args.details else None
    reviews = [] if args.blinded_review else None
    result = dict(format="decision-attribution-audit/v1")
    if args.control in {"all", "fixed"}:
        result["fixed"] = fixed(args.root, details)
    if args.control in {"all", "full"}:
        result["full"] = full(args.root, details, reviews)
    if args.control in {"all", "bfcl"}:
        result["bfcl"] = bfcl(args.root)
    if args.output:
        publish_json(args.output, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.details:
        publish_json(args.details, details)
    if args.blinded_review:
        publish_json(args.blinded_review, sorted(reviews, key=lambda p: p["pair_id"]))


if __name__ == "__main__":
    main()
