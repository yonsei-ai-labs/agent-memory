"""Find pre-inspection retail decisions and check which observations the archive can compare."""

import argparse
from collections import Counter
from copy import deepcopy
from pathlib import Path
import re

from agent_memory_eval.contracts.core import MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.memory.context import PAST_MEMORY_NOTICE, request_with_past_memory
from agent_memory_eval.recording.assets import asset_text, publish_json as publish
from agent_memory_eval.recording.journal import safe_path
from scripts.analyze_full_retail import require
from scripts.analyze_recheck_rounds import MUTATIONS
from scripts.audit_retail_transfer_writing import IDENTITY_TOOLS, role_calls

PROFILE = "protocols/diagnostics/tau2-retail-order-choice-v1.json"
ORDER_NUMBER = re.compile(r"(?<![A-Za-z0-9])#?W?(\d{7})(?![A-Za-z0-9])", re.I)


def order_mentions(text, orders):
    found = {"#W" + match for match in ORDER_NUMBER.findall(text)}
    return sorted(found & set(orders))


def archived_banks(root, donors):
    banks = {}
    for source, donor in donors.items():
        for arm, seal in donor["banks"].items():
            folder = safe_path(root, seal["journal"]).parent
            bank = read_form_bank(folder, expected_log_sha256=seal["journal_sha256"])
            require(bank["run_id"] == seal["run_id"] and bank["snapshot_id"] == seal["snapshot_id"]
                    and bank["task_id"] == source and bank["arm"] == arm
                    and bank["public_context_sha256"] == donor["public_context_sha256"]
                    and digest(canonical(bank["items"])) == seal["items_sha256"], "Wrong source bank")
            banks[source, arm] = bank["items"]
    return banks


def without_treatment(wire, arm, items, instruction_sha):
    """Invert only the archived renderer. Preserve every other public byte and parameter."""
    system = wire["messages"][0]
    require(system["role"] == "system" and not any(
        t["function"]["name"].startswith("memory_") for t in wire["tools"]
    ), "Unexpected business interface")
    base, marker, suffix = system["content"].partition("\n<memory_policy>\n")
    if arm == "no_memory":
        require(not marker and not items, "Pure baseline contains a memory policy")
        return wire
    require(marker and suffix.endswith("\n</memory_policy>"), "Missing preload policy")
    text, policy_text = suffix.removesuffix("\n</memory_policy>").rsplit("\n", 1)
    instruction = text.removeprefix(PAST_MEMORY_NOTICE + "\n")
    require(digest(instruction.encode()) == instruction_sha, "Different historical notice")
    policy = MemoryPolicy(**read_json(policy_text.encode()))
    original = dict(wire, messages=[dict(system, content=base), *wire["messages"][2:]])
    require(request_with_past_memory(original, policy, items, instruction=instruction) == wire,
            "Historical snapshot or renderer differs")
    return original


def profile_state(wire, customer):
    """Use only the current request. Authenticate with a successful lookup before the profile."""
    messages = wire["messages"]
    if messages[-1]["role"] != "tool":
        return None
    calls = {c["id"]: c["function"] for m in messages for c in m.get("tool_calls") or []}
    if calls[messages[-1]["tool_call_id"]]["name"] != "get_user_details":
        return None
    if any(c["name"] in MUTATIONS or c["name"] == "get_order_details" for c in calls.values()):
        return None
    try:
        profile = read_json(messages[-1]["content"].encode())
    except ValueError:
        return None
    if not isinstance(profile, dict) or "orders" not in profile:
        return None
    owner = profile["user_id"]
    require(digest(owner.encode()) == customer, "Profile belongs to another customer")
    profile_args = read_json(calls[messages[-1]["tool_call_id"]]["arguments"].encode())
    require(profile_args.get("user_id") == owner, "Wrong profile lookup")
    # A parallel unauthenticated profile lookup is not a post-authentication state.
    profile_call_id = messages[-1]["tool_call_id"]
    issued = next(i for i, m in enumerate(messages)
                  if any(c["id"] == profile_call_id for c in m.get("tool_calls") or []))
    if not any(m["role"] == "tool" and calls[m["tool_call_id"]]["name"] in IDENTITY_TOOLS
               and m["content"] == owner for m in messages[:issued]):
        return None
    return profile


def first_state(root, cell, items, instruction_sha):
    for _, wire, choice, response_sha in role_calls(root, cell, "worker"):
        base = without_treatment(wire, cell["arm"], items, instruction_sha)
        profile = profile_state(base, cell["customer"])
        if profile is not None:
            return base, profile, choice, response_sha, digest(canonical(wire))
    return None


def comparable_hash(wire):
    """Secondary comparison permits only a bijective renaming of tool call/result IDs."""
    normalized = deepcopy(wire)
    ids = {}
    for message in normalized["messages"]:
        for call in message.get("tool_calls") or []:
            original = call["id"]
            require(original not in ids, "Duplicate tool call ID in a public prefix")
            ids[original] = call["id"] = f"call_{len(ids)}"
        if message["role"] == "tool":
            message["tool_call_id"] = ids[message["tool_call_id"]]
    return digest(canonical(normalized))


def current_order(review, base, profile, db):
    public = "\n".join(m["content"] for m in base["messages"] if m["role"] == "user")
    require(review["quote"].casefold() in public.casefold(), "Customer evidence changed")
    orders = profile["orders"]
    require(orders == db["users"][profile["user_id"]]["orders"]
            and all(db["orders"][o]["user_id"] == profile["user_id"] for o in orders),
            "Archived profile differs from the pinned catalog")
    if "exclude" in review:
        if review["exclude"] == "single_order_profile":
            require(len(orders) == 1, "Single-order exclusion no longer applies")
        return None
    products = set(review["products"])
    matches = [o for o in orders if products <= {i["name"] for i in db["orders"][o]["items"]}]
    require(len(matches) == 1 and len(orders) > 1 and not order_mentions(public, orders),
            "Ambiguous, explicit or already resolved target")
    return matches[0]


def classify_reply(choice, target):
    if choice is None or choice.get("finish_reason") not in {"stop", "tool_calls"}:
        return "unobserved"
    calls = [c["function"] for c in choice["message"].get("tool_calls") or []]
    if any(c["name"] in MUTATIONS for c in calls):
        return "mutation_requested"
    lookups = []
    for call in calls:
        if call["name"] == "get_order_details":
            try:
                args = read_json(call["arguments"].encode())
            except ValueError:
                return "invalid_lookup"
            if not isinstance(args, dict) or not isinstance(args.get("order_id"), str):
                return "invalid_lookup"
            lookups.append(args["order_id"])
    if lookups == [target] and len(calls) == 1:
        return "focused_target"
    if target in lookups:
        return "includes_target_with_other_calls"
    if lookups:
        return "other_order_only"
    return "other_tool" if calls else "customer_reply"


def note_label(items, orders, target):
    if not items:
        return "writer_empty", []
    text = "\n".join(i["title"] + "\n" + i["body"] for i in items)
    mentioned = order_mentions(text, orders)
    if target in mentioned:
        return ("target_only" if mentioned == [target] else "target_and_others"), mentioned
    return ("other_only" if mentioned else "no_owned_id"), mentioned


def audit(root, cfg, db):
    plan, results = [read_json(asset_text(root, cfg["inputs"][k]).encode()) for k in ("transfer", "runs")]
    require(plan["benchmark_revision"] == cfg["benchmark_revision"], "Wrong retail benchmark")
    targets = {r["task"]: r for r in plan["targets"]}
    require(set(targets) == set(cfg["task_review"]), "Every registered target needs a decision")
    banks = archived_banks(root, plan["donors"])
    decisions, notes, comparisons = [], [], []
    support = Counter()
    for tid in sorted(targets, key=int):
        cohort = sorted((c for c in results["cells"] if c["task"] == tid),
                        key=lambda c: (c["stage"], c["run"]))
        state = None
        for baseline in (c for c in cohort if c["arm"] == "no_memory"):
            state = first_state(root, baseline, [], plan["instruction_sha256"])
            if state is not None:
                break
        if state is None:
            decisions.append(dict(task=tid, excluded="no_eligible_prefix"))
            continue
        base, profile, choice, response_sha, wire_sha = state
        review = cfg["task_review"][tid]
        target = current_order(review, base, profile, db)
        row = dict(task=tid, customer=baseline["customer"], run=baseline["run"],
                   stage=baseline["stage"], journal=baseline["journal"], wire_sha256=wire_sha,
                   profile_orders=profile["orders"], current_quote=review["quote"])
        decisions.append(row)
        if target is None:
            row["excluded"] = review["exclude"]
            continue
        row.update(target_order=target, response_sha256=response_sha,
                   observed_next=classify_reply(choice, target))
        canonical_id = comparable_hash(base)
        for source in targets[tid]["pairs"]:
            for arm, descriptor in plan["donors"][source]["banks"].items():
                group, mentions = note_label(banks[source, arm], profile["orders"], target)
                notes.append(dict(task=tid, source=source, arm=arm, items_sha256=descriptor["items_sha256"],
                                  group=group, mentioned_orders=mentions))
        for cell in cohort:
            if cell["run"] == baseline["run"]:
                continue
            arm = cell["arm"]
            items = [] if arm in {"empty_preload", "no_memory"} else banks[cell["source"], arm]
            candidate = first_state(root, cell, items, plan["instruction_sha256"])
            support["examined"] += 1
            if candidate is None:
                continue
            support["has_profile_state"] += 1
            stripped, _, reply, reply_sha, candidate_wire = candidate
            exact = stripped == base
            if not exact and comparable_hash(stripped) != canonical_id:
                continue
            comparisons.append(dict(task=tid, baseline_run=baseline["run"], run=cell["run"],
                                    arm=arm, source=cell["source"], exact=exact, items=len(items),
                                    wire_sha256=candidate_wire, response_sha256=reply_sha,
                                    observed_next=classify_reply(reply, target)))
    eligible = [r for r in decisions if "target_order" in r]
    return dict(decisions=decisions, notes=notes, matching_runs=comparisons, summary=dict(
        targets=len(decisions), multi_order_profiles=sum(len(r.get("profile_orders", [])) > 1 for r in decisions),
        eligible=len(eligible), customers=len({r["customer"] for r in eligible}),
        source_target_pairs=len({(n["source"], n["task"]) for n in notes}),
        note_assignments=dict(sorted(Counter(n["group"] for n in notes).items())),
        neutral_responses=dict(sorted(Counter(r["observed_next"] for r in eligible).items())),
        archive_support=dict(support), byte_identical_after_treatment_removal=sum(r["exact"] for r in comparisons),
        identical_allowing_call_id_renaming=len(comparisons)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cfg = read_json((ROOT / PROFILE).read_bytes())
    db = read_json(asset_text(ROOT, cfg["inputs"]["db"]).encode())
    result = audit(ROOT, cfg, db)
    publish(args.output, dict(protocol_sha256=digest((ROOT / PROFILE).read_bytes()),
                              model_requests_sent=0, **result))
    print(result["summary"])
