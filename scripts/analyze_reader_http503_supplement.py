"""Join separately sealed airline retries to the released 48 slots and their immutable first pass.

--packets-only exports only masked, newly sealed attempts, including before the
controller has written its final result. --reviews and --note-matches take the
same JSON inputs as analyze_cohort_reader_evidence.py. Supply new annotations
alone or the complete original set plus new annotations. The full export writes all
48 CSV slots and publishes only changed behavior rows and new annotations. Its
parent summary pins the inherited evidence. Write each snapshot to a new directory.
"""

import argparse
from collections import Counter
import csv
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path

from agent_memory_eval.adapters.banking_memory_cohort import observation
from agent_memory_eval.analysis.recorded_decisions import require, sealed_bytes
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.recording.assets import asset_bytes, asset_text, publish_json, publish_text
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.study.adapters.tau_banking import outcome_fields
from agent_memory_eval.study.adapters.tau_transfer import dialogue_counts
from agent_memory_eval.study.reader_supplements import reader_supplements
from agent_memory_eval.study.records import read, sealed_rows
from agent_memory_eval.study.transfer_baselines import observed_effort, outcome
from scripts.analyze_cohort_reader_evidence import apply_review, observed_attempt
from scripts.recover_study_readers import POLICY


RESULTS = ("reader-outcomes.csv", "behavior-evidence.json", "behavior-reviews.json", "note-matches.json")


def arm(row):
    return "__".join(row[k] for k in ("form", "reading", "bank"))


def original(workspace):
    policy = read(POLICY)
    summary = read_json(asset_bytes(ROOT, policy["parent_results"]))
    root = (ROOT / policy["parent_results"]["path"]).parent
    documents = [sealed_bytes(root / name, summary["files"][name]) for name in RESULTS]
    csv_rows = list(csv.DictReader(StringIO(documents[0].decode())))
    evidence, reviews, matches = map(read_json, documents[1:])
    airline = {arm(r): r for r in csv_rows if r["benchmark"] == policy["benchmark"]}
    by_arm = {e["condition"]: e for e in evidence if e["benchmark"] == policy["benchmark"]}
    missing = set(policy["conditions"])
    require(len(csv_rows) == len(evidence) == summary["assigned"] == 48 and set(airline) == set(by_arm)
            and len(missing) == 6 and missing == {name for name, r in airline.items() if not r["success"]}
            and all(len(by_arm[name]["attempts"]) == 3 and all(a["missing_reason"] == "HTTP_503" and
                    a["official_success"] is None for a in by_arm[name]["attempts"]) for name in missing),
            "Supplement is not limited to the original six missing slots")
    for e in evidence:
        for a in e["attempts"]:
            require(reviews[a["packet"]]["packet_sha256"] == a["packet_sha256"], "Released review differs from its attempt")
    batch = read_json(asset_bytes(ROOT, policy["batch"]))
    rules = read_json(asset_bytes(ROOT, batch["action_review"]))
    for b, stats in summary["benchmarks"].items():
        folder = workspace / "runs" / ("own-mixed-" + b + "-01") / "reader-batches" / batch["id"]
        sealed_bytes(folder / "registration.json", stats["batch_registration_sha256"])
        totals = read_json(sealed_bytes(folder / "result.json", stats["result_sha256"]))
        require(totals["requests"] == stats["requests"] and all(Decimal(totals[k]) == Decimal(stats[k])
                for k in ("reported_usd", "held_usd")), "Original batch cost differs from the released receipt")
    require(policy["result_sha256"] == summary["benchmarks"]["airline"]["result_sha256"]
            and policy["batch"]["sha256"] == summary["batch_sha256"],
            "Different original reader batch or plan")
    return policy, summary, csv_rows, evidence, reviews, matches, airline, by_arm, rules


def native_inputs(base, plan, record, case, batch):
    """Read the three original writer shards. Their ordered bank digest is verified in each native observation."""
    original = read(base / "reader-batches" / batch / "registration.json")
    stages = read_json(sealed_bytes(base / "through-write.json", original["writer_checkpoint_sha256"]))["stages"]
    catalog = {}
    for source in record["cases"]:
        task = source["task_id"]
        if "donor" in source or task not in case["pool_sources"]:
            continue
        stage = "task-" + source["task"] + "/write"
        shard = safe_path(base, stage)
        _, _, writers = sealed_rows(shard, stages[stage]["seal"])
        for row in writers:
            writer = safe_path(shard, row["directory"])
            bank = read_form_bank(writer, expected_log_sha256=row["result"]["log_sha256"])
            require((bank["task_id"], bank["arm"], bank["run_id"]) == (task, row["arm"], row["run_id"]),
                    "Writer bank differs from its native source and form")
            catalog[task, row["arm"]] = dict(bank, journal_sha256=row["result"]["log_sha256"])
    _, control, _ = sealed_rows(base / ("task-" + case["task"]) / "solve")
    return dict(case=case, registration=digest(canonical(record)), catalog=catalog, design=control["design"],
                instruction=asset_text(ROOT, plan["reading"]["preload"]["notice"]), native_revision=record["native_revision"])


def native_summary(obs):
    """Preserve the published analyst-facing grade and entity fields for the six updated rows."""
    grade, state = obs["official_components"], obs["state_diagnostics"]
    if grade:
        components = {k: grade[k] for k in ("reward_basis", "reward_breakdown", "db_check") if k in grade}
        for key in ("action_checks", "communicate_checks", "nl_assertions"):
            checks = grade.get(key)
            components[key] = None if checks is None else []
            for i, check in enumerate(checks or [], 1):
                details = dict(index=i)
                if isinstance(check, dict):
                    details.update({k: v for k, v in check.items() if type(v) in (bool, int, float)})
                    action = check.get("action")
                    if isinstance(action, dict):
                        details.update({k: action[k] for k in ("action_id", "name", "requestor") if k in action})
                components[key].append(details)
    else:
        components = None
    final = {k: state.get(k) for k in ("status", "reason", "replayed_reward_sha256", "official_db_match",
                                       "observed_db_sha256", "reference_db_sha256")}
    for group in ("target", "additional"):
        final[group + "_differences"] = [
            {k: e[k] for k in ("id", "table", "kind", "observed_present", "reference_present", "different_fields", "final_status") if k in e}
            for e in state.get(group + "_entities", []) if e.get("different_fields") or e.get("observed_present") != e.get("reference_present")
        ]
    return dict(official_components=components, final_state=final)


def measure(workspace, shard, row, context, base, assigned, target, n, *, packets_only=False):
    """Use the same native observation, first-worker effort and masked action packet as the original."""
    condition = row["arm"]
    case = context["case"]
    delivery = context["design"]["delivery_arms"][condition]
    pool = [context["catalog"][s, base["form"]] for s in delivery["source_tasks"]]
    own, = [b for b in pool if b["task_id"] == base["task"]]
    notes = []
    for bank in pool:
        item, = bank["items"]
        notes.append(dict(source_task=bank["task_id"], source_run=bank["source_run_id"], writer_run=bank["run_id"],
                          snapshot=bank["snapshot_id"], item_id=item["id"], version_id=item["version"],
                          source_log_sha256=bank["source_log_sha256"], writer_log_sha256=bank["journal_sha256"],
                          title=item["title"], body=item["body"]))
    require([(n["writer_run"], n["item_id"], n["version_id"]) for n in notes] ==
            [(n["writer_run"], n["item_id"], n["version_id"]) for n in assigned], "Released writer versions changed")
    action, packet = observed_attempt("airline", base["task"], condition, target, notes,
                                      dict(attempt=n, run=row["run_id"], folder=safe_path(shard, row["directory"])), workspace)
    if packets_only:
        return action, packet, notes, None, None
    transfer = dict(rule=case["decision"], bank=base["bank"], domain=case["domain"], workspace=workspace,
                    native_sha256=case["native_sha256"], cohort=True, pool=pool, delivery=delivery,
                    registration=context["registration"], instruction=context["instruction"],
                    descriptions=delivery.get("tool_descriptions"))
    obs = observation(shard, row, writer=own, transfer=transfer)
    _, success, missing, _ = outcome_fields(obs)
    require(action["official_success"] is success and own["source_run_id"] == base["source_run"]
            and (not obs["opening_sha256"] or (obs["opening_sha256"] == base["opening_sha256"] and
                 obs["opening_source_run"] == base["source_run"])) and
            (not obs["first_worker_sha256"] or obs["first_worker_sha256"] == base["first_worker_sha256"]),
            "Native grade, worker wire, opening or immutable first pass changed")
    effort = observed_effort(dict(effort_from_first_worker=obs["effort_from_first_worker"], dialogue=dialogue_counts(obs),
                                  business_calls=obs["effort"].get("business_calls"), evidence=obs, reads=obs["reads"]))
    update = {k: effort.get(k) for k in base if ("source_" + k) in base and k not in {"success", "run"}}
    exposure = action["exposure"]
    source = {"True": "success", "False": "failure"}[base["source_success"]]
    update.update(success=success, transition="ungraded" if success is None else source + ("_success" if success else "_failure"),
                  termination=action["reason"], missing_reason=missing, registered_decision=outcome(obs, "registered_decision"),
                  registered_call_executed=outcome(obs, "registered_call_executed", case["decision"]),
                  reader_run=row["run_id"], packet=action["packet"], opening_sha256=obs["opening_sha256"],
                  first_worker_sha256=obs["first_worker_sha256"], confirmed_titles=sum("title" in x["input_fields"] for x in exposure),
                  confirmed_bodies=sum("body" in x["input_fields"] for x in exposure),
                  memory_list=obs["reads"].get("memory_list", 0), memory_get=obs["reads"].get("memory_get", 0))
    native = dict(**native_summary(obs),
                  native_revision=context["native_revision"], native_control_sha256=case["native_sha256"],
                  native_task_sha256=case["task_sha256"], source_run=own["source_run_id"], source_log_sha256=own["source_log_sha256"],
                  writer_lineage=notes, exposure=exposure)
    return action, packet, notes, update, native


def publish(out, name, data):
    path = out / name
    if isinstance(data, str):
        if path.exists():
            require(path.read_bytes() == data.encode(), f"Use a new output directory for changed {name}")
        else:
            publish_text(path, data)
    elif path.exists():
        require(canonical(read(path)) == canonical(data), f"Use a new output directory for changed {name}")
    else:
        publish_json(path, data)


def totals(records):
    return dict(requests=sum(r["requests"] for r in records),
                reported_usd=str(sum((Decimal(r["reported_usd"]) for r in records), Decimal(0))),
                held_usd=str(sum((Decimal(r["held_usd"]) for r in records), Decimal(0))))


def refresh_counts(summary, rows):
    counts = Counter(row["transition"] for row in rows)
    summary["transitions"] = {k: counts[k] for k in summary["transitions"]}
    summary["graded"] = len(rows) - counts["ungraded"]
    return sum(counts[k] for k in ("success_success", "failure_success"))


def new_coding(provided, released):
    """Merged inputs must preserve every original entry. Publish new observations once."""
    if provided.keys() & released.keys():
        require(released.keys() <= provided.keys() and all(canonical(provided[k]) == canonical(v) for k, v in released.items()),
                "The merged annotation file changes or omits originally released entries")
    return {k: provided[k] for k in sorted(provided.keys() - released.keys())}


def export(workspace, out, ratings, new_matches, *, packets_only=False):
    policy, summary, rows, evidence, old_ratings, old_matches, airline, by_arm, rules = original(workspace)
    ratings, new_matches = new_coding(ratings, old_ratings), new_coding(new_matches, old_matches)
    plan = read_json(asset_bytes(ROOT, policy["plan"]))
    base = workspace / "runs" / plan["id"]
    record = read(base / "registration.json")["preflight"]
    target = rules["targets"]["airline"]
    require(not out.is_relative_to(workspace / "runs") and not out.is_relative_to((ROOT / policy["parent_results"]["path"]).parent),
            "Choose an output outside the original runs and release")
    supplements = [s for s in reader_supplements(workspace, ROOT, plan, record)
                   if s.batch == summary["batch"] and s.directory.name == policy["id"]]
    out.mkdir(parents=True, exist_ok=True)
    if packets_only and not supplements:
        publish(out, "blinded-packets.json", {})
        return dict(new_packets=0)
    require(len(supplements) == 1, "The registered supplement is unavailable")
    supplement, = supplements
    scope, archive = supplement.registration, supplement.completion
    require(scope["policy"] == dict(path=str(POLICY.relative_to(ROOT)), sha256=digest(POLICY.read_bytes()))
            and (archive is not None or packets_only), "A full export needs the registered, sealed supplement result")
    for name, history in scope["original_readers"].items():
        require(history[-1]["run"] == airline[name]["reader_run"] and history[-1]["first_worker"] == airline[name]["first_worker_sha256"]
                and [a["run"] for a in by_arm[name]["attempts"]] == [h["run"] for h in history],
                "Original native attempts differ from the published slot")
    case, = [c for c in record["cases"] if c["task"] == supplement.case]
    context = native_inputs(base, plan, record, case, supplement.batch) if supplement.attempts else {}
    packets, updates = {}, {}
    for attempt in supplement.attempts:
        result, shard, job = attempt.receipt, attempt.shard, attempt.row
        name, run = result["condition"], result["run_id"]
        path = shard.parent.parent / "receipt.json"
        b = by_arm[name]
        rec = dict(result, receipt=dict(path=str(path.relative_to(workspace)), sha256=digest(path.read_bytes())),
                   implementation=scope["implementation"]["implementation_commit"])
        actual = job.get("result") or {}
        if not actual.get("log_sha256"):
            require(result["success"] is None and result["missing_reason"], "Unsealed native outcome")
            if packets_only:
                continue
            update = {k: None for k in airline[name] if ("source_" + k) in airline[name]}
            update.update(reader_run=run, success=None, transition="ungraded", missing_reason=result["missing_reason"],
                          packet=None, termination=result["missing_reason"], first_worker_sha256=None, opening_sha256=None,
                          registered_decision=None, registered_call_executed=None, confirmed_titles=0, confirmed_bodies=0,
                          memory_list=0, memory_get=0)
            native = dict(exposure=[], official_components=None, final_state=None, journal=None,
                          findings={axis: [dict(fate="unobserved")] for axis in target["axes"]})
            rec.update(run=run, packet=None, official_success=None, reason=result["missing_reason"])
        else:
            action, packet, notes, update, native = measure(
                workspace, shard, job, context, airline[name], b["assigned"], target, attempt.ordinal, packets_only=packets_only)
            pid = action["packet"]
            require(pid not in packets and pid not in old_ratings and action["official_success"] is result["success"],
                    "Duplicate packet or native grade disagreement")
            packets[pid] = packet
            if packets_only:
                continue
            require(update["missing_reason"] == result["missing_reason"], "Native missing reason differs from its receipt")
            findings = apply_review(target, rules, packet, action, ratings.get(pid), new_matches.get(pid, []), notes)
            native.update(findings=findings, journal=action["journal"])
            rec.update(action, findings=findings)
        rec["effort_from_first_worker"] = {k: v for k, v in update.items() if "source_" + k in airline[name] and k != "success"}
        if name not in updates:
            old, = [a for a in b["attempts"] if a["run"] == b["reader_run"]]
            old["prior_attempt_findings"] = b["findings"]
        b["attempts"].append(rec)
        # The verified ledger only extends an unresolved 503. A graded False is terminal.
        updates[name] = update, native
    require(ratings.keys() <= packets.keys() and new_matches.keys() <= ratings.keys(), "Ratings reference unknown supplement packets")
    if packets_only:
        publish(out, "blinded-packets.json", dict(sorted(packets.items())))
        return dict(new_packets=len(packets))
    cost = {k: archive[k] for k in ("requests", "reported_usd", "held_usd")}
    for name, (update, native) in updates.items():
        old = airline[name]
        require(not old["success"], "Only ungraded airline slots may change")
        old.update({k: ("" if v is None else str(v)) for k, v in update.items()}, attempted=str(len(by_arm[name]["attempts"])))
        by_arm[name].update(packet=old["packet"] or None, reader_run=old["reader_run"], **native)
    bench = summary["benchmarks"]["airline"]
    old_cost = {k: summary[k] for k in cost}
    bench.update(totals([bench, cost]), reader_attempts=sum(int(row["attempted"]) for row in airline.values()))
    bench["succeeded"] = refresh_counts(bench, list(airline.values()))
    bench["success_bounds_over_assigned"] = [bench["succeeded"], bench["succeeded"] + bench["assigned"] - bench["graded"]]
    bench["missing_conditions"] = [dict(condition=name, reason=r["missing_reason"], attempts=int(r["attempted"]))
                                   for name, r in airline.items() if not r["success"]]
    bench["original_result_sha256"] = bench.pop("result_sha256")
    bench["supplement_result_sha256"] = digest((supplement.directory / "result.json").read_bytes())
    summary.update(totals([old_cost, cost]), format="agent-memory-reader-supplement-results/v1",
                   parent_results=policy["parent_results"], original_implementation=summary.pop("implementation"),
                   supplement=dict(policy=scope["policy"], registration_sha256=archive["registration_sha256"],
                                   result_sha256=bench["supplement_result_sha256"], implementation=scope["implementation"],
                                   native_revision=record["native_revision"],
                                   initial_reader_cost=old_cost, new_reader_cost=cost, complete=archive["complete"],
                                   changed_behavior_rows=len(updates), inherited_behavior_rows=len(rows) - len(updates),
                                   inherited_reviews=len(old_ratings), new_attempts=len(supplement.attempts),
                                   new_packets=len(packets), new_reviewed=sum(set(r["axes"]) == set(target["axes"]) for r in ratings.values())))
    require(Decimal(summary["reported_usd"]) + Decimal(summary["held_usd"]) <= Decimal(policy["cumulative_batch_max_usd"])
            and Decimal(bench["reported_usd"]) + Decimal(bench["held_usd"]) <= Decimal(policy["cumulative_benchmark_max_usd"]),
            "Combined reader costs exceed the registered ceilings")
    refresh_counts(summary, rows)
    summary["reader_attempts"] = sum(int(r["attempted"]) for r in rows)
    summary["reviewed_attempts"] = len(old_ratings) + summary["supplement"]["new_reviewed"]
    for mode, data in summary["delivery"].items():
        group = [r for r in rows if r["reading"] == mode]
        data.update(slots=len(group), title_in_input=sum(int(r["confirmed_titles"]) > 0 for r in group),
                    body_in_input=sum(int(r["confirmed_bodies"]) > 0 for r in group),
                    bodies_in_input=sum(int(r["confirmed_bodies"]) for r in group),
                    list_calls=sum(int(r["memory_list"]) for r in group), get_calls=sum(int(r["memory_get"]) for r in group))
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    changed = [r for r in evidence if r["benchmark"] == policy["benchmark"] and r["condition"] in updates]
    for name, content in zip(RESULTS, (stream.getvalue(), changed, ratings, new_matches)):
        publish(out, name, content)
    publish(out, "blinded-packets.json", dict(sorted(packets.items())))
    summary["files"] = {n: digest((out / n).read_bytes()) for n in (*RESULTS, "blinded-packets.json")}
    publish(out, "summary.json", summary)
    return {k: summary[k] for k in ("assigned", "graded", "reader_attempts", "reviewed_attempts", "requests", "reported_usd", "held_usd")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--packets-only", action="store_true")
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--note-matches", type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.workspace.resolve(), args.out.resolve(), read(args.reviews) if args.reviews else {},
                            read(args.note_matches) if args.note_matches else {}, packets_only=args.packets_only)))
