"""Compare paid initial requests with the pure interface and four sealed source writers, without reading choices."""

import argparse
from collections import Counter
from copy import deepcopy
from functools import cache
from pathlib import Path

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.memory.context import memory_system_suffix, snapshot_message
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from scripts.analyze_full_retail import require
from scripts.audit_retail_order_choice import publish
from scripts.retail_two_visit_episode import bridge_instruction, policy_for
from scripts.run_retail_two_visit import read, read_receipt, sealed_bank


def audit(folder, prefixfile, *, pure_folder=None):
    reg = read(folder / "registration.json")
    design = reg["design"]
    public = read(prefixfile)
    require(
        digest(canonical(public)) == reg["preflight"]["public_prefixes_sha256"], "Frozen public states changed"
    )
    replay = read_json(asset_text(ROOT, design["replay"]).encode())
    instructions = {a: asset_text(ROOT, replay[a + "_instruction"]) for a in ("query", "preload")}
    jobs = reg["jobs"]
    if reg.get("format") == "retail-derived-two-visit/recovery-v1":
        from scripts.run_retail_two_visit_recovery import verified_replacements

        require(pure_folder is not None, "Recovery needs the original pure control")
        verified_replacements(pure_folder, folder)
        jobs = {**jobs, **read(folder / "upstreams.json")["selected"]}
    else:
        require(pure_folder is None, "The original run has its own pure control")
        pure_folder = folder
    writers = {(r["task"], r["arm"]): r for r in jobs["writer"]}

    @cache
    def bank(source):
        lineage, items = [], []
        for f in design["forms"]:
            w = writers[source, f]
            p = folder / w["run"]
            r = read_receipt(p, "writer")
            items.extend(sealed_bank(p, r, w["run"]))
            lineage.append(w["run"])
        return lineage, items

    def first(path):
        scan, _, _ = read_experiment(path)
        ev = list(scan["events"].values())
        start = next(e for e in ev if e["type"] == "model.request_started")
        return scan, ev, BlobStore(path).get(start["payload"]["request_ref"])

    base = None
    for j in read(pure_folder / "registration.json")["jobs"]["decision"]:
        if (
            j["arm"] == "no_memory"
            and (pure_folder / j["run"] / "decision.json").exists()
            and read(pure_folder / j["run"] / "decision.json")["status"] in {"checkpoint", "decision"}
        ):
            _, _, base = first(pure_folder / j["run"])
            break
    require(base is not None, "No pure request yet")
    sessions = set()
    counts = Counter()
    current_inputs = {}
    for j in jobs["decision"]:
        arm = j["arm"]
        path = folder / j["run"]
        receipt = read_receipt(path, "decision")
        if not receipt or receipt["status"] not in {"checkpoint", "decision"}:
            continue
        scan, ev, raw = first(path)
        start = scan["starts"][j["run"]]
        require(
            start["session_id"] not in sessions and start["payload"]["fresh_session"], "Non-fresh target session"
        )
        sessions.add(start["session_id"])
        policy = policy_for(arm)
        sources, items = bank(j["source"]) if arm in {"query", "preload"} else ([], [])
        require(start["payload"]["source_run_ids"] == sources, "Wrong source binding")
        expected = deepcopy(base)
        expected["messages"] = deepcopy(base["messages"][:1])
        if policy.enabled:
            expected["messages"][0]["content"] += memory_system_suffix(
                policy, bridge_instruction(arm, instructions)
            )
        if policy.preloads:
            expected["messages"].append(snapshot_message(items))
        expected["messages"].extend(public[j["prefix"]])
        expected["tools"] += memory_tools(policy.tools("solve"))
        require(expected == raw, "Different private or visible first request")
        mark = next(e for e in ev if e["payload"].get("origin") == "revisit.first_decision")
        require(
            {
                e["payload"]["actor_role"]
                for e in ev
                if e["type"] == "model.request_started" and e["event_seq"] < mark["event_seq"]
            }
            == {"worker"},
            "Simulator called before decision",
        )
        counts[arm] += 1
        if arm in {"query", "empty_query", "empty_preload", "no_memory"}:
            interface = "query" if arm in {"query", "empty_query"} else arm
            h = digest(canonical(raw))
            require(current_inputs.setdefault((interface, j["prefix"]), h) == h, "Revealed hidden history or bank")
    return {
        "study": design["name"],
        "registered": len(jobs["decision"]),
        "verified_by_arm": dict(counts),
        "different_sessions": len(sessions),
        "merged_source_banks_checked": bank.cache_info().currsize,
        "model_requests_for_audit": 0,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--public", type=Path, required=True)
    p.add_argument("--output", type=Path)
    p.add_argument("--pure-run", type=Path, help="Original complete run for a separately authorized supplement")
    a = p.parse_args()
    v = audit(a.run, a.public, pure_folder=a.pure_run)
    if a.output:
        publish(a.output, v)
    print(v)
