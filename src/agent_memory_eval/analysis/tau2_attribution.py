"""τ² adapters for a fixed first business decision and for graded paired episodes."""

from agent_memory_eval.analysis.decision_attribution import fingerprint, public_history
from agent_memory_eval.analysis.efficiency import episode_effort
from agent_memory_eval.analysis.recorded_decisions import READS, RecordedDecisions, observed_step, require
from agent_memory_eval.contracts.core import MemoryPolicy, digest, read_json
from agent_memory_eval.experiments.matched_trial import role_settings
from agent_memory_eval.memory.context import memory_system_suffix, snapshot_message
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.recording.journal import safe_path


def fixed_choice(folder, job):
    receipt = read_json((folder / "decision.json").read_bytes())
    status = receipt["status"]
    # Historical provider-error receipts identify the attempt by their sealed journal only.
    require(receipt.get("run") == job["run"] or (status == "error" and "run" not in receipt),
            "Unregistered decision")
    log = RecordedDecisions(folder, job["run"], receipt["journal_sha256"], receipt["journal_bytes"])
    require(log.state["assignments"][log.run]["job"] == job and fingerprint(log.items) == job["items_sha256"]
            and log.start["source_run_ids"] == job["writer_runs"] and log.start["fresh_session"],
            "Fixed choice differs from its registered task or frozen bank")
    require(log.requests and all(r["actor_role"] == "worker" for r in log.requests)
            and log.requests[0]["request_ref"]["sha256"] == job["initial_sha256"], "Unregistered opening")
    initial = log.blobs.get(log.requests[0]["request_ref"])
    require(fingerprint(initial["messages"][1:]) == job["prefix"], "Public decision prefix differs")
    out = dict(initial=job["initial_sha256"], status=status, step=None, focused=None, requests=len(log.requests),
               items=log.items, visible=[], public=initial["messages"])
    if status == "error":
        return out
    req = log.requests[-1]
    if status == "private_read_limit":
        require(len(log.requests) == 4 and all(e["payload"].get("termination_reason") == status
                for e in log.of_type("run.ended")) and log.of_type("run.ended"), "Unconfirmed private-only stop")
        for r in log.requests:
            message = log.reply(r)["message"]
            require(message.get("tool_calls") and all(c["function"]["name"] in READS for c in message["tool_calls"]),
                    "Business call in a private-only stop")
        out["focused"] = False
        return out
    require(status in {"checkpoint", "decision"}, "Unknown fixed-choice outcome")
    cp = log.blobs.get(receipt["checkpoint"])
    require(receipt["request"] == cp["request_id"] == req["request_id"] and cp["public_prefix_sha256"] == job["prefix"]
            and cp["initial_sha256"] == job["initial_sha256"] and cp["items_sha256"] == job["items_sha256"],
            "Decision cutoff differs from registration")
    wire = log.blobs.get(req["request_ref"])
    choice = log.reply(req)
    history = log.business_history(wire["messages"])
    visible = log.visible(req, wire)
    step = observed_step("worker", req, wire, choice, (job["customer"], job["task"], job["source"], job["mode"]),
                         history, visible, private_names=READS)
    focused = choice.get("finish_reason") in {"stop", "tool_calls"} and step.selects_single_call(
        "get_order_details", {"order_id": job["target"]})
    out.update(step=step, focused=focused,
               visible=visible, public=history, at_registered_input=(
                   {k: v for k, v in initial.items() if k != "messages"} == {k: v for k, v in wire.items() if k != "messages"}
                   and public_history(initial["messages"]) == step.history))
    return out


def listed_orders(messages):
    """Read only the order list in the current authenticated profile's tool reply."""
    calls = {}
    for message in messages:
        calls.update((c["id"], c["function"]["name"]) for c in message.get("tool_calls") or [])
        if message["role"] == "tool" and calls.get(message.get("tool_call_id")) == "get_user_details":
            yield read_json(message["content"].encode()).get("orders", [])


def graded_episode(folder, row, case, read_instruction_sha, source=None, *, public_inputs=None):
    result = row["result"]
    log = RecordedDecisions(safe_path(folder, row["directory"]), row["run_id"], result["log_sha256"])
    marker, = [e["payload"] for e in log.of_type("business.transition")
               if e["payload"].get("origin") == "matched_trial.native_control"]
    control = log.blobs.get(marker["control_ref"])
    receipt = log.blobs.get(marker["receipt_ref"])
    require(receipt["native_sha256"] == fingerprint(control) == case["native_sha256"]
            and receipt["customer_opening_sha256"] == case["customer_opening_sha256"] and log.start["fresh_session"],
            "Episode is outside the matched native task")
    if source:
        require(receipt["source_run_id"] == source["run_id"] and
                receipt["source_log_sha256"] == source["result"]["log_sha256"], "Unpaired first pass")
    settings = role_settings(control)
    assignment = log.state["assignments"][log.run]
    policy = MemoryPolicy(**assignment["policy"])
    instruction = log.blobs.get(assignment["instruction_ref"])
    if source:
        require(policy.enabled and policy.read_mode == row["arm"].split("__")[1]
                and digest(instruction.encode()) == read_instruction_sha and len(log.items) == result["bank_sizes"]["items"],
                "Wrong memory policy, instruction or bank")
    else:
        require(not policy.enabled and not log.items, "First pass has memory")
    from agent_memory_eval.runner.task_start import FORMAT as START_FORMAT, TaskStart
    start = None
    if case.get("task_start_mode") == START_FORMAT:
        proof, = [e["payload"] for e in log.of_type("business.transition")
                  if e["payload"].get("origin") == "task_start.first_decision"]
        start = TaskStart.from_record(log.blobs.get(proof["task_start_ref"]))
        require(start.controls_sha256 == case["native_sha256"] and start.task.task_id ==
                f'{case["domain"]}/{case["task_id"]}' and proof["source_run_id"] == receipt["source_run_id"]
                and proof["source_log_sha256"] == receipt["source_log_sha256"], "Unregistered decision start")
    imported = start is not None and source is not None
    opening = None if imported else log.requests[0]
    if imported:
        require(log.requests[0]["actor_role"] == "worker", "Imported opening generated a new customer request")
    else:
        require(opening["actor_role"] == "user_simulator" and opening["request_ref"]["sha256"] ==
                case["customer_opening_sha256"], "First customer request differs from the registered opening")
    systems = {role: dict(role="system", content=value) for role, value in control["systems"].items()}
    expected_system = systems["worker"]
    if policy.enabled:
        expected_system = dict(role="system", content=expected_system["content"] + memory_system_suffix(policy, instruction))
    expected_settings = dict(settings["worker"])
    private_tools = policy.tools("solve")
    if private_tools:
        expected_settings["tools"] = [*expected_settings.get("tools", []), *memory_tools(private_tools)]
    steps, effort_choices = [], []
    first_worker = True
    for req in log.requests:
        role = req["actor_role"]
        if role not in systems:
            continue
        wire = log.blobs.get(req["request_ref"])
        history = wire["messages"]
        choice = log.reply(req)
        if role == "worker":
            actual = {k: v for k, v in wire.items() if k != "messages"}
            require(actual == expected_settings and history[0] == expected_system, "Undeclared worker settings")
            if policy.preloads:
                require(history[1] == snapshot_message(log.items), "Undeclared preload")
                history = [history[0], *history[2:]]
            history = [systems[role], *log.business_history(history[1:])]
            if start is not None and first_worker:
                require(read_json(start.public_input) == dict(messages=history, tools=settings[role].get("tools", [])),
                        "Observed first worker input differs outside of memory")
            first_worker = False
            spans = log.visible(req, wire)
            calls = choice["message"].get("tool_calls") or []
            reads = [c for c in calls if c["function"]["name"] in private_tools]
            effort_choices.append(dict(status="response", reads=reads))
            if reads and len(reads) == len(calls) and not choice["message"].get("content"):
                continue
            actor = "worker"
            if public_inputs is not None:
                public_inputs[req["request_id"]] = history
        else:
            require({k: v for k, v in wire.items() if k != "messages"} == settings[role] and history[0] == systems[role],
                    "Undeclared customer settings")
            if start is not None and req is opening:
                require(choice["message"]["content"] == read_json(start.public_input)["messages"][-1]["content"],
                        "Source opening differs from the registered public observation")
                continue  # The shared opening precedes the first decision in both episodes.
            spans, actor = [], "customer"
        steps.append(observed_step(actor, req, settings[role], choice, case["native_sha256"], history, spans,
                                   private_names=private_tools if role == "worker" else ()))
    score, = [e["payload"] for e in log.of_type("outcome.scored")]
    require(type(score["official_success"]) is bool and score["grading_status"] == result["grading_status"] == "graded"
            and score["official_success"] == result["official_success"] and score["official_artifact_ref"] == result["report_ref"],
            "Ledger grade differs from the original score")
    report = log.blobs.get(score["official_artifact_ref"])
    return steps, episode_effort(row, report["messages"], effort_choices)
