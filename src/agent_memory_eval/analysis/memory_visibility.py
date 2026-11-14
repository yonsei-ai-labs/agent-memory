"""Read-only diagnosis of offered tools, raw model choices, and routed returns."""

from collections import Counter, defaultdict
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.contracts.validation import inspect_journal, validators
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.recording.journal import BlobStore


READ_TOOLS = {"memory_list", "memory_get"}


def inspect_choice(wire, response, expected_names):
    """Schema presence and a permitted choice are separate facts."""
    tools = wire.get("tools") or []
    names = [t["function"]["name"] for t in tools]
    offered = [n for n in names if n.startswith("memory_")]
    schemas = [t for t in tools if t["function"]["name"].startswith("memory_")]
    message = (response.get("choices") or [{}])[0].get("message") or {}
    calls = message.get("tool_calls") or []
    choice = wire.get("tool_choice", "omitted")
    permitted = choice in ("auto", "required", "omitted") if isinstance(choice, str) else False
    if isinstance(choice, dict):
        permitted = choice.get("function", {}).get("name") in READ_TOOLS
    expected_read = bool(READ_TOOLS & set(expected_names))
    issues = []
    if schemas != memory_tools(expected_names):
        issues.append("memory_tool_schema_mismatch")
    if len(names) != len(set(names)):
        issues.append("duplicate_tool_name")
    if expected_read and not permitted:
        issues.append("read_tool_choice_blocked")
    legacy = message.get("function_call") or {}
    if str(legacy.get("name", "")).startswith("memory_"):
        issues.append("legacy_memory_function_call")
    return dict(
        tool_count=len(names), memory_tools=offered,
        read_tools_available=READ_TOOLS <= set(names),
        read_choice_permitted=permitted and bool(READ_TOOLS & set(names)),
        memory_tool_positions_1based=[i + 1 for i, n in enumerate(names) if n.startswith("memory_")],
        tool_choice=choice, issues=issues,
        memory_calls=[dict(id=c["id"], name=c["function"]["name"]) for c in calls
                      if c["function"]["name"].startswith("memory_")],
        business_calls=[c["function"]["name"] for c in calls
                        if not c["function"]["name"].startswith("memory_")],
        finish_reason=(response.get("choices") or [{}])[0].get("finish_reason"),
        content_mentions_memory_tools=isinstance(message.get("content"), str)
            and any(n in message["content"] for n in READ_TOOLS),
    )


def match_dispatches(raw_calls, returns):
    """Detect a generated call lost before a return, or a return without a call."""
    used, unmatched = set(), []
    for result in returns:
        candidates = [i for i, call in enumerate(raw_calls) if i not in used
                      and call["run_id"] == result["run_id"] and call["id"] == result["id"]
                      and call["seq"] < result["seq"] and call["name"] == result["name"]]
        if len(candidates) != 1:
            unmatched.append(result)
        else:
            used.add(candidates[0])
    return dict(raw_memory_calls=len(raw_calls), routed_results=len(returns),
                missing_results=[c for i, c in enumerate(raw_calls) if i not in used],
                unmatched_results=unmatched)


def inspect_visibility(root, *, checks=None):
    root = Path(root)
    raw = (root / "log.jsonl").read_bytes()
    scan = inspect_journal(raw, checks or validators())
    if scan["tail_bytes"] or scan["incomplete_runs"] or scan["pending_requests"]:
        raise ContractError("Visibility diagnosis requires closed journal records")
    blobs = BlobStore(root)
    state = blobs.get(scan["records"][-1]["state_ref"])
    events = list(scan["events"].values())
    finished = {e["payload"]["request_id"]: e for e in events if e["type"] == "model.request_finished"}
    calls, returns, observed = [], [], []
    groups = defaultdict(list)
    for event in events:
        p = event["payload"]
        if event["type"] == "model.request_started":
            actor, run = p["actor_role"], event["run_id"]
            wire = blobs.get(p["request_ref"])
            end = finished[p["request_id"]]
            response = blobs.get(end["payload"]["response_ref"]) if end["payload"]["response_ref"] else {}
            policy = state["assignments"][run]["policy"]
            phase = "post_task_write" if actor == "writer" else "solve"
            expected = [] if actor == "user_simulator" else MemoryPolicy(**policy).tools(phase)
            row = inspect_choice(wire, response, expected)
            system = wire["messages"][0]["content"]
            row.update(run_id=run, request_id=p["request_id"], seq=event["event_seq"],
                actor_role=actor, phase=event["phase"], condition=event["condition_id"],
                task_id=state["assignments"][run]["task_key"], request_ref=p["request_ref"],
                response_ref=end["payload"]["response_ref"], delivery_status=end["payload"]["delivery_status"],
                system_bytes=len(system.encode()), system_sha256=digest(system.encode()),
                memory_policy_tag_present="<memory_policy>" in system,
                policy=policy, response_status=end["payload"]["status"],
                parallel_tool_calls=wire.get("parallel_tool_calls", "omitted"))
            for call in row["memory_calls"]:
                calls.append(dict(call, run_id=run, request_id=p["request_id"], seq=end["event_seq"]))
            observed.append(row)
            groups[(run, actor, event["phase"])].append(row)
        elif event["type"] in {"memory.returned", "policy.denied"}:
            ref = p.get("result_ref") if event["type"] == "memory.returned" else p.get("tool_result_ref")
            if ref:
                wire = blobs.get(ref)
                if event["type"] == "memory.returned" or p.get("tool", "").startswith("memory_"):
                    name = ({"create": "memory_store", "update": "memory_store", "list": "memory_list",
                             "get": "memory_get"}[p["operation"]] if event["type"] == "memory.returned" else p["tool"])
                    returns.append(dict(run_id=event["run_id"], id=wire["tool_call_id"], seq=event["event_seq"],
                                        name=name, status=p.get("status", p.get("code"))))
    run_roles = []
    for (run, actor, phase), rows in groups.items():
        first = rows[0]
        assignment = state["assignments"][run]
        bank_id = assignment["bank_id"]
        bank = state["banks"][bank_id] if bank_id else None
        exposures = Counter(e["payload"]["source"]["kind"] for e in events
            if e["run_id"] == run and e["type"] == "memory.input_included" and e["payload"]["field"] == "body")
        run_roles.append(dict(run_id=run, actor_role=actor, phase=phase, condition=first["condition"],
            task_id=first["task_id"], request_count=len(rows),
            starting_bank_items=len(bank["start_heads"]) if bank else None,
            policy=first["policy"],
            tool_sets=[list(names) for names in sorted({tuple(r["memory_tools"]) for r in rows})],
            tool_counts=sorted({r["tool_count"] for r in rows}),
            tool_choices=sorted({str(r["tool_choice"]) for r in rows}),
            parallel_tool_call_modes=sorted({str(r["parallel_tool_calls"]) for r in rows}),
            memory_positions=[list(p) for p in sorted({tuple(r["memory_tool_positions_1based"]) for r in rows})],
            requests_with_both_read_tools=sum(r["read_tools_available"] for r in rows),
            requests_permitting_read_choice=sum(r["read_choice_permitted"] for r in rows),
            system_bytes_range=[min(r["system_bytes"] for r in rows), max(r["system_bytes"] for r in rows)],
            memory_policy_tag_requests=sum(r["memory_policy_tag_present"] for r in rows),
            raw_memory_calls=dict(Counter(c["name"] for r in rows for c in r["memory_calls"])),
            raw_business_calls=dict(Counter(c for r in rows for c in r["business_calls"])),
            finish_reasons=dict(Counter(str(r["finish_reason"]) for r in rows)),
            body_input_sources=dict(exposures) if actor == "worker" else {},
            content_mentions_memory_tools=sum(r["content_mentions_memory_tools"] for r in rows),
            issue_counts=dict(Counter(i for r in rows for i in r["issues"])),
            first_request=dict(event_seq=first["seq"], request_id=first["request_id"],
                request_ref=first["request_ref"], response_ref=first["response_ref"],
                system_sha256=first["system_sha256"])))
    totals = []
    for actor, condition, phase in sorted({(r["actor_role"], r["condition"], r["phase"]) for r in observed}):
        rows = [r for r in observed if (r["actor_role"], r["condition"], r["phase"]) == (actor, condition, phase)]
        totals.append(dict(actor_role=actor, condition=condition, phase=phase,
            runs=len({r["run_id"] for r in rows}), requests=len(rows),
            requests_with_both_read_tools=sum(r["read_tools_available"] for r in rows),
            requests_permitting_read_choice=sum(r["read_choice_permitted"] for r in rows),
            raw_memory_calls=dict(Counter(c["name"] for r in rows for c in r["memory_calls"])),
            business_call_count=sum(len(r["business_calls"]) for r in rows),
            tool_counts=sorted({r["tool_count"] for r in rows}),
            tool_choices=sorted({str(r["tool_choice"]) for r in rows}),
            issue_counts=dict(Counter(i for r in rows for i in r["issues"]))))
    parity = match_dispatches(calls, returns)
    return dict(root=str(root), log_sha256=digest(raw), journal_commits=len(scan["records"]),
        requests_checked=len(observed), summary=totals, run_roles=run_roles, dispatch_parity=parity,
        request_observations_sha256=digest(canonical(observed)),
        issue_counts=dict(Counter(i for r in observed for i in r["issues"])),
        limitation="recorded_wire_and_raw_choices_do_not_reveal_provider_internal_attention_or_why_the_model_declined")
