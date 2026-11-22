"""Independently trace the assigned banks, public inputs, charges and final retail DB against the registration."""

import argparse
from collections import Counter
from decimal import Decimal
from functools import cache
from pathlib import Path

from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.experience import public_text_message
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from scripts.analyze_full_retail import require
from scripts.analyze_retail_two_visit import public_turns
from scripts.audit_retail_order_choice import publish
from scripts.replay_full_retail_fields import reference_state
from scripts.run_full_retail import modules_for
from scripts.run_retail_two_visit import read, read_receipt, sealed_bank


def audit(root, folder, stage, public_path):
    reg = read(folder / "registration.json")
    plan = reg["jobs"]
    if reg.get("format") == "retail-derived-two-visit/recovery-v1":
        plan = {**plan, **read(folder / "upstreams.json")["selected"]}
    design = reg["design"]
    tasks = {r["task"]: r for r in plan["source"]}
    writers = {(r["task"], r["arm"]): r for r in plan["writer"]}
    cohorts = read(root / design["cohort"]["path"])
    expected = {c["customer"]: c["expected_db"] for c in cohorts["reserved"]}
    public = read(public_path) if public_path else None
    if public is not None:
        require(digest(canonical(public)) == cohorts["public_prefixes_sha256"], "Public state archive changed")
    if stage == "continuation":
        require(public is not None, "Final audit needs public states")
        modules_for(root)
        from tau2.domains.retail.environment import get_environment

        native = get_environment()
    sessions = set()
    receipt_ids = {}
    roles = Counter()
    statuses = Counter()
    missing = Counter()
    money = Decimal(0)
    steps = Counter()
    total_spans = 0

    @cache
    def public_source(source):
        job = tasks[source]
        path = folder / job["run"]
        scan, _, _ = read_experiment(path)
        events = list(scan["events"].values())
        last = next(
            e["payload"]
            for e in reversed(events)
            if e["type"] == "model.request_started" and e["payload"]["actor_role"] == "worker"
        )
        end = next(
            e["payload"]
            for e in events
            if e["type"] == "model.request_finished" and e["payload"]["request_id"] == last["request_id"]
        )
        blobs = BlobStore(path)
        msgs = blobs.get(last["request_ref"])["messages"]
        return {
            "kind": "past_public_experience",
            "task_context": public_text_message(msgs[0]),
            "messages": [
                public_text_message(m)
                for m in msgs[1:] + [blobs.get(end["response_ref"])["choices"][0]["message"]]
            ],
        }

    @cache
    def bank(source):
        runs, items = [], []
        for form in design["forms"]:
            w = writers[source, form]
            path = folder / w["run"]
            wr = read_receipt(path, "writer")
            runs.append(w["run"])
            items.extend(sealed_bank(path, wr, w["run"]))
        return runs, items

    for j in plan[stage]:
        path = folder / j["run"]
        result = read_receipt(path, stage)
        if not result:
            missing["no_receipt"] += 1
            continue
        statuses[result["status"]] += 1
        if result["status"] not in {"completed", "empty"}:
            continue
        evidence = read_experiment(path)
        scan, state, exp = evidence
        require(exp == j["run"], "Unexpected experiment ID")
        money_check = audit_requests(path, evidence=evidence)
        money += Decimal(money_check["observed_cost_usd_decimal"])
        total_spans += money_check["memory_input_spans"]
        blobs = BlobStore(path)
        start = scan["starts"][j["run"]]
        require(
            start["record_kind"] == "observed"
            and start["session_id"] not in sessions
            and start["payload"]["fresh_session"],
            "Session was reused",
        )
        sessions.add(start["session_id"])
        reqs = []
        for e in scan["events"].values():
            p = e["payload"]
            if e["type"] == "model.request_started":
                role = p["actor_role"]
                wire = blobs.get(p["request_ref"])
                names = {t["function"]["name"] for t in wire.get("tools", [])}
                require(
                    not {"previous_response_id", "conversation", "thread_id"}.intersection(wire),
                    "Stateful request",
                )
                if role == "user_simulator":
                    require(
                        not any(n.startswith("memory_") for n in names)
                        and "<memory_policy>" not in wire["messages"][0]["content"],
                        "Private interface sent to simulator",
                    )
                roles[role] += 1
                reqs.append((role, wire))
            if e["type"] == "model.request_finished" and p["response_ref"]:
                raw = blobs.get(p["response_ref"])
                if p["status"] == "response":
                    rid = raw["id"]
                    require(rid not in receipt_ids, "Duplicate provider ID")
                    receipt_ids[rid] = p["request_id"]
        if stage == "source":
            require(
                start["payload"]["bank_id"] is None and not start["payload"]["source_run_ids"],
                "First visit has memory",
            )
            require(
                all(
                    not any(t["function"]["name"].startswith("memory_") for t in w.get("tools", []))
                    for _, w in reqs
                ),
                "Memory tool in first visit",
            )
        if stage == "writer":
            first = reqs[0][1]
            require(
                all(
                    r == "writer" and {t["function"]["name"] for t in w["tools"]} == {"memory_store"}
                    for r, w in reqs
                ),
                "Writer received unexpected tools",
            )
            msgs = first["messages"]
            require(
                len(msgs) == 3
                and read_json(msgs[1]["content"].encode()) == public_source(j["task"])
                and read_json(msgs[2]["content"].encode()) == {"kind": "evaluation_feedback", "level": "none"},
                "Writer input differs from source or feedback assignment",
            )
        if stage == "continuation":
            arm = j["arm"]
            lineage, items = bank(j["source"]) if arm in {"preload", "query"} else ([], [])
            p = start["payload"]
            require(
                p["source_run_ids"] == lineage and (p["bank_id"] is None) == (arm == "no_memory"),
                "Bank ownership differs",
            )
            actual = snapshot_items(blobs, state, p["start_snapshot_id"]) if p["bank_id"] else []
            require(items == actual, "Wrong items or order received")
            role, w = reqs[0]
            messages = w["messages"]
            preload = arm in {"preload", "empty_preload"}
            require(
                role == "worker" and messages[2 if preload else 1 :] == public[j["prefix"]],
                "History exposed the answer or changed",
            )
            if preload:
                require(
                    messages[1]["role"] == "user"
                    and read_json(messages[1]["content"].encode()) == {"items": items},
                    "Incorrect preload",
                )
            trace = public_turns(blobs.get(result["trajectory"]))
            actions = [
                {
                    "name": trace[t["action"]]["name"],
                    "arguments": trace[t["action"]]["arguments"],
                    "requestor": trace[t["action"]]["actor"],
                }
                for t in trace
                if t["role"] == "result" and t["ok"]
            ]
            env = get_environment(db=native.tools.db, policy=native.policy)
            hashes, errors = reference_state(env, actions)
            require(
                not errors and int(hashes[0] == expected[j["customer"]][j["target"]]) == result["db_reward"],
                "Final live score failed independent DB reconstruction",
            )
            steps["native_db_regraded"] += 1
    return {
        "stage": stage,
        "registered": len(plan[stage]),
        "receipts": dict(statuses),
        "missing": dict(missing),
        "audited_sessions": len(sessions),
        "provider_responses": len(receipt_ids),
        "provider_ids_sha256": digest(canonical(sorted(receipt_ids))),
        "requests_by_role": dict(roles),
        "included_spans": total_spans,
        "confirmed_usd_for_audited_sessions": str(money),
        "checks": dict(steps),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("source", "writer", "continuation"))
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--public", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(ROOT, args.run, args.stage, args.public)
    if args.output:
        publish(args.output, result)
    print(result)
