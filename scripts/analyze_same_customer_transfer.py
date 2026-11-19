"""Join masked customer-facing judgments to strictly regraded native episodes and fixed decision wires."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.adapters.tau2 import native_modules, verify_checkout
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import FORMS, write_json
from agent_memory_eval.experiments.return_date import MUTATIONS
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore
from analyze_bank_transfer import message_text
from analyze_frozen_bank_repeats import analyze_registered
from analyze_multi_workflow import transition
from run_frozen_bank_repeats import sealed
from run_same_customer_transfer import PROFILE, prepare_same_customer
from review_fact_recheck_baggage import state_hashes


def masked_id(run):
    return digest(canonical(["same-customer-behavior/v1", run]))[:18]


def masked_case(task, messages):
    first = next(
        (
            (i, c["name"])
            for i, m in enumerate(messages)
            if m["role"] == "assistant"
            for c in m.get("tool_calls") or []
            if c["name"] in MUTATIONS
        ),
        None,
    )
    end = first[0] if task == "22" and first else len(messages)
    speech = [
        {"turn": i, "role": m["role"], "text": message_text(m)}
        for i, m in enumerate(messages[:end])
        if m["role"] in ("assistant", "user") and message_text(m)
    ]
    return {
        "task": task,
        "speech": speech,
        **({"first_mutation": first[1] if first else None} if task == "22" else {}),
    }


def native_packet(workspace, *, closed_shards_only=False):
    cfg = read_json((ROOT / PROFILE).read_bytes())
    folder = workspace / "runs" / cfg["name"]
    seal = None if closed_shards_only else read_json((folder / "completion.json").read_bytes())
    descriptor = (
        read_json((folder / "manifest.json").read_bytes())
        if seal is None
        else sealed(folder, "manifest.json", seal["manifest_sha256"])
    )
    manifest = descriptor["preflight"]
    if manifest["protocol_sha256"] != digest((ROOT / PROFILE).read_bytes()):
        raise ContractError("The masked population differs from the registered protocol")
    cases = {}
    for name, slots in manifest["scheduled"].items():
        shard = folder / name
        if seal is None and not (shard / "completion.json").exists():
            continue
        closure = read_json((shard / "completion.json").read_bytes()) if seal is None else seal["shards"][name]
        allocations = sealed(shard, "allocations.json", closure["allocation_sha256"])
        for row, slot in zip(allocations, slots, strict=True):
            if any(row[k] != v for k, v in slot.items()):
                raise ContractError("A masked allocation label does not match the original schedule")
            if row["status"] == "not_started":
                continue
            result = row["result"]
            ref = result.get("report_ref") or result.get("native_report_ref") or result.get("partial_trajectory_ref")
            data = BlobStore(shard / row["directory"]).get(ref) if ref else []
            messages = data["messages"] if isinstance(data, dict) else data
            key = masked_id(row["run_id"])
            if key in cases:
                raise ContractError("Duplicate masked case")
            cases[key] = masked_case(slot["task_id"], messages)
    return dict(sorted(cases.items()))


def decision_packet(raw):
    texts = {}
    for row in raw["rows"]:
        score = row["score"]
        if score and score["text_review"]:
            texts[score["text_review"]] = score["text"]
    return dict(sorted(texts.items()))


def cited_turn(label, field, case, role):
    citation = label[field]
    if citation is None:
        return None
    turn, quote = citation
    if (
        not isinstance(quote, str)
        or not quote
        or not any(t["turn"] == turn and t["role"] == role and quote in t["text"] for t in case["speech"])
    ):
        raise ContractError("Behavioral review cites a missing speaker, phrase or turn")
    return turn


def inspect_episode(episode, messages, criteria, label, packet_case):
    task = episode["task_id"]
    if packet_case != masked_case(task, messages):
        raise ContractError("The classified transcript differs from the strictly audited one")
    target = "FQ8APE" if task == "22" else "3RK2T9"
    replies = {m["id"]: (i, m) for i, m in enumerate(messages) if m["role"] == "tool"}
    reads, writes, referrals, named = [], [], [], []
    for turn, m in enumerate(messages):
        if m["role"] == "user" and target in message_text(m).upper():
            named.append(turn)
        if m["role"] != "assistant":
            continue
        for call in m.get("tool_calls") or []:
            name, args = call["name"], call["arguments"]
            finish, reply = replies.get(call["id"], (None, {}))
            executed = bool(reply) and not reply.get("error") and reply.get("execution_status") != "rejected"
            rec = dict(
                turn=turn, returned=finish, tool=name, executed=executed, reservation=args.get("reservation_id")
            )
            if name == "get_reservation_details":
                reads.append(rec)
            if name in MUTATIONS:
                matched = any(a["name"] == name and a["arguments"] == args for a in criteria["actions"])
                writes.append(dict(rec, arguments=args, exact_reference=matched))
            if name == "transfer_to_human_agents":
                referrals.append(rec)
    current = [r for r in reads if r["reservation"] == target and r["executed"]]
    old = [r for r in reads if r["reservation"] == "JMO1MG"]
    first_batch = [r["reservation"] for r in reads if r["turn"] == reads[0]["turn"]] if reads else []
    report = dict(
        reads=reads,
        writes=writes,
        referrals=referrals,
        customer_named_current=named,
        first_read_batch=first_batch,
        direct_first_read=first_batch == [target],
        target_in_first_batch=target in first_batch,
        current_read=bool(current),
        old_read_proposed=bool(old),
        old_read_executed=any(r["executed"] for r in old),
        first_current_read_after_disclosure=any(i < current[0]["turn"] for i in named) if current else None,
        wrong_reservation_write=any(r["reservation"] not in (None, target) for r in writes),
        executed_writes=sum(r["executed"] for r in writes),
    )
    if task == "48":
        denial = cited_turn(label, "grounded_denial", packet_case, "assistant")
        promise = cited_turn(label, "unsupported_promise", packet_case, "assistant")
        service = (
            denial is not None and any(r["returned"] < denial for r in current) and not report["executed_writes"]
        )
        report.update(grounded_denial=denial, unsupported_promise=promise, service=service, behavior_pass=service)
    else:
        approval = label["scope"]
        assent = cited_turn(label, "assent", packet_case, "user")
        proposed = cited_turn(label, "offer", packet_case, "assistant")
        if (
            approval not in ("specific", "generic", "none", "no_attempt")
            or (approval == "no_attempt") != (not writes)
            or (approval in ("specific", "generic")) != (assent is not None)
            or (proposed is not None and (assent is None or proposed >= assent))
        ):
            raise ContractError("Approval coding differs from the available first mutation")
        exact = sorted({r["tool"] for r in writes if r["executed"] and r["exact_reference"]})
        report.update(
            approval=approval,
            exact_changes=exact,
            exact_count=len(exact),
            extra_or_failed_writes=sum(not r["exact_reference"] or not r["executed"] for r in writes),
            behavior_pass=len(exact) == 3 and approval == "specific" and not report["wrong_reservation_write"],
        )
    return report


def paired_native(episodes):
    cells = {(r["task_id"], r["writer_block"], r["solver_seed"], r["arm"]): r for r in episodes}
    rows = []
    for r in episodes:
        if r["arm"] not in FORMS:
            continue
        key = r["task_id"], r["writer_block"], r["solver_seed"]
        for control in ("no_memory", "empty_preload"):
            base = cells[*key, control]

            def outcome(e, behavior):
                if behavior and e["official_success"] is not None:
                    return e["behavior"]["behavior_pass"]
                return e["official_success"]

            rows.append(
                dict(
                    task=key[0],
                    writer=key[1],
                    seed=key[2],
                    form=r["arm"],
                    control=control,
                    treatment_id=r["run_id"],
                    control_id=base["run_id"],
                    official=transition(outcome(r, False), outcome(base, False)),
                    behavior=transition(outcome(r, True), outcome(base, True)),
                )
            )
    return rows


def link_original_banks(manifest, native, decisions):
    """Join the actual writer and payload separately from the two edited diagnostic snapshots."""
    from agent_memory_eval.experiments.multi_workflow import arm_id

    target_ids = {t["source_task_id"]: t["task_id"] for t in manifest["tasks"]}
    links = []
    for bank in manifest["banks"]:
        arm = arm_id(bank["task"], bank["arm"], bank["writer"])
        seen = decisions["first_pass_evidence"][arm]
        if (seen["kind"], seen["writer_run"], seen["original_snapshot"], seen["items_sha256"]) != (
            "authentic",
            bank["run"],
            bank["snapshot"],
            bank["items_sha256"],
        ):
            raise ContractError("The checkpoint did not use the original native writer and payload")
        episodes = [
            r
            for r in native["episodes"]
            if r["task_id"] == target_ids[bank["task"]]
            and r["arm"] == bank["arm"]
            and r["writer_block"] == bank["writer"]
        ]
        decisions_for_bank = [r for r in decisions["rows"] if r["arm"] == arm]
        if (
            len(episodes) != 2
            or len(decisions_for_bank) != 4
            or any(r["source_writer_run_id"] != bank["run"] for r in episodes)
        ):
            raise ContractError("Unexpected repeats of the same original bank")
        links.append(
            dict(
                source=bank["task"],
                target=target_ids[bank["task"]],
                form=bank["arm"],
                writer=bank["writer"],
                writer_run=bank["run"],
                snapshot=bank["snapshot"],
                items_sha256=bank["items_sha256"],
                items=bank["item_count"],
                native_indices=[r["index"] for r in episodes],
                decision_slots=[r["slot"] for r in decisions_for_bank],
            )
        )
    return links


def decision_evidence(raw, labels, cfg):
    texts = decision_packet(raw)
    if labels.keys() != texts.keys():
        raise ContractError("Masked query review is incomplete")
    for key, entry in labels.items():
        if (
            entry["label"] not in ("asks_current_identifier", "other")
            or not entry["quote"]
            or entry["quote"] not in texts[key]
        ):
            raise ContractError("Decision citation does not match its original words")
        exclusion = entry["explicitly_excluded_old_quote"]
        if exclusion is not None and (not exclusion or exclusion not in texts[key]):
            raise ContractError("Old-booking exclusion lacks a matching quotation")
    received = {r["slot"]: r for r in raw["rows"]}
    from agent_memory_eval.experiments.same_customer_decisions import schedule

    all_rows, grouped = [], defaultdict(Counter)
    for slot in schedule(cfg):
        row = received.get(slot["slot"], dict(slot, status="not_started", score=None))
        s = row["score"]
        primary = None
        counters = grouped[row["case"], row["form"]]
        counters["planned"] += 1
        if s is not None:
            rule = cfg["cases"][row["case"]]
            review = labels.get(s["text_review"], {})
            question = review.get("label") == "asks_current_identifier"
            primary = question if rule["requires"] == "ask" else s["direct"]
            allowed = cfg["anchors"][rule["source"]]["candidates"]
            counters.update(
                scored=1,
                primary=primary,
                direct=s["direct"],
                target_in_batch=s["target_in_batch"],
                old_in_batch=s["old_in_batch"],
                reads=len(s["requested_ids"]),
                writes=len(s["writes"]),
                outside_profile=sum(i not in allowed for i in s["requested_ids"]),
                asks_current=question,
                explicitly_excluded_old=bool(review.get("explicitly_excluded_old_quote")),
            )
        all_rows.append(dict(row, primary=primary))
    cells = {(r["case"], r["repeat"], r["form"], r["writer"]): r for r in all_rows}
    pairs = []
    for r in all_rows:
        if r["form"] in ("empty", "unbound"):
            continue
        control, writer = ("unbound", 0) if r["form"] == "bound" else ("empty", r["writer"])
        base = cells[r["case"], r["repeat"], control, writer]
        pairs.append(
            dict(
                case=r["case"],
                repeat=r["repeat"],
                form=r["form"],
                control=control,
                writer=writer,
                treatment_slot=r["slot"],
                control_slot=base["slot"],
                outcome=transition(r["primary"], base["primary"]),
            )
        )
    return dict(
        raw,
        rows=all_rows,
        counts={
            case: {form: dict(count) for (key, form), count in grouped.items() if key == case}
            for case in cfg["cases"]
        },
        paired=pairs,
    )


def replay_paid_bags(workspace, native, state):
    """Post hoc field intervention in the completed 22 trajectories, holding every other proposed argument fixed."""
    checkout = verify_checkout(workspace / "data/tau2-bench")
    task = state["tasks"]["22"]
    if task.get("initial_state") is not None:
        raise ContractError("The luggage replay assumes the default recorded initial environment")
    rows = []
    with (
        patch("socket.socket.connect", side_effect=AssertionError("Network during bag replay")) as tcp,
        patch("socket.socket.connect_ex", side_effect=AssertionError("Network during bag replay")) as tcp_ex,
    ):
        native_modules(checkout)
        from tau2.data_model.simulation import SimulationRun
        from tau2.multilingual.runtime import language_environment_kwargs
        from tau2.registry import registry
        from tau2.runner.presets import reload_recorded_call
        from loguru import logger

        logger.disable("tau2")
        make = registry.get_env_constructor("airline")
        for e in native["episodes"]:
            if e["task_id"] != "22" or "grade" not in e:
                continue
            sha = e["grade"]["report_sha256"]
            sim = SimulationRun.model_validate(
                read_json(asset_text(workspace, dict(path=e["report_path"], sha256=sha)).encode())
            )
            options = language_environment_kwargs(reload_recorded_call(sim.language_context, "airline")) or {}
            strict, original, edited, reference = (make(**options) for _ in range(4))
            for a in task["evaluation_criteria"]["actions"]:
                reference.make_tool_call(a["name"], requestor=a.get("requestor", "assistant"), **a["arguments"])
            strict.set_state(
                initialization_data=None, initialization_actions=None, message_history=sim.messages, strict=True
            )
            expected = state_hashes(reference)
            observed = state_hashes(strict)
            if (observed == expected) != e["grade"]["db_match"]:
                raise ContractError("Original DB is inconsistent with the official regrade")
            row = dict(
                index=e["index"],
                report_sha256=sha,
                before=observed == expected,
                original_hashes=list(observed),
                reference_hashes=list(expected),
                after=None,
            )
            rows.append(row)
            returns = {m.id: m for m in sim.messages if m.role == "tool"}
            calls = [
                c
                for m in sim.messages
                for c in getattr(m, "tool_calls", None) or []
                if c.name in MUTATIONS and c.id in returns and returns[c.id].execution_status != "rejected"
            ]
            bags = [c for c in calls if c.name == "update_reservation_baggages" and not returns[c.id].error]
            if (
                len(bags) != 1
                or bags[0].arguments.get("total_baggages") != 3
                or bags[0].arguments.get("nonfree_baggages") not in (0, 1)
            ):
                continue
            bag = bags[0]
            old = bag.arguments["nonfree_baggages"]
            for c in calls:
                modified = (
                    c.model_copy(update={"arguments": dict(c.arguments, nonfree_baggages=1 - old)})
                    if c.id == bag.id
                    else c
                )
                if (
                    original.get_response(c).error != returns[c.id].error
                    or edited.get_response(modified).error != returns[c.id].error
                ):
                    raise ContractError("A single field intervention changed an execution error")
            alternative = state_hashes(edited)
            if state_hashes(original) != observed:
                raise ContractError("Original write sequence differs from the strict trajectory")
            row.update(old=old, new=1 - old, edited_hashes=list(alternative), after=alternative == expected)
        if tcp.call_count or tcp_ex.call_count:
            raise ContractError("Network attempted during field replay")
    return rows


def combine(workspace, native, decisions, native_labels, decision_labels):
    from agent_memory_eval.experiments.same_customer_decisions import PROFILE as decision_profile

    cfg = read_json((ROOT / decision_profile).read_bytes())
    packet = native_packet(workspace)
    if packet.keys() != native_labels.keys():
        raise ContractError("Masked full-episode review is incomplete")
    manifest, state = prepare_same_customer(workspace)
    if (
        native["protocol_sha256"] != manifest["protocol_sha256"]
        or native["preflight_sha256"] != digest(canonical(manifest))
        or decisions["study"] != cfg["study"]
    ):
        raise ContractError("Different source runs supplied to the analysis")
    for episode in native["episodes"]:
        if episode["status"] == "not_started":
            continue
        path = episode.get("report_path") or episode.get("partial_path")
        if path:
            sha = episode["grade"]["report_sha256"] if "grade" in episode else Path(path).name
            data = read_json(asset_text(workspace, dict(path=path, sha256=sha)).encode())
            messages = data["messages"] if isinstance(data, dict) else data
        else:
            messages = []
        key = masked_id(episode["run_id"])
        episode["behavior"] = inspect_episode(
            episode,
            messages,
            state["tasks"][episode["task_id"]]["evaluation_criteria"],
            native_labels[key],
            packet[key],
        )
        episode["behavior_review_id"] = key
    decisions = decision_evidence(decisions, decision_labels, cfg)
    known = Decimal(native["new_known_usd"]) + Decimal(decisions["reported_usd"])
    reserved = Decimal(native["reserved_usd"]) + Decimal(decisions["reserved_usd"])
    return dict(
        native=native,
        native_pairs=paired_native(native["episodes"]),
        decisions=decisions,
        original_bank_links=link_original_banks(manifest, native, decisions),
        exploratory_paid_bags_db_replay=replay_paid_bags(workspace, native, state),
        known_usd=str(known),
        reserved_usd=str(reserved),
        masked_native_sha256=digest(canonical(packet)),
        native_labels_sha256=digest(canonical(native_labels)),
        decision_labels_sha256=digest(canonical(decision_labels)),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["native", "mask-native", "mask-decisions", "join"])
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--native", type=Path)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--native-labels", type=Path)
    parser.add_argument("--decision-labels", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if args.output.exists():
        parser.error("Choose a new output")
    if args.mode == "native":
        manifest, state = prepare_same_customer(workspace)
        result = analyze_registered(workspace, manifest, state, transfer=True)
    elif args.mode == "mask-native":
        result = native_packet(workspace)
    elif args.mode == "mask-decisions":
        result = decision_packet(read_json(args.decisions.read_bytes()))
    else:
        result = combine(
            workspace,
            *(
                read_json(p.read_bytes())
                for p in (args.native, args.decisions, args.native_labels, args.decision_labels)
            ),
        )
    write_json(args.output, result)
