"""Reproduce evidence for three cited memory clauses and two complete sets of policy comparisons."""

import argparse
import csv
from collections import Counter, defaultdict
from decimal import Decimal
from io import StringIO
from pathlib import Path

from agent_memory_eval.analysis.claim_evidence import paired_counts, trace_claim
from agent_memory_eval.analysis.selection import worker_choices
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import FORMS
from agent_memory_eval.experiments.forms_write import _project_context
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.experience import _closed_run, _pair
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path
from scripts.audit_retail_order_choice import publish


SPEC = "protocols/diagnostics/memory-claim-citations-v1.json"
SCORES = {"True": True, "False": False, "": None}


def index(rows, fields):
    result = {}
    for r in rows:
        key = tuple(r[k] for k in fields)
        if key in result:
            raise ContractError(f"Duplicate comparison unit {key}")
        result[key] = r
    return result


def airline_groups(data):
    rows = [dict(r, repetition=int(r["repetition"])) for r in csv.DictReader(StringIO(data["airline_scores"]))]
    units = ("task_id", "repetition")
    controls = index((r for r in rows if r["condition"] == "no_memory"), units)
    annotations = index(data["airline_writing"]["annotations"], (*units, "arm"))
    treatments = index((r for r in rows if r["condition"] in FORMS), (*units, "condition"))
    if set(treatments) != {(*k, form) for k in controls for form in FORMS}:
        raise ContractError("Missing scheduled form or control")
    pairs = defaultdict(list)
    for (task, rep, form), row in treatments.items():
        review = annotations[task, rep, form]
        control = controls[task, rep]
        if (review["grounding"] != row["reviewed_grounding"] or row["control_success"] != control["official_success"]
                or review["item_count"] != int(row["writer_items"])):
            raise ContractError("Writing review, stored items or control score differs")
        label = review["grounding"]
        if label == "empty" and (review["item_count"] or row["normal_response_memory_spans"] not in ("0", "")):
            raise ContractError("An empty bank has an item or an exposure")
        pairs[label].append((SCORES[row["official_success"]], SCORES[control["official_success"]]))
    return dict(metric="official_success", control="no_memory", distinct_control_assignments=len(controls),
                by_bank_grounding={k: paired_counts(v) for k, v in sorted(pairs.items())},
                all=paired_counts(p for v in pairs.values() for p in v))


def retail_groups(data):
    units = ("customer", "task", "source")

    def previous(rows, arm):
        return index((r for r in rows if r["mode"] == "previous_only" and r["arm"] == arm), units)

    banks = previous(data["retail_banks"]["decisions"], "query")
    rows = data["retail_decisions"]["decisions"]
    real, empty = previous(rows, "conditional_query"), previous(rows, "conditional_empty_query")
    if real.keys() != empty.keys() or real.keys() != banks.keys():
        raise ContractError("Different retail history or task allocations")
    pairs = defaultdict(list)
    target_seen = target_seen_correct = 0
    for key, row in real.items():
        bank = banks[key]
        label = "present" if bank["bank_mentions_target_order"] else "absent"
        if any(span["target_identifier"] for span in row["included"]):
            if label == "absent":
                raise ContractError("Identifier reached a decision from a bank classified as lacking it")
            target_seen += 1
            target_seen_correct += int(row["focused"] is True)
        pairs[label].append((row["focused"], empty[key]["focused"]))
    costs = {}
    for label, arm in (("real", real), ("empty", empty)):
        costs[label] = dict(worker_requests=sum(r["requests_started"] for r in arm.values()),
                            confirmed_usd=str(sum(Decimal(r["confirmed_usd"]) for r in arm.values())),
                            held_usd=str(sum(Decimal(r["held_usd"]) for r in arm.values())))
    return dict(metric="focused_target_first_business_action", guidance="conditional", control="empty_query",
                by_target_identifier_in_bank={k: paired_counts(v) for k, v in sorted(pairs.items())},
                all=paired_counts(p for v in pairs.values() for p in v),
                target_identifier_in_decision_input=target_seen, correct_with_identifier_in_input=target_seen_correct,
                costs_to_first_decision=costs)


def journal_folder(root, ref):
    asset_text(root, ref)
    journal = safe_path(root, ref["path"])
    if journal.name != "log.jsonl":
        raise ContractError("Citation must pin a log.jsonl")
    return journal.parent


def original_source(folder, writing):
    run, = writing["source_runs"]
    scan, _, _ = read_experiment(folder)
    _, end, score = _closed_run(scan, run)
    request, finish = _pair(scan, run)
    blobs = BlobStore(folder)
    req, res = request["payload"]["request_ref"], finish["payload"]["response_ref"]
    projection = _project_context(blobs.get(req), blobs.get(res))
    if digest(canonical(projection)) != writing["public_context_sha256"]:
        raise ContractError("Writer's public experience differs from the first session's original transcript")
    return dict(source_run=run, final_request_sha256=req["sha256"], final_response_sha256=res["sha256"],
                end_event=end["event_id"], score_event=score["event_id"],
                analyst_official_success=score["payload"]["official_success"])


def matched_outcomes(data, traces):
    # Join through the verified worker run or the pinned raw job, then use the original experiment's pairing.
    real = next(r for r in data["retail_decisions"]["decisions"]
                if r["run"] == traces["retail_previous_order"]["decision"]["worker_run"])
    if real["decision_request"] != traces["retail_previous_order"]["decision"]["request_id"]:
        raise ContractError("Selected source is not the scored first decision")
    units = ("customer", "task", "source", "mode", "arm")
    choices = index(data["retail_decisions"]["decisions"], units)
    control_key = tuple(real[k] for k in units[:-1]) + ("conditional_empty_query",)
    control = choices[control_key]
    first = {r["arm"]: {k: r[k] for k in ("run", "decision_request", "behavior", "focused")} for r in (real, control)}
    decisions = traces["retail_handoff"]["decision"]
    cells = data["retail_handoffs"]["cells"]
    row = next(r for r in cells if r["run"] == decisions["worker_run"])
    handoff = {r["arm"]: {k: r[k] for k in ("run", "official", "position")} for r in cells
               if all(r[k] == row[k] for k in ("study", "customer", "task", "repeat", "stage"))
               and (r["run"] == row["run"] or r["arm"] in ("empty_preload", "no_memory"))}
    diagnostic = data["airline_diagnostics"]
    job = int(Path(traces["airline_upgrade"]["worker"]["path"]).parent.name)
    pair = next(r for r in diagnostic["discordant_comparisons"] if r["job"] == job)
    probe = next(r for r in diagnostic["diagnostic_db_probes"] if r["job"] == job)
    review = next(r for r in diagnostic["verified_business_trajectories"] if r["job"] == job)
    if (probe["task"] != pair["task"] or review["task"] != pair["task"]
            or probe["original_db_match"] != review["db_match"]):
        raise ContractError("Inconsistent airline outcome or trajectory")
    return dict(retail_previous_order=first, retail_handoff=handoff, airline_upgrade=dict(
        official_pair={k: pair[k] for k in ("task", "repetition", "job", "control", "direction", "empty_control_success")},
        selected_public_turns=review["selected_public_turns"],
        db_probe={k: probe[k] for k in ("edit", "original_db_match", "edited_db_match")}))


def analyze(root, *, cohorts_only=False):
    spec_bytes = (root / SPEC).read_bytes()
    spec = read_json(spec_bytes)
    data = {}
    for key, ref in spec["inputs"].items():
        if cohorts_only and key in {"retail_handoffs", "airline_diagnostics"}:
            continue
        content = asset_text(root, ref)
        data[key] = content if key == "airline_scores" else read_json(content.encode())
    result = dict(format="agent-memory-claim-evidence/v1", citations_sha256=digest(spec_bytes),
                  retail_first_decisions=retail_groups(data), airline_second_pass=airline_groups(data))
    if cohorts_only:
        return result
    traces = {}
    for case in spec["cases"]:
        args = {k: case[k] for k in ("item", "version", "field", "clause", "source_citations",
                                     "request_id", "current_citations", "action_citations")}
        trace = trace_claim(journal_folder(root, case["writer"]), journal_folder(root, case["worker"]), **args)
        source = original_source(journal_folder(root, case["source"]), trace["writing"])
        traces[case["id"]] = dict(annotation=case["annotation"], source=dict(**case["source"], **source),
                                  writer=case["writer"], worker=case["worker"], **trace)
    result.update(selection=spec["selection"], traces=traces, matched_outcomes=matched_outcomes(data, traces))
    return result


def retail_selection(root):
    spec = read_json((root / SPEC).read_bytes())
    ref = spec["inputs"]["retail_decisions"]
    results = read_json(asset_text(root, ref).encode())
    folder = safe_path(root / "runs", results["study"])
    registration = dict(path="registration.json", sha256=results["registration_sha256"])
    jobs = read_json(asset_text(folder, registration).encode())["jobs"]["decision"]
    selected = {j["run"]: j for j in jobs if j["arm"] == "conditional_query" and j["mode"] == "previous_only"}
    rows = index(results["decisions"], ["run"])
    if set(selected) != {r["run"] for r in rows.values() if r["arm"] == "conditional_query" and r["mode"] == "previous_only"}:
        raise ContractError("Published selection cohort differs from registration")
    counts = Counter(dict.fromkeys(("no_get", "get_without_unique_list_position", "get_with_unique_list_position",
                                    "get_with_alternatives", "first_position_with_alternatives", "selected_title_contains_target"), 0))
    ranks, cases = Counter(), []
    for run, job in selected.items():
        row = rows[run,]
        if any(row[k] != job[k] for k in ("task", "customer", "mode", "arm")):
            raise ContractError("Selection outcome differs from the registered case")
        trace = worker_choices(safe_path(folder, run), run_id=run, through_request=row["decision_request"])
        steps = trace["choices"]
        if (not steps or steps[0]["request_ref"]["sha256"] != job["initial_sha256"]
                or (row["decision_request"] is None and (row["status"] != "private_read_limit" or len(steps) != 4
                                                         or any(s["choice"] != "memory_tools" for s in steps)))
                or not all(s["status"] == "response" for s in steps)):
            raise ContractError("Incomplete or different retail selection prefix")
        first = trace["first_body_choice"]
        if first["get_requested"] is not row["get_requested"]:
            raise ContractError("Get observation disagrees with the published decision")
        case = dict(run=run, through_request=row["decision_request"], focused=row["focused"],
                    first_get=first.get("request_id"), returned_status=first.get("returned_status"))
        entries = first["matched_list_entries"]
        if not first["get_requested"]:
            counts["no_get"] += 1
        elif len(entries) != 1:
            counts["get_without_unique_list_position"] += 1
        else:
            entry, = entries
            ranks[entry["size"], entry["position"]] += 1
            counts["get_with_unique_list_position"] += 1
            counts["get_with_alternatives"] += entry["size"] > 1
            counts["first_position_with_alternatives"] += entry["size"] > 1 and entry["position"] == 1
            case.update(position=entry["position"], page_size=entry["size"],
                        selected_title_contains_target=job["target"] in entry["title"])
            counts["selected_title_contains_target"] += case["selected_title_contains_target"]
        cases.append(case)
    return dict(format="retail-first-memory-choice/v1", source=ref, registration=registration, assigned=len(selected),
                mode="previous_only", arm="conditional_query", counts=dict(counts),
                by_page=[dict(page_size=n, position=i, cases=k) for (n, i), k in sorted(ranks.items())], cases=cases)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cohorts-only", action="store_true", help="Recompute both full cohorts from Git assets alone")
    mode.add_argument("--retail-selection", action="store_true", help="Audit the first selected title in the previous-order real-bank cohort")
    args = parser.parse_args()
    result = retail_selection(ROOT) if args.retail_selection else analyze(ROOT, cohorts_only=args.cohorts_only)
    publish(args.output, result)
