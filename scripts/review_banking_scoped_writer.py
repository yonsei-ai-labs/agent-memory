"""Blind the two writing policies, bind citations to the original records and evaluate the registered gate."""

import argparse
from collections import Counter
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from scripts import run_banking_scoped_writer as study
from scripts import run_banking_policy_cycle as cycle
from scripts.analyze_banking_policy_cycle import checked_shard

SIDES = ("A", "B")
POLICIES = ("incumbent", "scoped_candidate")
RATINGS = "artifacts/analyses/20261125-scoped-writer-blind-ratings.json"
REVIEW = "artifacts/analyses/20261125-scoped-writer-review.json"


def material(workspace):
    p, d, previous = study.setup()
    root = workspace / "runs" / p["writer_study"]
    reg, close = [cycle.read(root / name) for name in ("registration.json", "completion.json")]
    require(digest((root / "registration.json").read_bytes()) == close["registration_sha256"]
            and reg["preflight"]["protocol_sha256"] == digest((study.ROOT / study.PROFILE).read_bytes()),
            "Different writing registration")
    covered = sum(Decimal(v["spent"]) for v in close["stages"].values())
    require(covered == Decimal(close["total_covered_usd"]) <= Decimal(p["additional_limit_usd"]),
            "Unreconciled writing expense")
    prior = cycle.read(study.ROOT / d["previous_protocol"]["path"])
    old = {(r["task"], r["form"]):r for r in previous["writers"] if r["policy"] == "existing"}
    notes, sources = {}, {}
    for case in reg["preflight"]["cases"]:
        task = case["task_id"]
        imported = case["imported_first_pass"]
        source = cycle.bank.matched.source_from(workspace / "runs" / imported["directory"], task)
        sources[task] = source
        for index, base in enumerate((workspace / "runs" / prior["validation_study"], root)):
            folder = base / ("task-" + task + ("__existing" if index == 0 else "")) / "write"
            rows = cycle.bank.matched.sealed_rows(folder)[2] if index == 0 else checked_shard(root, folder, close, reg)
            for row in rows or []:
                if row["status"] != "completed":
                    continue
                form = row["arm"]
                log_sha = row["result"]["log_sha256"]
                path = folder / row["directory"]
                bank = cycle.read_form_bank(path, expected_log_sha256=log_sha)
                if index == 0:
                    require(old[task,form]["bank"] == bank and old[task,form]["writer_log_sha256"] == log_sha,
                            "Changed incumbent bank")
                log = RecordedDecisions(path, row["run_id"], log_sha)
                first = next(r for r in log.requests if r["actor_role"] == "writer")
                wire = log.blobs.get(first["request_ref"])
                require(wire["messages"][1]["content"] == source.public_context
                        and read_json(wire["messages"][2]["content"].encode()) == source.feedback_message(d["feedback"])
                        and source.source_run_id == bank["source_run_id"] and bank["result_feedback"] == source.result_receipt()
                        and bank["run_id"] == row["run_id"], "Different source, bit or writer")
                if index == 1:
                    require(first["request_ref"]["sha256"] == case["first_writer_inputs"][form],
                            "Candidate received a different first request")
                (item,) = bank["items"]
                notes[task,form,POLICIES[index]] = dict(source=source.source_run_id, writer=bank["run_id"],
                    log_sha256=log_sha, snapshot=bank["snapshot_id"], item=item)
    return notes, sources, close


def packet(workspace):
    notes, sources, close = material(workspace)
    public, key = {}, {}
    for task, form in sorted({(t,f) for t,f,_ in notes}):
        if not all((task,form,k) in notes for k in POLICIES):
            continue
        ordered = sorted(POLICIES, key=lambda k: digest(canonical(["blind-order/v1", notes[task,form,k]["writer"]])))
        pair = digest(canonical(["review-pair", task, form, [notes[task,form,k]["writer"] for k in ordered]]))[:12]
        key[pair] = dict(zip(SIDES, ordered))
        public[pair] = dict(task=task, form=form, **{side:{f:notes[task,form,k]["item"][f] for f in ("title","body")}
                                                for side,k in zip(SIDES, ordered)})
    source_packet = {t:dict(experience=read_json(s.public_context.encode()), outcome=s.official_success)
                     for t,s in sources.items()}
    return dict(sources=source_packet, pairs=dict(sorted(public.items()))), key, notes, close


def evaluate(workspace, submission):
    p, _, _ = study.setup()
    blind, key, notes, close = packet(workspace)
    require(submission["packet_sha256"] == digest(canonical(blind)) and set(submission["pairs"]) == set(key),
            "Blind verdicts do not match the frozen packet")
    counts = Counter()
    details = []
    for pair, judgment in sorted(submission["pairs"].items()):
        shown = blind["pairs"][pair]
        task, form = shown["task"], shown["form"]
        sources = blind["sources"][task]["experience"]["messages"]
        for idx, quote in judgment["source"]:
            require(0 <= idx < len(sources) and isinstance(quote, str) and quote
                    and (quote in (sources[idx].get("content") or "") or
                         any(quote in c["function"]["name"] or quote in str(c["function"]["arguments"])
                             for c in sources[idx].get("tool_calls") or [])), "Unverified source quote")
        require(judgment["source"] and judgment["rationale"] and len(set(judgment["advantages"])) == len(judgment["advantages"])
                and len(set(judgment["unsupported"])) == len(judgment["unsupported"])
                and set(judgment["advantages"] + judgment["unsupported"]) <= set(SIDES), "Invalid review label")
        for side in SIDES:
            quote = judgment[side]
            require(quote and any(quote in shown[side][f] for f in ("title","body")), "Unverified note quote")
        adv = [key[pair][side] for side in judgment["advantages"]]
        ungrounded = [key[pair][side] for side in judgment["unsupported"]]
        counts.update(adv)
        counts.update("unsupported_" + k for k in ungrounded)
        counts["mixed" if len(adv)==2 else "tie" if not adv else "only_"+adv[0]] += 1
        refs = {k:{f:v for f,v in notes[task,form,k].items() if f!="item"} |
                  {"id":notes[task,form,k]["item"]["id"],"version":notes[task,form,k]["item"]["version"]} for k in POLICIES}
        details.append(dict(pair=pair, task=task, form=form, assignments=key[pair], advantages=adv,
                            unsupported_completion=ungrounded, references=refs))
    missing = p["gate"]["denominator"] - len(key)
    passed = (missing == 0 and counts["scoped_candidate"] > counts["incumbent"]
              and counts["unsupported_scoped_candidate"] <= counts["unsupported_incumbent"])
    return dict(format="scoped-memory-note-review/v1", registration=close["registration_sha256"],
                ratings_sha256=digest(canonical(submission)), packet_sha256=submission["packet_sha256"],
                reviewer=submission["reviewer"], gate_passed=passed, reviewed=len(key), missing=missing,
                counts=dict(counts), writer_ledger=close, pairs=details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("blind", "evaluate"))
    parser.add_argument("--workspace", type=Path, default=study.ROOT)
    parser.add_argument("--ratings", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.phase == "blind":
        result = packet(args.workspace)[0]
    else:
        result = evaluate(args.workspace, cycle.read(args.ratings))
    cycle.save(args.output, result)
    print(args.output)


if __name__ == "__main__":
    main()
