"""Verify a new no-memory episode and its same-task descendants before dispatch."""

from agent_memory_eval.adapters.tau2_task_start import matched_opening, recorded_task_start
from agent_memory_eval.contracts.connection import provider_route
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.experiments.first_pass_match import FirstPassMismatch, wire_verifier
from agent_memory_eval.memory.context import memory_system_suffix, snapshot_message
from agent_memory_eval.recording.experience import _closed_run, _source
from agent_memory_eval.runner.task_start import FORMAT as TASK_START_FORMAT, TaskStart, TaskStartMismatch


def native_control(orch, native, connection, limits, http_seconds, grader):
    """Everything shared by the two conversational roles, before adding memory."""
    return dict(
        task_sha256=digest(canonical(orch.task.model_dump(mode="json"))),
        config=connection, native=native.model_dump(mode="json"),
        initial_db=orch.environment.get_db_hash(), initial_user_db=orch.environment.get_user_db_hash(),
        systems=dict(worker=orch.agent.system_prompt, user_simulator=orch.user.system_prompt),
        tools=dict(worker=[t.openai_schema for t in orch.agent.tools],
                   user_simulator=[t.openai_schema for t in (orch.user.tools or [])]),
        limits=limits, http_timeout_seconds=http_seconds, grader=grader,
    )


def role_settings(control):
    settings = {}
    for role, tools in control["tools"].items():
        cfg = control["config"][role]
        settings[role] = dict(model=cfg["model_id"], provider=provider_route(control["config"], role),
                              stream=False, **cfg["parameters"])
        if tools:
            settings[role].update(tools=tools, tool_choice="auto")
    return settings


def opening_hash(orch, customer_settings, utils):
    """Render the registered default greeting from a disposable native environment."""
    if orch.task.initial_state is not None:
        raise FirstPassMismatch("This comparison requires an original task with empty initial history")
    orch.initialize()
    if orch.to_role.value != "user" or orch.user_state.messages:
        raise FirstPassMismatch("Expected a new customer responding to the native greeting")
    state = orch.user_state.model_copy(deep=True)
    state.messages.append(orch.message)
    messages = utils.to_litellm_messages(state.system_messages + state.flip_roles())
    return digest(canonical(dict(customer_settings, messages=messages)))


def verify_trial(journal, ctx, spec, control, policy, instruction, items):
    """The full control is analyst-only. Each role receives its own native messages."""
    first = spec.get("source")
    fingerprint = digest(canonical(control))
    if (policy.enabled != (first is not None) or fingerprint != spec["native_sha256"]
            or (policy.enabled and digest(instruction.encode()) != spec["instruction_sha256"])):
        raise FirstPassMismatch("Fresh task configuration, memory instruction or assignment changed")
    settings = role_settings(control)
    if first is not None and (
        first["registration_sha256"] != spec["registration_sha256"]
        or first["native_sha256"] != fingerprint or first["wire"] != settings
        or first["customer_opening_sha256"] != spec["customer_opening_sha256"]
        or first["public_context_sha256"] != spec["public_context_sha256"]
    ):
        raise FirstPassMismatch("First-pass receipt does not belong to this registered case")
    receipt = dict(registration_sha256=spec["registration_sha256"], native_sha256=fingerprint,
                   source_run_id=first["run_id"] if first else None,
                   source_log_sha256=first["log_sha256"] if first else None,
                   customer_opening_sha256=spec["customer_opening_sha256"])
    journal.append([ctx.event("business.transition", dict(origin="matched_trial.native_control",
        control_ref=journal.blobs.put(control), receipt_ref=journal.blobs.put(receipt)))])
    match = dict(control=control, wire=settings, customer_opening_sha256=spec["customer_opening_sha256"])
    return wire_verifier(match, policy, instruction, items)


def bind_task_start(journal, ctx, spec, control, bridge, modules, policy, instruction, items):
    if spec["task_start_mode"] != TASK_START_FORMAT:
        raise TaskStartMismatch("Unsupported matched task start mode")
    source = spec.get("source")
    reference = TaskStart.from_record(source.get("task_start")) if source is not None else None
    verify = bridge.verify_wire

    def record(start):
        journal.append([ctx.event("business.transition", dict(origin="task_start.first_decision",
            task_start_ref=journal.blobs.put(start.record()), source_run_id=source["run_id"] if source else None,
            source_log_sha256=source["log_sha256"] if source else None,
            source_worker_request_id=source["first_worker_request_id"] if source else None))])
        messages = read_json(start.public_input)["messages"]
        if policy.enabled:
            messages[0]["content"] += memory_system_suffix(policy, instruction)
        if policy.preloads:
            messages.insert(1, snapshot_message(items))

        def verify_first(actor, wire, native_messages):
            verify(actor, wire, native_messages)
            if actor == "worker":
                if canonical(wire["messages"]) != canonical(messages):
                    raise TaskStartMismatch("First worker request differs outside the assigned memory")
                bridge.verify_wire = verify

        bridge.verify_wire = verify_first

    return matched_opening(bridge.orchestrator, modules, control, role_settings(control)["user_simulator"],
                           verify, record, reference=reference)


def source_task_start(control, events, requests, blobs):
    if (len(requests) < 2 or
            [e["payload"]["actor_role"] for e in requests[:2]] != ["user_simulator", "worker"]):
        raise TaskStartMismatch("Expected an original customer opening followed by the first worker request")
    customer, worker = requests[:2]
    replies = [e for e in events if e["type"] == "model.request_finished"
               and e["payload"]["request_id"] == customer["payload"]["request_id"]]
    if (len(replies) != 1 or replies[0]["payload"]["status"] != "response"
            or not customer["event_seq"] < replies[0]["event_seq"] < worker["event_seq"]):
        raise TaskStartMismatch("Source opening has no preceding confirmed customer response")
    answer = blobs.get(replies[0]["payload"]["response_ref"])["choices"][0]["message"]
    return recorded_task_start(control, blobs.get(worker["payload"]["request_ref"]),
                               blobs.get(customer["payload"]["request_ref"]), answer)


def source_receipt(folder, source, case, *, reconstruct_archived_start=False):
    """Accept a fresh measured source only from the registered case's source shard."""
    with _source(folder, source.source_log_sha256) as (scan, blobs, _, _):
        start, _, _ = _closed_run(scan, source.source_run_id)
        events = [e for e in scan["events"].values() if e["run_id"] == source.source_run_id]
        controls = [e for e in events if e["payload"].get("origin") == "matched_trial.native_control"]
        requests = [e for e in events if e["type"] == "model.request_started"]
        if (len(controls) != 1 or not requests or controls[0]["event_seq"] >= requests[0]["event_seq"]
                or start["record_kind"] != "observed" or start["condition_id"] != "no_memory"
                or not start["payload"]["fresh_session"] or start["payload"]["bank_id"] is not None
                or start["payload"]["start_snapshot_id"] is not None or start["payload"]["source_run_ids"]):
            raise FirstPassMismatch("Missing prerequest evidence for a fresh no-memory source")
        pay = controls[0]["payload"]
        receipt, control = blobs.get(pay["receipt_ref"]), blobs.get(pay["control_ref"])
        if (receipt["source_run_id"] is not None or receipt["source_log_sha256"] is not None
                or receipt["registration_sha256"] != case["registration_sha256"]
                or receipt["native_sha256"] != case["native_sha256"] or digest(canonical(control)) != case["native_sha256"]
                or control["config"]["benchmark"]["task_id"] != source.task_id):
            raise FirstPassMismatch("Source receipt differs from this series or native task")
        settings = {}
        customer_opening = None
        for event in requests:
            role = event["payload"]["actor_role"]
            if role not in ("worker", "user_simulator"):
                continue
            wire = blobs.get(event["payload"]["request_ref"])
            settings.setdefault(role, {k: v for k, v in wire.items() if k != "messages"})
            if role == "user_simulator" and customer_opening is None:
                customer_opening = digest(canonical(wire))
        if (settings != role_settings(control) or customer_opening != case["customer_opening_sha256"]
                or customer_opening != receipt["customer_opening_sha256"]):
            raise FirstPassMismatch("Observed source inputs differ from the registered native environment")
        extra = {}
        if "task_start_mode" in case:
            if case["task_start_mode"] != TASK_START_FORMAT:
                raise TaskStartMismatch("Unknown first-decision source mode")
            start = source_task_start(control, events, requests, blobs)
            saved = [blobs.get(e["payload"]["task_start_ref"]) for e in events
                     if e["payload"].get("origin") == "task_start.first_decision"]
            if saved != [start.record()] and (saved or not reconstruct_archived_start):
                raise TaskStartMismatch("Stored start differs from the observed source worker input")
            extra["task_start"] = start.record()
            extra["first_worker_request_id"] = requests[1]["payload"]["request_id"]
    return dict(registration_sha256=receipt["registration_sha256"], native_sha256=receipt["native_sha256"],
                customer_opening_sha256=customer_opening, wire=settings, run_id=source.source_run_id,
                log_sha256=source.source_log_sha256, public_context_sha256=source.public_context_sha256, **extra)
