"""The original public text opening of a pinned τ² task, before any worker action."""

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from unittest.mock import patch

from agent_memory_eval.contracts.connection import native_parameters
from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.runner.task_start import TaskKey, TaskStartMismatch, open_task_start


def empty_initial_state(state):
    """Some native tasks serialize an otherwise empty initialization object."""
    if state is None:
        return True
    data = state.initialization_data
    return not state.message_history and not state.initialization_actions and (
        data is None or (data.agent_data is None and data.user_data is None)
    )


def greeting_start(state):
    """The native reset may populate the database before the ordinary first greeting."""
    return state is None or (not state.message_history and not state.initialization_actions)


def initialized_databases(orch):
    """Predict data-only resets with the native implementation, keeping the live environment pristine."""
    state = orch.task.initial_state
    if empty_initial_state(state) or not greeting_start(state):
        return None
    environment = deepcopy(orch.environment)
    environment.set_state(state.initialization_data, None, [])
    return dict(agent=environment.get_db_hash(), user=environment.get_user_db_hash())


def task_key(control):
    bench = control["config"]["benchmark"]
    return TaskKey("tau2", bench["source_revision"], f'{bench["domain"]}/{bench["task_id"]}')


def business_input(messages, tools, control):
    if (not isinstance(messages, list) or len(messages) != 3 or not all(isinstance(m, dict) for m in messages)
            or messages[0] != dict(role="system", content=control["systems"]["worker"])
            or messages[1].get("role") != "assistant" or messages[1].get("tool_calls") is not None
            or set(messages[1]) - {"role", "content", "tool_calls"}
            or set(messages[2]) != {"role", "content"} or messages[2]["role"] != "user"
            or not all(isinstance(m.get("content"), str) and m["content"].strip() for m in messages)
            or tools != control["tools"]["worker"]):
        raise TaskStartMismatch("Expected the native policy, greeting, first public user text and business tools")
    return dict(messages=messages, tools=tools)


def recorded_task_start(control, worker_wire, customer_wire, customer_reply):
    """Project a sealed no-memory source onto its first public decision only."""
    public = business_input(worker_wire["messages"], worker_wire.get("tools", []), control)
    greeting, opening = public["messages"][1:]
    if (customer_wire["messages"] != [dict(role="system", content=control["systems"]["user_simulator"]),
                                      dict(role="user", content=greeting["content"])]
            or customer_reply.get("role") != "assistant" or customer_reply.get("tool_calls")
            or customer_reply.get("content") != opening["content"]):
        raise TaskStartMismatch("The first worker input does not contain the source customer's opening")
    return open_task_start(task_key(control), control, lambda _: public)


def _restore_customer(orch, utils, control, customer_settings, verify_wire, public):
    from tau2.data_model.message import UserMessage

    if set(public) != {"messages", "tools"}:
        raise TaskStartMismatch("The source is missing its original public τ² input")
    source = business_input(public["messages"], public["tools"], control)

    def restored(message, state):
        schemas = [tool.openai_schema for tool in (orch.user.tools or [])]
        if (state.messages or message is not orch.message or message.is_tool_call()
                or orch.user.llm != customer_settings["model"] or schemas != control["tools"]["user_simulator"]
                or orch.user.llm_args != native_parameters(control["config"], "user_simulator")):
            raise TaskStartMismatch("The native opening generator differs from the source")
        state.messages.append(message)
        rendered = utils.to_litellm_messages(state.system_messages + state.flip_roles())
        # Validate the would-be request. An imported observation incurs no provider call.
        verify_wire("user_simulator", dict(customer_settings, messages=rendered), rendered)
        return UserMessage(role="user", content=source["messages"][-1]["content"])

    # The native user appends the returned message to its own state. The hook is per instance.
    return patch.object(orch.user, "_generate_next_message", restored)


@contextmanager
def matched_opening(orch, modules, control, customer_settings, verify_wire, record, *, reference=None):
    """Preserve native run/step/termination. Only the first customer generation can be imported."""
    state = orch.task.initial_state
    if (orch.solo_mode or not greeting_start(state)
            or ("initialized_databases" in control) != (not empty_initial_state(state))):
        raise TaskStartMismatch("This τ² adapter supports text tasks starting at the native greeting")
    databases = control.get("initialized_databases", dict(agent=control["initial_db"], user=control["initial_user_db"]))
    utils = modules[2]
    native_step = orch.step
    start = None

    def prepare(public):
        opening = (nullcontext() if public is None else
                   _restore_customer(orch, utils, control, customer_settings, verify_wire, public))
        with opening:
            native_step()
        if orch.done or orch.to_role.value != "agent" or orch.message.is_tool_call():
            raise TaskStartMismatch("The initial observation did not reach a text worker decision")
        state = orch.agent_state
        messages = utils.to_litellm_messages([*state.system_messages, *state.messages, orch.message])
        return business_input(messages, [tool.openai_schema for tool in orch.agent.tools], control)

    def first_step():
        nonlocal start
        if start is not None:
            return native_step()
        if (orch.to_role.value != "user" or orch.from_role.value != "agent" or orch.user_state.messages
                or orch.agent_state.messages != [orch.message] or orch.trajectory != [orch.message]
                or databases["agent"] != orch.environment.get_db_hash()
                or databases["user"] != orch.environment.get_user_db_hash()
                or control["task_sha256"] != digest(canonical(orch.task.model_dump(mode="json")))):
            raise TaskStartMismatch("Expected the original task and a newly initialized native environment")

        start = open_task_start(task_key(control), control, prepare, reference=reference)
        record(start)

    with patch.object(orch, "step", first_step):
        yield
    if start is None:
        raise TaskStartMismatch("The run ended before a matched first decision was available")
