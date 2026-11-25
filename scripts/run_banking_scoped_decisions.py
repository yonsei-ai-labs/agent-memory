"""Compare two sealed note banks at the registered memory-free public decisions."""

import argparse
from collections import Counter
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from random import Random

from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.memory.context import memory_system_suffix
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.runner import fixed_decision
from scripts import review_banking_scoped_writer as review
from scripts import run_banking_policy_cycle as cycle
from scripts import run_banking_scoped_writer as study


def prepare(workspace):
    p, draft, previous = study.setup()
    ratings = cycle.read(study.ROOT / review.RATINGS)
    gate = review.evaluate(workspace, ratings)
    require(gate["gate_passed"] and gate == cycle.read(study.ROOT / review.REVIEW), "Unmet or changed writing gate")
    notes, _, close = review.material(workspace)
    prior, _, base, _, _ = cycle.design(workspace)
    notice = asset_text(study.ROOT, base["delivery_instruction"])
    cells, origins, roles = {}, {}, {}
    for state in draft["states"]:
        task, ordinal = state["task"], state["worker_ordinal"]
        case = f"{task}/{ordinal}"
        folder = workspace / "runs" / prior["source_study"] / ("task-" + task) / "source"
        _, _, (row,) = cycle.bank.matched.sealed_rows(folder)
        log = RecordedDecisions(folder / row["directory"], state["source_run"], previous["sources"][task]["log_sha256"])
        req = [r for r in log.requests if r["actor_role"] == "worker"][ordinal - 1]
        orig = log.blobs.get(req["request_ref"])
        require(log.run == row["run_id"] and log.start["fresh_session"] and not log.start["source_run_ids"]
                and not log.start["start_snapshot_id"] and not any(e["type"].startswith("memory.") for e in log.events.values())
                and req["request_id"] == state["request_id"] and digest(canonical(orig)) == state["request_sha256"],
                "Unmatched memory-free decision")
        roles[case] = state["role"]
        for form in draft["forms"]:
            handle = digest(canonical(["scoped-note/v1", task, form]))
            normalized = []
            for policy in review.POLICIES:
                n = notes[task,form,policy]
                require(n["source"] == log.run, "Note from a different trajectory")
                item = dict(n["item"], id="m_"+handle[:32], version="v_"+handle[32:])
                wire = cycle.preload_at_decision(orig, cycle.POLICY, notice, item)
                require(wire["messages"][0]["content"] == orig["messages"][0]["content"] + memory_system_suffix(cycle.POLICY, notice)
                        and len(canonical(wire)) <= prior["max_input_bytes"], "Invalid delivery or input size")
                mask = deepcopy(wire)
                (msg,) = read_json(mask["messages"][1]["content"].encode())["items"]
                require(msg == item, "The visible item differs from its immutable source and masked handles")
                msg.update(title="", body="")
                mask["messages"][1]["content"] = canonical(dict(items=[msg])).decode()
                normalized.append(digest(canonical(mask)))
                cells[case,form,policy] = (orig, cycle.POLICY, [item], wire, [log.run,n["writer"]])
                origins[task,form,policy] = dict(task=task, form=form, policy=policy, writer=n["writer"], log=n["log_sha256"],
                                                original_id=n["item"]["id"], original_version=n["item"]["version"],
                                                visible_id=item["id"], visible_version=item["version"])
            require(normalized[0] == normalized[1], "Arms differ beyond title and body")
    # Each wave covers all eight states. Repeat close enough together for both policies to have cache opportunity.
    rng = Random(p["schedule_seed"])
    forms_by_case = {case:rng.sample(draft["forms"], len(draft["forms"])) for case in sorted(roles)}
    slots = []
    repeats = draft["decision_scope"]["repeats_per_cell"]
    for wave in range(len(draft["forms"])):
        first_arms = {case:rng.sample([0,1] * (repeats // 2), repeats) for case in sorted(roles)}
        for rep in range(repeats):
            for case in rng.sample(sorted(roles), len(roles)):
                form = forms_by_case[case][wave]
                order = review.POLICIES if first_arms[case][rep] == 0 else tuple(reversed(review.POLICIES))
                for policy in order:
                    cell = cells[case,form,policy]
                    key = [p["decision_study"],case,form,rep+1,policy]
                    slots.append(dict(slot=len(slots)+1, case=case, task=case.split("/")[0], role=roles[case], form=form,
                        repeat=rep+1, arm=policy, run="r_"+digest(canonical(key))[:32],
                        initial_sha256=digest(canonical(cell[3])), items_sha256=digest(canonical(cell[2]))))
    require(len({s["run"] for s in slots}) == len(slots) == draft["decision_scope"]["conditional_one_step_responses"]
            and set(Counter((s["case"],s["form"],s["arm"]) for s in slots).values()) == {repeats},
            "Invalid paired schedule")
    terms = base["endpoint_terms"]["worker"]
    endpoints = read_json(asset_text(study.ROOT, base["endpoints"]).encode())["models"]
    cfg = dict(name=p["decision_study"], status="ready", expected_canonical_slug=endpoints["worker"]["canonical_slug"],
               max_usd=str(Decimal(p["additional_limit_usd"]) - Decimal(close["total_covered_usd"])), max_seconds=7200,
               timeout_seconds=base["http_timeout_seconds"], max_model_requests=len(slots), query_worker_limit=1,
               max_input_bytes=prior["max_input_bytes"], protocol_sha256=digest((study.ROOT / study.PROFILE).read_bytes()),
               gate_sha256=digest(canonical(gate)), masked_sources=list(origins.values()), writer_spend=close["total_covered_usd"])
    return cfg, slots, cells, terms


def recorded_type(slot, choice):
    return "native_call" if choice["message"].get("tool_calls") else "public_reply"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "run"))
    parser.add_argument("--workspace", type=Path, default=study.ROOT)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    cfg, slots, cells, terms = prepare(workspace)
    if args.phase == "preflight":
        print(f"{len(slots)} slots, {len(cells)} public requests, USD {cfg['max_usd']} available")
        return
    done = fixed_decision.run_slots(workspace, cfg, slots, lambda s: cells[s["case"],s["form"],s["arm"]],
                                   terms, recorded_type, result_origin=study.ORIGIN, journal_prefix="scope")
    print(done["submitted"], done["confirmed_usd"], done["held_usd"], done["stop_reason"])


if __name__ == "__main__":
    main()
