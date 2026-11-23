"""Score the registered conditional-retrieval decisions from their original requests and responses."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.paired_policy import customer_contrast
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT, pointer
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from scripts.analyze_full_retail import require
from scripts.audit_retail_order_choice import classify_reply, order_mentions, publish
from scripts.audit_retail_two_visit_billing import audit as audit_billing
from scripts.run_retail_conditional_retrieval import DRAFT, PREFLIGHT
from scripts.run_retail_two_visit import read, read_receipt

PRIVATE = {"memory_list", "memory_get"}
OBSERVED = {"checkpoint", "decision", "private_read_limit"}


def business_outcome(choice, target, history):
    message = choice["message"]
    raw = message.get("tool_calls") or []
    calls = [c for c in raw if c["function"]["name"] not in PRIVATE]
    business = dict(choice, message=dict(message, tool_calls=calls))
    category = classify_reply(business, target)
    orders = []
    for call in calls:
        if call["function"]["name"] == "get_order_details":
            oid = read_json(call["function"]["arguments"].encode()).get("order_id")
            if isinstance(oid, str) and oid:
                orders.append(oid)
    return dict(behavior=category, focused=category == "focused_target",
                strict_focused=classify_reply(choice, target) == "focused_target",
                prior_lookup=history in orders, other_order_lookup=any(oid != target for oid in orders),
                mutation=category == "mutation_requested", mixed=bool(calls and len(calls) < len(raw)))


def seen_in_input(events, req, wire, items, target):
    """Read only memory-return spans in the wire that generated this response."""
    included = []
    for e in events.values():
        p = e["payload"]
        if e["type"] != "memory.input_included" or p["request_id"] != req["request_id"]:
            continue
        if p["source"]["kind"] != "memory_return":
            continue
        producer = events[p["source"]["returned_event_id"]]
        field = p["field"]
        value = pointer(wire, p["pointer"])[p["char_start"]:p["char_end"]]
        item = items[p["item_id"], p["version_id"]]
        require(producer["type"] == "memory.returned" and producer["event_seq"] < e["event_seq"]
                and p["request_ref"] == req["request_ref"] and field in {"title", "body"}
                and value == canonical(item[field]).decode()[1:-1] and digest(value.encode()) == p["rendered_sha256"],
                "Unmatched memory span in the decision wire")
        included.append(dict(field=field, item=p["item_id"], version=p["version_id"],
                             target_identifier=bool(order_mentions(item[field], [target])),
                             request_id=req["request_id"], return_event=producer["event_id"]))
    return included


def score_session(folder, job, receipt):
    if receipt:
        status = receipt["status"]
    else:
        status = "interrupted" if (folder / "decision.started").exists() else "unstarted"
    row = {k: job[k] for k in ("customer", "task", "source", "arm", "mode", "position", "run", "in_sample")}
    row.update(status=status, reason=(receipt or {}).get("provider_error") or (receipt or {}).get("reason"),
               session=None, first_lookup=None, focused=None, strict_focused=None, prior_lookup=None,
               other_order_lookup=None, mutation=None, mixed=None, behavior=None, decision_request=None,
               list_requested=False, get_requested=False, title_returned=False, body_returned=False, included=[],
               requests_started=0, prompt_tokens=0, completion_tokens=0, cached_tokens=0, cache_write_tokens=0)
    if not (folder / "log.jsonl").exists():
        require(status not in OBSERVED, "Observed outcome lacks its journal")
        return row
    scan, state, run = read_experiment(folder)
    if not scan["starts"] and status == "interrupted":
        return row
    require(run == job["run"] and state["assignments"][run]["job"] == job, "Mismatched registered job")
    start = scan["starts"][run]
    blobs = BlobStore(folder)
    row["session"] = start["session_id"]
    items = snapshot_items(blobs, state, start["payload"]["start_snapshot_id"])
    require(start["payload"]["fresh_session"] and start["payload"]["source_run_ids"] == job["writer_runs"]
            and digest(canonical(items)) == job["items_sha256"], "Incorrect session or starting bank")
    events = scan["events"]
    if status in {"checkpoint", "decision"}:
        (marker,) = [e for e in events.values() if e["payload"].get("origin") == "revisit.first_decision"]
        require(marker["payload"]["checkpoint_ref"] == receipt["checkpoint"], "Wrong decision receipt")
        events = {k: e for k, e in events.items() if e["event_seq"] <= marker["event_seq"]}
    starts = [e["payload"] for e in events.values() if e["type"] == "model.request_started"]
    finishes = {e["payload"]["request_id"]: e["payload"]
                for e in events.values() if e["type"] == "model.request_finished"}
    require(all(r["actor_role"] == "worker" for r in starts) and len(starts) <= 4, "Unexpected actor or request count")
    if starts:
        require(starts[0]["request_ref"]["sha256"] == job["initial_sha256"], "First wire differs from registration")
    row["requests_started"] = len(starts)
    outputs = []
    returns = set()
    for e in events.values():
        p = e["payload"]
        if p.get("origin") == "tau2_worker_output":
            outputs.append(blobs.get(p["public_message_ref"]))
        elif e["type"] == "memory.returned":
            returns.update(field for it in p["items"] for field in it["fields"])
    row["title_returned"], row["body_returned"] = "title" in returns, "body" in returns
    selections = [{c["function"]["name"] for c in m.get("tool_calls") or []} for m in outputs]
    if selections:
        row["first_lookup"] = bool(selections[0] & PRIVATE)
    row["list_requested"] = any("memory_list" in s for s in selections)
    row["get_requested"] = any("memory_get" in s for s in selections)
    for f in finishes.values():
        usage = f.get("usage") or {}
        for field in ("prompt_tokens", "completion_tokens"):
            row[field] += usage.get(field) or 0
        for field in ("cached_tokens", "cache_write_tokens"):
            row[field] += (usage.get("prompt_tokens_details") or {}).get(field) or 0
    if status not in OBSERVED:
        return row
    require(starts and len(starts) == len(outputs) == len(finishes)
            and all(finishes[r["request_id"]]["status"] == "response" for r in starts),
            "Observed decision has incomplete responses")
    if status == "private_read_limit":
        require(len(selections) == 4 and all(s and s <= PRIVATE for s in selections)
                and any(e["payload"].get("termination_reason") == status for e in events.values()),
                "Private-only limit lacks four completed private responses")
        row.update(behavior=status, focused=False, strict_focused=False, prior_lookup=False,
                   other_order_lookup=False, mutation=False, mixed=False)
        return row
    cp = blobs.get(receipt["checkpoint"])
    req = starts[-1]
    rid = req["request_id"]
    require(cp["request_id"] == receipt["request"] == rid and cp["public_prefix_sha256"] == job["prefix"]
            and cp["initial_sha256"] == job["initial_sha256"] and cp["items_sha256"] == job["items_sha256"]
            and (status == "checkpoint") == job["in_sample"] and ("trajectory" in cp) == job["in_sample"],
            "Unregistered checkpoint or first business response")
    choice = blobs.get(finishes[rid]["response_ref"])["choices"][0]
    require(not selections[-1] or bool(selections[-1] - PRIVATE), "Missing first business action")
    row.update(business_outcome(choice, job["target"], job["history"]), decision_request=rid)
    row["included"] = seen_in_input(events, req, blobs.get(req["request_ref"]),
                                    {(i["id"], i["version"]): i for i in items}, job["target"])
    return row


def contrast(rows, terms, field, uncertainty=None):
    cases = defaultdict(dict)
    for row in rows:
        slot = (row["mode"], row["arm"])
        if slot in terms:
            group = cases[row["customer"], row["source"]]
            require(slot not in group, "Duplicated policy cell")
            group[slot] = row[field]
    return customer_contrast(cases, terms, uncertainty=uncertainty)


def summarize(rows, design):
    analysis = design["analysis"]
    mode = analysis["primary_mode"]
    terms = {(mode, arm): c for arm, c in analysis["primary_contrast"].items()}
    primary = contrast(rows, terms, "focused", analysis["uncertainty"])
    pairs = {}
    for left, right in (("conditional_query", "current_query"), ("conditional_query", "conditional_empty_query"),
                        ("current_query", "current_empty_query"), ("conditional_empty_query", "current_empty_query")):
        pairs[left + "_minus_" + right] = contrast(rows, {(mode, left): 1, (mode, right): -1}, "focused")
    selectivity_terms = {}
    for scenario in design["allocation"]["modes"]:
        weight = 1 if scenario == mode else -0.5
        selectivity_terms[scenario, "conditional_query"] = weight
        selectivity_terms[scenario, "current_query"] = -weight
    selectivity = contrast(rows, selectivity_terms, "first_lookup")
    harm = contrast(rows, {("explicit_other", "conditional_query"): 1, ("explicit_other", "current_query"): -1},
                    "prior_lookup", analysis["uncertainty"])
    evidence = [r for r in rows if r["mode"] == mode and r["arm"] == "conditional_query"
                and r["focused"] and r["included"]]
    positive_lower = bool(primary["interval"] and primary["interval"][0] > 0)
    conditions = {
        "positive_primary_interval_and_bound": positive_lower and primary["bounds"][0] > 0,
        "real_arm_improved": (pairs["conditional_query_minus_current_query"]["difference"] or 0) > 0,
        "conditional_real_beats_empty":
            (pairs["conditional_query_minus_conditional_empty_query"]["difference"] or 0) > 0,
        "selective_lookup": (selectivity["difference"] or 0) > 0,
        "correct_after_exposure": bool(evidence),
        "no_prior_order_increase": bool(harm["bounds"] and harm["bounds"][1] <= 0),
    }
    groups = []
    for scenario in design["allocation"]["modes"]:
        for arm in design["arms"]:
            group = [r for r in rows if r["mode"] == scenario and r["arm"] == arm]
            groups.append(dict(mode=scenario, arm=arm, scheduled=len(group),
                observed=sum(r["focused"] is not None for r in group),
                observable_first_lookup=sum(r["first_lookup"] is not None for r in group),
                statuses=dict(Counter(r["status"] for r in group)),
                behavior=dict(Counter(r["behavior"] for r in group if r["behavior"])),
                **{k: sum(r[k] or 0 for r in group) for k in (
                    "focused", "strict_focused", "first_lookup", "prior_lookup", "other_order_lookup", "mutation",
                    "list_requested", "get_requested", "title_returned", "body_returned", "mixed", "requests_started",
                    "prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens", "submissions")},
                title_in_decision_input=sum(any(e["field"] == "title" for e in r["included"]) for r in group),
                body_in_decision_input=sum(any(e["field"] == "body" for e in r["included"]) for r in group),
                **{k: str(sum((Decimal(r[k]) for r in group), Decimal(0))) for k in ("confirmed_usd", "held_usd")}))
    return dict(primary=primary, simple_contrasts=pairs, selectivity=selectivity, old_order_harm=harm,
                correct_after_exposure=len(evidence),
                correct_after_target_identifier=sum(any(e["target_identifier"] for e in r["included"])
                                                    for r in evidence),
                continuation_criteria=conditions, recommend_continuation=all(conditions.values()), by_mode_arm=groups)


def analyze(folder):
    reg = read(folder / "registration.json")
    design = read(ROOT / DRAFT)
    preflight = read(ROOT / PREFLIGHT)
    require(reg["design"] == design and reg["preflight"] == preflight
            and digest(canonical(reg["jobs"])) == preflight["registered_jobs_sha256"], "Registration changed")
    billing = audit_billing(folder, allow_interrupted_decisions=True)
    amounts = defaultdict(lambda: [0, Decimal(0), Decimal(0)])
    reserved = {}
    ledger = folder / "expense.jsonl"
    for line in ledger.read_bytes().splitlines() if ledger.exists() else ():
        entry = read_json(line)
        if "reserved" in entry:
            maximum = Decimal(entry["reserved"])
            reserved[entry["seq"]] = (entry["run"], maximum)
            amounts[entry["run"]][0] += 1
            amounts[entry["run"]][2] += maximum
        else:
            run, maximum = reserved.pop(entry["seq"])
            amounts[run][1] += Decimal(entry["cost"])
            amounts[run][2] -= maximum
    rows = []
    sessions = set()
    for job in reg["jobs"]["decision"]:
        path = folder / job["run"]
        row = score_session(path, job, read_receipt(path, "decision"))
        if row["session"]:
            require(row["session"] not in sessions, "Shared decision session")
            sessions.add(row["session"])
        calls, confirmed, held = amounts[job["run"]]
        rows.append(dict(row, submissions=calls, confirmed_usd=str(confirmed), held_usd=str(held)))
    historical = read_json(asset_text(ROOT, design["inputs"]["previous_results"]).encode())
    prior_expense = {stage: {k: str(sum(Decimal(historical[batch][stage][k]) for batch in
                                         ("expense_by_stage", "recovery_expense_by_stage")))
                             for k in ("confirmed_usd", "held_usd")} for stage in ("source", "writer")}
    return dict(study=design["name"], implementation_commit=reg["freeze"]["implementation_commit"],
                registration_sha256=digest((folder / "registration.json").read_bytes()), billing=billing,
                historical_expense_excluded_from_this_run=prior_expense,
                registered=len(rows), observed=sum(r["focused"] is not None for r in rows),
                statuses=dict(Counter(r["status"] for r in rows)), **summarize(rows, design), decisions=rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.run)
    publish(args.output, result)
    print({k: result[k] for k in ("registered", "observed", "primary", "recommend_continuation")})
