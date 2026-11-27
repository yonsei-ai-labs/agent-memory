"""Check a same-task memory comparison against its sealed no-memory execution."""
from copy import deepcopy

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.memory.context import memory_system_suffix, snapshot_message
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.recording.experience import _source
from agent_memory_eval.recording.journal import safe_path


# The original transport recorded its source hash; these two defaults were in
# OpenRouterClient.respond at that revision, before they became profile fields.
BASELINE_TRANSPORT = "0781638a889bb544bd6c454fdfe5b450d750f17b0099255bc1ccf9ecb61fd70a"
BASELINE_INPUT_BYTES = 64000
BASELINE_HTTP_SECONDS = 45


class FirstPassMismatch(ContractError):
    code = "FIRST_PASS_MISMATCH"


def check_common_read_instruction(text, *, paired_delivery):
    """Writing-only and fixed-bank delivery comparisons each hold their solve notice fixed."""
    # frozen-memory-replay-v1.txt, or tau2-delivery-memory.txt shared by both delivery arms.
    expected = ("050147f01a4ae531d38f0d8a85b2bec2fcc07df51e7cf3076d8f1735526fca1d" if paired_delivery else
                "cd5d4dede32681b19eaa7eb1919d59b9a08ae2732170afba22cba35ab1d245e4")
    if digest(text.encode()) != expected:
        raise FirstPassMismatch("Common memory-use instruction changed outside the declared comparison")


def archived_controls(workspace, checkout, source, *, match_opening=False):
    from tau2.domains.airline.data_model import FlightDB
    selected = {r["run_id"]: r["task_id"] for r in source["cohort"]}
    controls = {task: {"run_id": run, "wire": {}} for run, task in selected.items()}
    with _source(safe_path(workspace, source["root"]), source["log_sha256"]) as (scan, blobs, _, _):
        state = blobs.get(scan["records"][-1]["state_ref"])
        global_control = blobs.get(state["baseline_screening"]["control_ref"])
        transport = global_control["implementation"]["sources"]["src/agent_memory_eval/models/openrouter.py"]
        if transport != BASELINE_TRANSPORT or global_control["response_cache_enabled"] is not False:
            raise ContractError("Unknown archived transport limits or enabled response cache")
        for name, asset in global_control["assets"].items():
            if digest(safe_path(checkout, name).read_bytes()) != asset["sha256"]:
                raise ContractError(f"First-pass benchmark asset differs: {name}")
        archived_db = global_control["assets"]["data/tau2/domains/airline/db.json"]["text_ref"]
        initial_db = FlightDB.model_validate_json(blobs.get(archived_db)).get_hash()
        for event in scan["events"].values():
            if event["run_id"] not in selected:
                continue
            match = controls[selected[event["run_id"]]]
            pay = event["payload"]
            if event["type"] == "business.transition" and pay.get("origin") == "baseline.run_control":
                if "control" in match:
                    raise ContractError("Duplicate archived first-pass control")
                control = blobs.get(pay["control_ref"])
                match.update(control=control, control_sha256=pay["control_ref"]["sha256"],
                             task_sha256=digest(canonical(blobs.get(control["task_ref"]))))
            if event["type"] == "model.request_started" and pay["actor_role"] not in match["wire"]:
                wire = blobs.get(pay["request_ref"])
                role = pay["actor_role"]
                match["wire"][role] = {k: v for k, v in wire.items() if k != "messages"}
                if role == "user_simulator":
                    match["customer_opening_sha256"] = digest(canonical(wire))
        for match in controls.values():
            if "control" not in match or set(match["wire"]) != {"worker", "user_simulator"}:
                raise ContractError("Missing archived role input or first-pass control")
            match["initial_db"] = initial_db
            if match_opening:
                from agent_memory_eval.experiments.matched_trial import source_task_start
                events = [e for e in scan["events"].values() if e["run_id"] == match["run_id"]]
                requests = [e for e in events if e["type"] == "model.request_started"]
                control = dict(match["control"], initial_db=initial_db, initial_user_db=None, task_sha256=match["task_sha256"])
                start = source_task_start(control, events, requests, blobs)
                match.update(opening_control=control, opening_source=dict(run_id=match["run_id"], log_sha256=source["log_sha256"],
                             task_start=start.record(), first_worker_request_id=requests[1]["payload"]["request_id"]))
    return controls


def check_native(match, config, orch, native):
    control = match["control"]
    actual = dict(task=digest(canonical(orch.task.model_dump(mode="json"))), config=config,
                  native=native.model_dump(mode="json"),
                  initial_db=orch.environment.get_db_hash(), initial_user_db=orch.environment.get_user_db_hash(),
                  systems=dict(worker=orch.agent.system_prompt, user_simulator=orch.user.system_prompt),
                  tools=dict(worker=[t.openai_schema for t in orch.agent.tools],
                             user_simulator=[t.openai_schema for t in (orch.user.tools or [])]))
    expected = dict(task=match["task_sha256"], config=control["config"], native=control["native_resolved"],
                    initial_db=match["initial_db"], initial_user_db=None,
                    systems=control["systems"], tools=control["tools"])
    for field, value in actual.items():
        if canonical(value) != canonical(expected[field]):
            raise FirstPassMismatch(f"Same-task comparison differs from first pass in {field} ({match['run_id']})")
    opening = {}
    if "opening_source" in match:
        source = match["opening_source"]
        opening = dict(task_start_sha256=digest(canonical(source["task_start"])), source_log_sha256=source["log_sha256"],
                       source_worker_request_id=source["first_worker_request_id"])
    return dict(**opening, source_run_id=match["run_id"], source_control_sha256=match["control_sha256"],
                task_sha256=actual["task"], native_seed=native.seed, initial_db=orch.environment.get_db_hash(),
                initial_user_db=orch.environment.get_user_db_hash(), native_config_sha256=digest(canonical(actual["native"])),
                systems_sha256=digest(canonical(actual["systems"])), tools_sha256=digest(canonical(actual["tools"])),
                customer_opening_sha256=match["customer_opening_sha256"], episode_limits=dict(
                    control["config"]["guards"], max_input_bytes=BASELINE_INPUT_BYTES, http_timeout_seconds=BASELINE_HTTP_SECONDS))


def _history(messages):
    # τ² repeats the function name at the call's top level and reserializes
    # arguments. The journal keeps the provider's original assistant message.
    values = deepcopy(messages)
    for message in values:
        if message.get("role") == "assistant":
            if message.get("tool_calls") is None:
                message.pop("tool_calls", None)
            elif message["tool_calls"] and message.get("content") is None:
                message.pop("content", None)
        for call in message.get("tool_calls") or []:
            if "name" in call and call["name"] == call["function"]["name"]:
                del call["name"]
            arguments = call["function"]["arguments"]
            if isinstance(arguments, str):
                call["function"]["arguments"] = read_json(arguments.encode())
    return values


def wire_verifier(match, policy, instruction, items, *, read_descriptions=None):
    control = match["control"]
    settings = deepcopy(match["wire"])
    tools = memory_tools(policy.tools("solve"), read_descriptions=read_descriptions)
    if tools:
        settings["worker"]["tools"] = [*settings["worker"].get("tools", []), *tools]
    systems = dict(control["systems"])
    if policy.enabled and read_descriptions is None:
        systems["worker"] += memory_system_suffix(policy, instruction)
    opening = True

    def verify(actor, wire, native_messages):
        nonlocal opening
        messages = wire["messages"]
        if (canonical({k: v for k, v in wire.items() if k != "messages"}) != canonical(settings[actor])
                or not messages or messages[0] != dict(role="system", content=systems[actor])):
            raise FirstPassMismatch(f"First-pass role settings, policy or tools changed for {actor}")
        expected = list(native_messages)
        if actor == "worker" and policy.preloads:
            expected.insert(1, snapshot_message(items))
        if canonical(messages) != canonical(expected):
            try:
                equivalent = actor == "worker" and canonical(_history(messages)) == canonical(_history(expected))
            except (KeyError, TypeError, ValueError, AttributeError):
                equivalent = False
            if not equivalent:
                raise FirstPassMismatch(f"Unexpected message outside the designated memory input for {actor}")
        if actor == "user_simulator" and opening:
            if digest(canonical(wire)) != match["customer_opening_sha256"]:
                raise FirstPassMismatch("Initial customer request differs from the archived first pass")
            opening = False

    return verify
