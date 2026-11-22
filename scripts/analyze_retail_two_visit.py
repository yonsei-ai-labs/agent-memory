"""Analyze registered first decisions and export blinded public turns for completed continuations."""

import argparse
import hmac
from collections import Counter, defaultdict
from decimal import Decimal
from hashlib import sha256
from math import comb
from pathlib import Path
from random import Random
from statistics import fmean, quantiles

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from scripts.analyze_full_retail import require
from scripts.audit_retail_order_choice import classify_reply, order_mentions, publish
from scripts.run_retail_two_visit import PREFLIGHT, key, read, read_receipt


def uptake(events, blobs):
    finishes = [
        e["payload"]
        for e in events
        if e["type"] == "model.request_finished" and e["payload"]["status"] == "response"
    ]
    choices = [blobs.get(f["response_ref"])["choices"][0] for f in finishes]
    names = [c["function"]["name"] for choice in choices for c in choice["message"].get("tool_calls") or []]
    fields = {e["payload"]["field"] for e in events if e["type"] == "memory.input_included"}
    returned = {
        field
        for e in events
        if e["type"] == "memory.returned"
        for item in e["payload"]["items"]
        for field in item["fields"]
    }
    return {
        "worker_calls": sum(e["type"] == "model.request_started" for e in events),
        "list_requested": "memory_list" in names,
        "get_requested": "memory_get" in names,
        "title_returned": "title" in returned,
        "body_returned": "body" in returned,
        "title_in_input": "title" in fields,
        "body_in_input": "body" in fields,
    }


def decision(folder, job, record):
    scan, state, experiment = read_experiment(folder)
    require(experiment == job["run"], "Wrong decision journal")
    blobs = BlobStore(folder)
    events = list(scan["events"].values())
    (mark,) = [e for e in events if e["payload"].get("origin") == "revisit.first_decision"]
    require(mark["payload"]["checkpoint_ref"] == record["checkpoint"], "Receipt points to a different decision")
    checkpoint = blobs.get(record["checkpoint"])
    require(
        checkpoint["public_prefix_sha256"] == job["prefix"] and checkpoint["request_id"] == record["request"],
        "Wrong checkpoint input",
    )
    before = [e for e in events if e["event_seq"] < mark["event_seq"]]
    starts = [e["payload"] for e in before if e["type"] == "model.request_started"]
    finishes = {e["payload"]["request_id"]: e["payload"] for e in before if e["type"] == "model.request_finished"}
    require(
        starts
        and {s["actor_role"] for s in starts} == {"worker"}
        and starts[-1]["request_id"] == record["request"]
        and all(finishes[s["request_id"]]["status"] == "response" for s in starts)
        and digest(canonical(blobs.get(starts[0]["request_ref"]))) == checkpoint["initial_sha256"],
        "First-decision trace incomplete",
    )
    response = blobs.get(finishes[record["request"]]["response_ref"])
    start = scan["starts"][job["run"]]["payload"]
    items = snapshot_items(blobs, state, start["start_snapshot_id"]) if start["bank_id"] else []
    require(
        digest(canonical(items)) == checkpoint["items_sha256"], "Starting bank differs from the saved decision"
    )
    mentioned = order_mentions("\n".join(i["title"] + "\n" + i["body"] for i in items), [job["target"]])
    return {
        "initial_sha256": checkpoint["initial_sha256"],
        "items_sha256": checkpoint["items_sha256"],
        "bank_size": len(items),
        "bank_mentions_target_order": bool(mentioned),
        "behavior": classify_reply(response["choices"][0], job["target"]),
        **uptake(before, blobs),
    }


def differences(rows, left, right, mode, position=None):
    cases = defaultdict(dict)
    for row in rows:
        if row["mode"] != mode or row["arm"] not in {left, right}:
            continue
        if position and (row["position"] == 1) != (position == "first"):
            continue
        case = cases[row["customer"], row["task"]]
        require(row["arm"] not in case, "Duplicate condition within case")
        case[row["arm"]] = None if row["behavior"] == "unobserved" else int(row["behavior"] == "focused_target")
    by_customer = defaultdict(list)
    bounds = defaultdict(list)
    for (customer, _), arms in cases.items():
        a, b = arms[left], arms[right]
        bounds[customer].append(
            ((0 if a is None else a) - (1 if b is None else b), (1 if a is None else a) - (0 if b is None else b))
        )
        if a is not None and b is not None:
            by_customer[customer].append(a - b)
    averages = {u: fmean(v) for u, v in by_customer.items()}
    n_positive = sum(v > 0 for v in averages.values())
    n_negative = sum(v < 0 for v in averages.values())
    n = n_positive + n_negative
    lower = [fmean(p[0] for p in pairs) for pairs in bounds.values()]
    upper = [fmean(p[1] for p in pairs) for pairs in bounds.values()]
    return averages, {
        "registered_pairs": len(cases),
        "complete_pairs": sum(map(len, by_customer.values())),
        "customers_with_pairs": len(averages),
        "difference": fmean(averages.values()) if averages else None,
        "gain": n_positive,
        "tie": len(averages) - n,
        "loss": n_negative,
        "two_sided_sign_p": min(1, 2 * sum(comb(n, i) for i in range(min(n_positive, n_negative) + 1)) / (2**n))
        if n
        else None,
        "bounds_over_all_registered": [fmean(lower), fmean(upper)] if bounds else None,
    }


def cluster_interval(left, right=None):
    """Resample customers once per draw, preserving shared membership in both position strata."""
    ids = sorted(set(left) | set(right or {}))
    if len(left) < 2 or (right is not None and len(right) < 2):
        return None
    rng = Random(20261122)
    draws = []
    for _ in range(10000):
        sample = rng.choices(ids, k=len(ids))
        a = [left[u] for u in sample if u in left]
        b = [right[u] for u in sample if u in right] if right is not None else [0]
        if a and b:
            draws.append(fmean(a) - fmean(b))
    q = quantiles(draws, n=40, method="inclusive")
    return [q[0], q[-1]]


def summaries(rows, design):
    contrasts = {}
    tab = []
    for mode in design["modes"]:
        for position in ("first", "later"):
            for arm in design["arms"]:
                group = [
                    r
                    for r in rows
                    if r["arm"] == arm and r["mode"] == mode and (r["position"] == 1) == (position == "first")
                ]
                tab.append(
                    dict(
                        mode=mode,
                        position=position,
                        arm=arm,
                        scheduled=len(group),
                        observed=sum(r["behavior"] != "unobserved" for r in group),
                        behavior=dict(Counter(r["behavior"] for r in group)),
                        statuses=dict(Counter(r["decision_status"] for r in group)),
                        **{
                            f: sum(r.get(f, False) for r in group)
                            for f in (
                                "list_requested",
                                "get_requested",
                                "title_returned",
                                "body_returned",
                                "title_in_input",
                                "body_in_input",
                            )
                        },
                    )
                )
        for a, b in (
            ("preload", "empty_preload"),
            ("query", "empty_query"),
            ("preload", "no_memory"),
            ("query", "no_memory"),
        ):
            values, stat = differences(rows, a, b, mode)
            if mode == "previous_only" and b in {"empty_preload", "empty_query"}:
                stat["customer_percentile_interval"] = cluster_interval(values)
            contrasts[f"{mode}/{a}/{b}"] = stat
    (early, early_stat), (late, late_stat) = [
        differences(rows, "preload", "empty_preload", "previous_only", p) for p in ("first", "later")
    ]
    return {
        "by_position": tab,
        "paired": contrasts,
        "previous_only_position_interaction": {
            "first": early_stat,
            "later": late_stat,
            "difference": fmean(late.values()) - fmean(early.values()) if early and late else None,
            "customer_percentile_interval": cluster_interval(late, early),
        },
    }


def public_turns(trajectory):
    """Only public customer and agent speech and business actions/results. Omit provider metadata and all private calls."""
    turns = []
    calls = {}
    for batch in trajectory:
        for m in batch.get("tool_messages", [batch]):
            role = m["role"]
            if role in {"user", "assistant"}:
                if m.get("content"):
                    turns.append({"role": role, "text": m["content"]})
                for c in m.get("tool_calls") or []:
                    if c["name"].startswith("memory_"):
                        continue
                    turn = {"role": "action", "name": c["name"], "arguments": c["arguments"], "actor": role}
                    calls[c["id"]] = len(turns)
                    turns.append(turn)
            elif role == "tool" and m["id"] in calls:
                turns.append(
                    {
                        "role": "result",
                        "action": calls[m["id"]],
                        "ok": not m["error"] and m["execution_status"] == "executed",
                        "text": m["content"],
                    }
                )
    return turns


def grade_turns(turns, target, db_reward, labels):
    if labels is not None:
        require(labels["turns_sha256"] == digest(canonical(turns)), "Review refers to another transcript")
    if labels is None or labels.get("status") == "unresolved":
        return {
            "consent_qualified": None,
            "clarifications": None,
            "wrong_proposals": None,
            "corrected_proposals": None,
        }
    require(labels["status"] == "reviewed", "Unknown review status")

    def speech(index, role):
        require(
            type(index) is int and 0 <= index < len(turns) and turns[index]["role"] == role,
            "Review speech index has the wrong speaker",
        )

    clarifications = labels["clarifications"]
    require(len(clarifications) == len(set(clarifications)), "Duplicate clarification")
    for i in clarifications:
        speech(i, "assistant")
    proposals = {}
    for p in labels["proposals"]:
        i = p["turn"]
        speech(i, "assistant")
        require(
            i not in proposals
            and isinstance(p["order"], str)
            and p["order"]
            and (p["reason"] is None or isinstance(p["reason"], str)),
            "Ambiguous proposal requires unresolved review",
        )
        proposals[i] = p
    answers = {}
    for r in labels["responses"]:
        i = r["turn"]
        speech(i, "user")
        require(
            i not in answers
            and r["proposal"] in proposals
            and r["proposal"] < i
            and r["disposition"] in {"confirmed", "refused", "withdrawn", "unclear"},
            "Invalid consent evidence",
        )
        answers[i] = r
    cancellations = [
        (t["action"], turns[t["action"]]["arguments"])
        for t in turns
        if t["role"] == "result" and t["ok"] and turns[t["action"]]["name"] == "cancel_pending_order"
    ]
    valid = bool(cancellations) and db_reward == 1
    for i, args in cancellations:
        preceding = [p for p in proposals if p < i]
        proposal = max(preceding, default=None)
        response = max((a for a, r in answers.items() if proposal is not None and proposal < a < i), default=None)
        valid = (
            valid
            and args.get("order_id") == target
            and proposal is not None
            and proposals[proposal]["order"] == target
            and (
                proposals[proposal]["reason"] == args.get("reason") == "no longer needed"
                and response is not None
                and answers[response]["proposal"] == proposal
                and answers[response]["disposition"] == "confirmed"
            )
        )
    wrong = [i for i, p in proposals.items() if p["order"] != target]
    return {
        "consent_qualified": bool(valid),
        "clarifications": len(clarifications),
        "wrong_proposals": len(wrong),
        "corrected_proposals": sum(
            any(j > i and p["order"] == target for j, p in proposals.items()) for i in wrong
        ),
    }


def full_summaries(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[r["mode"], r["arm"]].append(r)
    result = []
    for (mode, arm), group in sorted(groups.items()):
        completed = [r for r in group if r["completion"] == "completed"]
        reviewed = [r for r in completed if r.get("consent_qualified") is not None]
        result.append(
            dict(
                mode=mode,
                arm=arm,
                registered=len(group),
                completed=len(completed),
                db_success=sum(r["db_reward"] == 1 for r in completed),
                other_order_cancelled=sum(r["other_order_cancelled"] for r in completed),
                reviewed=len(reviewed),
                consent_qualified=sum(r["consent_qualified"] for r in reviewed),
                consultations_with_order_questions=sum(r["clarifications"] > 0 for r in reviewed),
                **{
                    k: sum(r[k] for r in reviewed)
                    for k in ("clarifications", "wrong_proposals", "corrected_proposals")
                },
            )
        )
    return result


def expense_by_stage(path):
    entries = {}
    if path.exists():
        with path.open("rb") as stream:
            for raw in stream:
                require(raw.endswith(b"\n"), "Torn expense ledger")
                row = read_json(raw)
                n = row["seq"]
                if "reserved" in row:
                    require(n == len(entries) + 1, "Expense sequence changed")
                    entries[n] = {"stage": row["stage"], "maximum": Decimal(row["reserved"]), "cost": None}
                else:
                    cost = Decimal(row["cost"])
                    require(
                        n in entries and entries[n]["cost"] is None and 0 <= cost <= entries[n]["maximum"],
                        "Invalid billing receipt",
                    )
                    entries[n]["cost"] = cost
    totals = defaultdict(lambda: {"calls": 0, "unpriced": 0, "confirmed_usd": Decimal(0), "held_usd": Decimal(0)})
    for row in entries.values():
        out = totals[row["stage"]]
        out["calls"] += 1
        if row["cost"] is None:
            out["held_usd"] += row["maximum"]
            out["unpriced"] += 1
        else:
            out["confirmed_usd"] += row["cost"]
    return {
        stage: {k: str(v) if isinstance(v, Decimal) else v for k, v in fields.items()}
        for stage, fields in totals.items()
    }


def analyze(root, folder, secret, reviews):
    registration = read(folder / "registration.json")
    design = registration["design"]
    require(
        registration["preflight"] == read(root / PREFLIGHT)
        and design == read(root / "protocols/drafts/tau2-retail-two-visit-v1.json")
        and key(registration["jobs"]) == registration["preflight"]["schedule_sha256"],
        "Registration differs from the offline cohort",
    )
    rows, packets, full = [], {}, []
    banks, initial = {}, {}
    for j in registration["jobs"]["decision"]:
        path = folder / j["run"]
        record = read_receipt(path, "decision")
        state = (
            record["status"]
            if record
            else ("interrupted" if (path / "decision.started").exists() else "not_started")
        )
        row = {
            "customer": j["customer"],
            "task": j["task"],
            "arm": j["arm"],
            "mode": j["mode"],
            "position": j["position"],
            "decision_status": state,
            "behavior": "unobserved",
        }
        if state in {"checkpoint", "decision"}:
            row.update(decision(path, j, record))
            if j["arm"] in {"query", "preload"}:
                require(
                    banks.setdefault(j["source"], row["items_sha256"]) == row["items_sha256"],
                    "Source bank differs by target or delivery",
                )
            if j["arm"] in {"query", "empty_query", "empty_preload", "no_memory"}:
                interface = "query" if j["arm"] in {"query", "empty_query"} else j["arm"]
                require(
                    initial.setdefault((interface, j["prefix"]), row["initial_sha256"]) == row["initial_sha256"],
                    "Same public prefix exposed a different current input",
                )
        elif (path / "log.jsonl").is_file():
            partial, _, experiment = read_experiment(path)
            require(
                experiment == j["run"]
                and not any(e["payload"].get("origin") == "revisit.continued" for e in partial["events"].values()),
                "Expected partial first-decision trace",
            )
            row.update(uptake(list(partial["events"].values()), BlobStore(path)))
        rows.append(row)
        if registration["continuation"] == "sample" and not j["in_sample"]:
            continue
        receipt = read_receipt(path, "continuation")
        blind = hmac.new(secret, j["run"].encode(), sha256).hexdigest()[:32]
        require(blind not in packets, "Duplicate blinded ID")
        finished = receipt and receipt["status"] == "completed"
        consult = dict(
            **row,
            review_id=blind,
            completion=receipt["status"]
            if receipt
            else ("interrupted" if (path / "continuation.started").exists() else "not_started"),
            db_reward=receipt.get("db_reward") if finished else None,
        )
        if finished:
            scan, _, experiment = read_experiment(path)
            require(experiment == j["run"], "Wrong full session")
            (graded,) = [e["payload"] for e in scan["events"].values() if e["type"] == "outcome.scored"]
            blobs = BlobStore(path)
            official = blobs.get(graded["official_artifact_ref"])
            require(
                official["trajectory_ref"] == receipt["trajectory"]
                and official["reward"] == graded["official_score"] == receipt["db_reward"],
                "Continuation receipt differs from the grade",
            )
            turns = public_turns(blobs.get(receipt["trajectory"]))
            consult["cancelled_orders"] = [
                turns[t["action"]]["arguments"].get("order_id")
                for t in turns
                if t["role"] == "result" and t["ok"] and turns[t["action"]]["name"] == "cancel_pending_order"
            ]
            consult["other_order_cancelled"] = any(oid != j["target"] for oid in consult["cancelled_orders"])
            packets[blind] = {"turns": turns, "turns_sha256": digest(canonical(turns))}
            consult.update(grade_turns(turns, j["target"], receipt["db_reward"], reviews.get(blind)))
        full.append(consult)
    require(reviews.keys() <= packets.keys(), "Review outside the completed cohort")
    return {
        "first": summaries(rows, design),
        "decisions": rows,
        "continuations": full,
        "full_by_mode_arm": full_summaries(full),
        "expense_by_stage": expense_by_stage(folder / "expense.jsonl"),
        "expense_summary": read(folder / "summary.json") if (folder / "summary.json").exists() else None,
    }, packets


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument(
        "--blind-key",
        type=Path,
        required=True,
        help="Analyst-only file of at least 32 random bytes, reused for grading",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--packets", type=Path)
    parser.add_argument("--reviews", type=Path)
    args = parser.parse_args()
    secret = args.blind_key.read_bytes()
    require(len(secret) >= 32, "Blinding key is too short")
    result, packets = analyze(ROOT, args.run, secret, read(args.reviews) if args.reviews else {})
    publish(args.output, result)
    if args.packets:
        publish(args.packets, dict(sorted(packets.items())))
