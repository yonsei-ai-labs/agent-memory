"""Offline, retrospective diagnosis of the sealed banking writer comparison."""

import argparse
from collections import defaultdict
from pathlib import Path
import re

from agent_memory_eval.analysis.action_grounding import input_evidence
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.experiments.forms_write import read_form_bank
from scripts import analyze_banking_policy_cycle as study
from scripts import run_banking_policy_cycle as cycle
from scripts.analyze_banking_memory import document_ids

# Analyst-selected document witnesses in the published new tasks; never worker inputs.
DOCS = {
    "task_002": {
        "platinum_rate": "doc_credit_cards_platinum_rewards_card_002",
        "platinum_rebate": "doc_credit_cards_platinum_rewards_card_007",
    },
    "task_014": {"transfer_codes": "doc_bank_accounts_bank_accounts_(general)_042"},
    "task_016": {"silver_referral": "doc_credit_cards_silver_rewards_card_011"},
    "task_017": {
        "dispute": "doc_credit_cards_credit_cards_(general)_003",
        "silver_base": "doc_credit_cards_silver_rewards_card_001",
        "silver_bonus": "doc_credit_cards_silver_rewards_card_002",
    },
}
ARGUMENTS = {
    "transfer_to_human_agents": "reason",
    "apply_for_credit_card": "card_type",
    "give_discoverable_user_tool": "discoverable_tool_name",
    "call_discoverable_user_tool": "discoverable_tool_name",
    "submit_transaction": "amount",
}
STEM = "artifacts/analyses/20261125-banking-policy-cycle-"


def rank_hits(ranked, witnesses):
    return {key: ranked.index(doc) + 1 for key, doc in witnesses.items() if doc in ranked}


def query_replayer(workspace, corpus_hash):
    from agent_memory_eval.adapters.tau_banking_results import native_grep
    cycle.bank.matched.modules_for(workspace)
    native, documents, top_k = native_grep(corpus_hash)
    state = native.state
    alternate = dict(
        state,
        doc_content_map={
            i: state["doc_title_map"][i] + "\n" + content for i, content in state["doc_content_map"].items()
        },
    )
    (retriever,) = native.retrievers

    def replay(pattern, returned, witnesses):
        native_ids = [i for i, _ in native.retrieve(pattern)]
        require(native_ids == returned, "Native grep no longer reproduces the observed ordered hits")
        amended = [i for i, _ in retriever.retrieve({"query": pattern}, alternate)]
        return dict(
            returned=len(returned),
            ranking_sha256=digest(canonical(returned)),
            observed=rank_hits(returned, witnesses),
            with_titles=rank_hits(amended, witnesses),
        )

    return replay, documents, top_k


def wire_evidence(log):
    calls, shown = {}, {}
    first = last_text = None
    for req in log.requests:
        response = log.reply(req)["message"]
        for call in response.get("tool_calls") or []:
            require(call["id"] not in calls, "Repeated call identifier")
            calls[call["id"]] = req
        if req["actor_role"] != "worker":
            continue
        wire = log.blobs.get(req["request_ref"])
        if first is None:
            first = req
        if response.get("content"):
            last_text = req
        names = {c["id"]: c["function"]["name"] for m in wire["messages"] for c in m.get("tool_calls") or []}
        for m in wire["messages"]:
            cid = m.get("tool_call_id")
            if m["role"] == "tool" and names.get(cid) == "grep" and cid not in shown:
                shown[cid] = (req["request_id"], digest(m["content"].encode()))
    return calls, shown, first, last_text


def inner_args(args):
    value = args.get("arguments")
    if not isinstance(value, str):
        return None
    try:
        value = read_json(value.encode())
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def compare_dispute_calls(call, checks):
    """Audit the native matcher, then vary only the extra field or the encoding of the inner JSON."""
    from tau2.data_model.message import ToolCall
    from tau2.data_model.tasks import Action

    tool = ToolCall.model_validate(call)
    result = []
    for expected in checks:
        target = Action.model_validate(expected)
        original = target.compare_with_tool_call(tool)
        args = dict(tool.arguments)
        if tool.name == "give_discoverable_user_tool" and set(args) - set(target.arguments) == {"arguments"}:
            del args["arguments"]
            edit = "omit_prefilled_arguments"
        elif inner_args(args) is not None and inner_args(args) == inner_args(target.arguments):
            args["arguments"] = target.arguments["arguments"]
            edit = "use_expected_serialization_of_same_inner_values"
        else:
            continue
        result.append(
            dict(
                action=target.action_id,
                original=original,
                edit=edit,
                edited=target.compare_with_tool_call(tool.model_copy(update={"arguments": args})),
            )
        )
    return result


def selected_action(call, reply, gold, request, log, witnesses):
    name, args = call["name"], call["arguments"]
    checks = [c for c in gold if c["name"] == name and c["requestor"] == call["requestor"]]
    item = dict(
        tool=name,
        actor=call["requestor"],
        argument=ARGUMENTS[name],
        value=args.get(ARGUMENTS[name]),
        request=request["request_id"],
        response_sha256=log.finishes[request["request_id"]]["response_ref"]["sha256"],
        executed=reply["execution_status"] == "executed" and not reply["error"],
        exact_gold_args=[c["action_id"] for c in checks if c["arguments"] == args],
    )
    if name == "transfer_to_human_agents":
        wire = log.blobs.get(request["request_ref"])
        docs = document_ids(input_evidence(wire["messages"], wire.get("tools", []), {"grep"}))
        item["docs_before_call"] = sorted(k for k, v in witnesses.items() if v in docs)
        item["expected_reasons_in_schema"] = [
            c["arguments"]["reason"]
            for c in checks
            if c["arguments"]["reason"] in canonical(wire["tools"]).decode()
        ]
    if name in {"give_discoverable_user_tool", "call_discoverable_user_tool"}:
        item["extra_argument_keys_vs_target"] = (
            sorted(set(args) - set(checks[0]["arguments"])) if checks else []
        )
        nested = inner_args(args)
        item["decoded_gold_args"] = [
            c["action_id"]
            for c in checks
            if nested is not None
            and args.get("discoverable_tool_name") == c["arguments"].get("discoverable_tool_name")
            and nested == inner_args(c["arguments"])
        ]
        item["return_line"] = reply["content"].splitlines()[0]
        item["native_comparison_probe"] = compare_dispute_calls(call, checks)
    return item


def inspect_episode(folder, row, task, reference, replay):
    log = RecordedDecisions(folder / row["directory"], row["run_id"], row["result"]["log_sha256"])
    (score,) = log.of_type("outcome.scored")
    report_ref = score["payload"]["official_artifact_ref"]
    report = log.blobs.get(report_ref)
    require(
        (log.run, row["result"]["log_sha256"], score["payload"]["official_success"])
        == (reference["run"], reference["log_sha256"], reference["success"]),
        "Changed episode",
    )
    native = report["messages"]
    returns = {m["id"]: m for m in native if m["role"] == "tool"}
    by_id, shown, first, last_text = wire_evidence(log)
    gold = [c["action"] for c in report["reward_info"]["action_checks"]]
    queries, actions = [], []
    for message in native:
        for call in message.get("tool_calls") or []:
            name, cid = call["name"], call["id"]
            if name != "grep" and name not in ARGUMENTS:
                continue
            req = by_id[cid]
            response = log.reply(req)["message"]
            (wire_call,) = [c for c in response["tool_calls"] if c["id"] == cid]
            require(
                wire_call["function"]["name"] == name
                and read_json(wire_call["function"]["arguments"].encode()) == call["arguments"],
                "Native and recorded response disagree",
            )
            ret = returns[cid]
            if name in ARGUMENTS:
                actions.append(selected_action(call, ret, gold, req, log, DOCS[task]))
            else:
                first_seen, text_sha = shown[cid]
                require(
                    digest(ret["content"].encode()) == text_sha, "Search result differs in the worker's input"
                )
                ids = re.findall(r"^\s*ID: ([^\s]+)\s*$", ret["content"], re.M)
                pattern = call["arguments"]["pattern"]
                queries.append(
                    dict(
                        pattern=pattern,
                        request=req["request_id"],
                        seen_in=first_seen,
                        **replay(pattern, ids, DOCS[task]),
                    )
                )
    require(len(shown) == len(queries) == reference["calls"].get("grep", 0), "Dropped search")

    def text_ref(req, include_excerpt=False):
        ref = dict(
            request=req["request_id"],
            response_sha256=log.finishes[req["request_id"]]["response_ref"]["sha256"],
        )
        if include_excerpt:
            ref["excerpt"] = (log.reply(req)["message"].get("content") or "")[:520]
            wire = log.blobs.get(req["request_ref"])
            docs = document_ids(input_evidence(wire["messages"], wire.get("tools", []), {"grep"}))
            ref["docs_visible"] = sorted(k for k, v in DOCS[task].items() if v in docs)
        return ref

    first_message = log.reply(first)["message"]
    first_text = first_message.get("content") or ""
    past_as_current = "your case was transferred" in first_text.lower() and "IN_PROGRESS" in first_text
    fields = {name: sorted({k for q in queries for k in q[name]}) for name in ("observed", "with_titles")}
    require(
        {DOCS[task][k] for k in fields["observed"]}
        == set(reference["tagged_documents_seen"]) & set(DOCS[task].values()),
        "Observed decision documents do not match the earlier audit",
    )
    return dict(
        run=log.run,
        journal_sha256=row["result"]["log_sha256"],
        native_report=report_ref["sha256"],
        success=reference["success"],
        first_worker=text_ref(first, past_as_current),
        first_actions=reference["first_business_actions"],
        first_says_prior_transfer_and_status=past_as_current,
        last_worker_text=text_ref(last_text, task == "task_002") if last_text else None,
        native_checks=reference["native_action_checks"],
        calls=reference["calls"],
        **fields,
        searches=queries,
        actions=actions,
    )


def inspect_writer(folder, row, task, reference):
    recorded = folder / row["directory"]
    log = RecordedDecisions(recorded, row["run_id"], row["result"]["log_sha256"])
    bank = read_form_bank(recorded, expected_log_sha256=reference["writer_log_sha256"])
    require(bank == reference["bank"], "The compared note differs from the stored bank")
    first = next(q for q in log.requests if q["actor_role"] == "writer")
    wire = log.blobs.get(first["request_ref"])
    full_context = wire["messages"][1]["content"]
    feedback = read_json(wire["messages"][2]["content"].encode())
    require(
        row["result"]["log_sha256"] == reference["writer_log_sha256"]
        and log.run == bank["run_id"]
        and digest(full_context.encode()) == bank["public_context_sha256"]
        and feedback["level"] == "result_only"
        and feedback["success"] is bank["result_feedback"]["success"],
        "Writer input or feedback differs",
    )
    experience = read_json(full_context.encode())
    public = experience["messages"]
    public_calls = sorted({c["function"]["name"] for m in public for c in m.get("tool_calls") or []})
    public_docs = document_ids(input_evidence([experience["task_context"], *public], [], {"grep"}))
    (item,) = bank["items"]
    body = item["body"]
    marker = body.lower().find("consequential decision")
    clause = None
    if marker >= 0:
        excerpt = re.split(r"(?<=[.!?])(?=\s|$)|\n", body[marker:], maxsplit=1)[0]
        clause = dict(start=marker, end=marker + len(excerpt), quote=excerpt)
    return dict(
        run=log.run,
        journal_sha256=reference["writer_log_sha256"],
        source=bank["source_run_id"],
        public_context_sha256=bank["public_context_sha256"],
        feedback=feedback["success"],
        public_tools=public_calls,
        public_docs=sorted(k for k, v in DOCS[task].items() if v in public_docs),
        item=item["id"],
        version=item["version"],
        body_words=len(body.split()),
        marked_clause=clause,
        mentions_platinum="platinum" in body.lower(),
        contains_literal_750="750" in body,
        contains_dispute_tool="submit_cash_back_dispute_0589" in body,
        contains_correct_transfer_code="unconfirmed_external_communication" in body,
    )


def summarize(episodes, writers, dev):
    summary = {}
    for task in DOCS:
        summary[task] = {}
        for group in ("source", "existing", "candidate"):
            rows = [x for x in episodes if x["task"] == task and x["policy"] == group]
            notes = [x for x in writers if x["task"] == task and x["policy"] == group]
            summary[task][group] = dict(
                episodes=len(rows),
                success=sum(x["success"] for x in rows),
                searches=sum(len(x["searches"]) for x in rows),
                docs={
                    k: {mode: sum(k in x[mode] for x in rows) for mode in ("observed", "with_titles")}
                    for k in DOCS[task]
                },
                notes=len(notes),
                marked_transfer_and_success=sum(
                    all(
                        term in (x["marked_clause"] or {}).get("quote", "").lower()
                        for term in ("transfer", "success")
                    )
                    for x in notes
                ),
                notes_mention_platinum=sum(x["mentions_platinum"] for x in notes),
            )
    indexed = {(x["task"], x["policy"], x["form"]): x for x in episodes}
    pairs = []
    for task in DOCS:
        for form in cycle.bank.matched.FORMS:
            a, b = [indexed[task, p, form] for p in ("existing", "candidate")]
            change = "same"
            if a["first_actions"] != b["first_actions"]:
                same_names = [v["name"] for v in a["first_actions"]] == [
                    v["name"] for v in b["first_actions"]
                ]
                change = "arguments" if same_names else "function_or_call_vs_text"
            pairs.append(
                dict(task=task, form=form, existing=a["success"], candidate=b["success"], first_change=change)
            )
    cells = defaultdict(list)
    for row in dev["rows"]:
        cells[row["initial_sha256"]].append(row["behavior"] == "target_action")
    require(
        all(len(xs) == 4 for xs in cells.values()) and len(cells) == 24, "Development repeat count changed"
    )
    return dict(
        by_task=summary,
        pairs=pairs,
        dev_wires=len(cells),
        dev_mixed_target=sum(len(set(xs)) > 1 for xs in cells.values()),
    )


def diagnose(workspace):
    spec, _, _, _, _ = cycle.design(workspace)
    primary_path, dev_path = [
        cycle.ROOT / (STEM + suffix + ".json") for suffix in ("validation", "development")
    ]
    primary, dev = [cycle.read(p) for p in (primary_path, dev_path)]
    corpus = cycle.read(cycle.ROOT / cycle.PREFLIGHT)["validation"]["corpus_sha256"]
    replay, size, top = query_replayer(workspace, corpus)
    writers, episodes = [], []
    for series, stages in (
        (spec["source_study"], ("source",)),
        (spec["validation_study"], ("write", "solve")),
    ):
        root = workspace / "runs" / series
        reg, closed = cycle.read(root / "registration.json"), cycle.read(root / "completion.json")
        require(
            closed["registration_sha256"]
            == digest((root / "registration.json").read_bytes())
            == primary["source_registration" if stages == ("source",) else "validation_registration"],
            "Different registration",
        )
        for case in reg["preflight"]["cases"]:
            task, policy = case["task_id"], case.get("writer_policy", "source")
            for stage in stages:
                folder = root / ("task-" + case["task"]) / stage
                rows = study.checked_shard(root, folder, closed, reg)
                for row in rows:
                    form = row["arm"].split("__")[0]
                    if stage == "write":
                        (ref,) = [
                            w
                            for w in primary["writers"]
                            if (w["task"], w["policy"], w["form"]) == (task, policy, form)
                        ]
                        writers.append(
                            dict(
                                task=task, policy=policy, form=form, **inspect_writer(folder, row, task, ref)
                            )
                        )
                    else:
                        if stage == "source":
                            ref = primary["sources"][task]
                        else:
                            (ref,) = [
                                v
                                for v in primary["trials"][policy]
                                if (v["task"], v["arm"]) == (task, row["arm"])
                            ]
                        episodes.append(
                            dict(
                                task=task,
                                policy=policy,
                                form=form,
                                **inspect_episode(folder, row, task, ref, replay),
                            )
                        )
    require(len(episodes) == 44 and len(writers) == 40, "This diagnosis requires the full published cohort")
    for task in DOCS:
        (source,) = [e for e in episodes if e["task"] == task and e["policy"] == "source"]
        notes = [w for w in writers if w["task"] == task]
        require(
            len({w["public_context_sha256"] for w in notes}) == 1
            and all(
                (w["source"], w["feedback"], w["public_docs"])
                == (source["run"], source["success"], source["observed"])
                for w in notes
            ),
            "Different writer experiences, documents or official feedback",
        )
    return dict(
        format="banking-policy-cycle-diagnosis/v1",
        scope="post_hoc_offline_fixed_queries_and_observed_episodes",
        inputs={p.name: digest(p.read_bytes()) for p in (primary_path, dev_path)},
        corpus_sha256=corpus,
        documents=size,
        top_k=top,
        witnesses=DOCS,
        summary=summarize(episodes, writers, dev),
        writers=writers,
        episodes=episodes,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=cycle.ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cycle.save(args.output, diagnose(args.workspace.resolve()))
