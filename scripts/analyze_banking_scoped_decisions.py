"""Reopen fixed-state trials, mask writer identity and score paired one-response outcomes."""

import argparse
from collections import Counter
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.claim_evidence import paired_counts
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.runner import fixed_decision
from scripts import review_banking_scoped_writer as review
from scripts import run_banking_policy_cycle as cycle
from scripts import run_banking_scoped_decisions as decisions
from scripts import run_banking_scoped_writer as study

LABELS = {
    "before_handoff": (
        "investigate",
        "unsupported_handoff",
        "unsupported_state",
        "premature_transfer",
        "other",
    ),
    "after_handoff": (
        "acknowledge",
        "duplicate_transfer",
        "redundant_check",
        "unsupported_resolution",
        "other",
    ),
    "after_identity_check": ("verified_lookup", "redundant_check", "other_supported", "other"),
    "answer_from_current_terms": (
        "conditional_answer",
        "boundary_error",
        "relevant_verification",
        "unnecessary_delay",
        "unsupported_personal_claim",
        "other",
    ),
}
FLAGS = {
    "unsupported_current_state",
    "unsupported_resolution",
    "redundant_check",
    "delayed_progress",
    "unrequested_action",
    "relevant_verification",
}

# These two literal search terms were selected after reading masked worker responses.
OPENING_TERMS = {"task_016/1": "IN_PROGRESS", "task_017/1": "Silver Rewards"}


def public_action(choice):
    m = choice["message"]
    calls = []
    for c in m.get("tool_calls") or []:
        f = c["function"]
        try:
            args = read_json(f["arguments"].encode())
        except (ValueError, AttributeError):
            args = f["arguments"]
        calls.append({"name": f["name"], "arguments": args})
    return {"text": m.get("content") or "", "calls": calls}


def collect(workspace):
    cfg, planned, cells, _ = decisions.prepare(workspace)
    folder = workspace / "runs" / cfg["name"]
    reg, done = [cycle.read(folder / name) for name in ("registration.json", "completion.json")]
    require(
        reg["slots"] == planned
        and reg["profile"] == cfg
        and done["registration_sha256"] == digest((folder / "registration.json").read_bytes()),
        "Changed registered decisions",
    )
    model = {
        "pricing": reg["reservation_pricing"],
        "reservation_pricing": reg["reservation_pricing"],
        "identity": reg["identity"],
    }
    blind, contexts, rows = {}, {}, []
    for slot in planned:
        base, _, (note,), wire, _ = cells[slot["case"], slot["form"], slot["arm"]]
        context_ref = digest(canonical(base))
        contexts.setdefault(
            slot["case"],
            {"task": slot["task"], "role": slot["role"], "public_wire_sha256": context_ref},
        )
        row = {
            "slot": slot["slot"],
            "case": slot["case"],
            "role": slot["role"],
            "form": slot["form"],
            "repeat": slot["repeat"],
            "arm": slot["arm"],
            "run": slot["run"],
            "initial_sha256": slot["initial_sha256"],
            "observed_id": None,
            "status": "unstarted",
            "confirmed_usd": "0",
            "held_usd": "0",
            "note_words": sum(len(note[k].split()) for k in ("title", "body")),
        }
        rows.append(row)
        name = str(slot["slot"])
        location = folder / f"slot-{slot['slot']:03}"
        if name not in done["journals"]:
            require(not (location / "log.jsonl").exists(), "Omitted slot journal")
            continue
        receipt = fixed_decision.receipt(
            location, slot, model, decisions.recorded_type, result_origin=study.ORIGIN, full_audit=True
        )
        require(
            receipt["journal_sha256"] == done["journals"][name],
            "Slot journal differs from sealed study",
        )
        row.update(
            status=receipt["error"] or receipt["behavior"],
            confirmed_usd=receipt["confirmed_usd"],
            held_usd=receipt["held_usd"],
            journal=receipt["journal_sha256"],
            submitted=receipt["submitted_or_uncertain"],
            session=receipt["session"],
            generations=receipt["generations"],
        )
        if receipt["error"] or receipt["behavior"] == "unobserved":
            continue
        log = RecordedDecisions(location, slot["run"], receipt["journal_sha256"])
        (req,) = log.requests
        (grade,) = log.of_type("outcome.scored")
        require(
            log.blobs.get(req["request_ref"]) == wire
            and req["actor_role"] == "worker"
            and {span["field"] for span in log.visible(req, wire)} == {"title", "body"}
            and receipt["complete"]
            and not receipt["memory_calls"]
            and grade["payload"]["grading_status"] == "not_available"
            and grade["payload"]["official_success"] is None,
            "Unexpected fixed input, exposure or grading",
        )
        raw = log.finishes[req["request_id"]]
        action = public_action(log.reply(req))
        # Keep frequency, form, severity, arm and replicate outside the rating packet.
        token = digest(canonical([slot["case"], action]))[:20]
        view = {"case": slot["case"], "action": action}
        require(blind.setdefault(token, view) == view, "Colliding blinded decision IDs")
        row.update(
            observed_id=token,
            request=req["request_id"],
            response_sha256=raw["response_ref"]["sha256"],
            tokens=raw["usage"]["total_tokens"],
            output_tokens=raw["usage"]["completion_tokens"],
            business_tools=[c["name"] for c in action["calls"]],
        )
        term = OPENING_TERMS.get(slot["case"])
        if term:
            needle = term.casefold()
            row["opening_term"] = {
                "term": term,
                "in_original_request": needle in canonical(base).decode().casefold(),
                "in_shown_note": any(needle in note[k].casefold() for k in ("title", "body")),
                "in_search": any(
                    c["name"] == "grep" and needle in canonical(c["arguments"]).decode().casefold()
                    for c in action["calls"]
                ),
            }
    all_generations = [g for r in rows for g in r.get("generations", ())]
    sessions = [r["session"] for r in rows if r.get("session")]
    require(
        len(set(all_generations)) == len(all_generations)
        and len(set(sessions)) == len(sessions)
        and sum(r.get("submitted", 0) for r in rows) == done["submitted"]
        and len(done["journals"]) == done["attempted"]
        and done["unstarted"] + done["attempted"] == len(rows),
        "Duplicate or unaccounted decision",
    )
    for field in ("confirmed_usd", "held_usd"):
        require(
            sum(Decimal(r[field]) for r in rows) == Decimal(done[field]),
            "Decision cost does not reconcile",
        )
    return {"states": contexts, "decisions": dict(sorted(blind.items()))}, rows, done


def summarize(rows):
    by_arm = {}
    for arm in ("incumbent", "scoped_candidate"):
        subset = [r for r in rows if r["arm"] == arm]
        observed = [r for r in subset if r["observed_id"] is not None]
        cell_outcomes = {}
        for r in observed:
            cell_outcomes.setdefault((r["case"], r["form"]), set()).add(
                r["label"] == LABELS[r["role"]][0]
            )
        by_arm[arm] = {
            "assigned": len(subset),
            "observed": len(observed),
            "labels": dict(Counter(r["label"] for r in observed)),
            "flags": dict(Counter(flag for r in observed for flag in r["flags"])),
            "targeted": sum(r["label"] == LABELS[r["role"]][0] for r in observed),
            "observed_cells": len(cell_outcomes),
            "cells_with_both_outcomes": sum(len(v) == 2 for v in cell_outcomes.values()),
            "tokens": sum(r["tokens"] for r in observed),
            "output_tokens": sum(r["output_tokens"] for r in observed),
            "cost": str(sum(Decimal(r["confirmed_usd"]) for r in subset)),
        }
    groups = {}
    for r in rows:
        group = groups.setdefault((r["case"], r["form"], r["repeat"]), {})
        require(r["arm"] not in group, "Duplicate assigned policy")
        group[r["arm"]] = None if r["observed_id"] is None else r["label"] == LABELS[r["role"]][0]
    paired = paired_counts((g["scoped_candidate"], g["incumbent"]) for g in groups.values())
    return {
        "by_arm": by_arm,
        "paired": {
            "both": paired["both_success"],
            "neither": paired["both_failure"],
            "incumbent_only": paired["loss"],
            "candidate_only": paired["gain"],
            "missing": paired["missing"],
        },
    }


def evaluate(workspace, ratings):
    packet, rows, done = collect(workspace)
    require(
        ratings["packet_sha256"] == digest(canonical(packet))
        and set(ratings["labels"]) == set(packet["decisions"]),
        "Incomplete or different masked response ratings",
    )
    for r in rows:
        if r["observed_id"] is not None:
            rating = ratings["labels"][r["observed_id"]]
            require(
                rating["label"] in LABELS[r["role"]]
                and len(set(rating["flags"])) == len(rating["flags"])
                and set(rating["flags"]) <= FLAGS,
                "Invalid decision rating",
            )
            r.update(label=rating["label"], flags=rating["flags"])
    _, sources, _ = review.material(workspace)
    source_terms = {}
    for case, term in OPENING_TERMS.items():
        experience = read_json(sources[case.split("/")[0]].public_context.encode())
        source_terms[case] = {
            "term": term,
            "public_message_indices": [
                i
                for i, msg in enumerate(experience["messages"])
                if term.casefold() in canonical(msg).decode().casefold()
            ],
        }
    return {
        "format": "scoped-memory-decision-result/v1",
        "registration": done["registration_sha256"],
        "completion": done,
        "packet_sha256": ratings["packet_sha256"],
        "ratings_sha256": digest(canonical(ratings)),
        "exploratory_search_terms_in_writer_source": source_terms,
        "overall": summarize(rows),
        "by_role": {r: summarize([x for x in rows if x["role"] == r]) for r in LABELS},
        "by_state": {
            case: summarize([r for r in rows if r["case"] == case]) for case in packet["states"]
        },
        "by_form": {
            form: summarize([r for r in rows if r["form"] == form])
            for form in sorted({r["form"] for r in rows})
        },
        "trials": rows,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase", choices=("blind", "evaluate"))
    p.add_argument("--workspace", type=Path, default=study.ROOT)
    p.add_argument("--ratings", type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    if args.phase == "evaluate" and args.ratings is None:
        p.error("evaluate requires --ratings")
    out = (
        collect(args.workspace)[0]
        if args.phase == "blind"
        else evaluate(args.workspace, cycle.read(args.ratings))
    )
    cycle.save(args.output, out)
