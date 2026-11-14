"""Worker-private memory rounds around the pinned native tau2 agent and environment."""

from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

from agent_memory_eval.adapters.tau2 import Tau2Bridge
from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.contracts.connection import native_parameters
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.runner.router import ToolRouter


class NativeBusiness:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.stop_tools = orchestrator.agent.additional_stop_tool_names

    @property
    def ended(self):
        return self.orchestrator.done

    @property
    def steps(self):
        return self.orchestrator.step_count


class Tau2MemoryBridge(Tau2Bridge):
    def __init__(self, *args, memory, bank_id, policy, solve_instruction, post_instruction, **kwargs):
        super().__init__(*args, **kwargs)
        self.memory, self.bank_id, self.policy = memory, bank_id, policy
        self.native_system = self.systems["worker"]
        self.systems["worker"] += "\n<memory_policy>\n" + solve_instruction + "\n" + canonical(asdict(policy)).decode() + "\n</memory_policy>"
        self.systems["writer"] = self.systems["worker"]
        self.models = dict(self.models)
        self.clients["writer"], self.models["writer"] = self.clients["worker"], self.models["worker"]
        self.post_instruction = post_instruction
        self.history_sources, self.history_hashes, self.tool_sources = {}, {}, {}
        self.pending, self.input_prefix = None, None
        self.original_next = self.orchestrator.agent.generate_next_message
        self.original_execute = self.orchestrator._execute_tool_calls
        self.router = ToolRouter(self.journal, memory, NativeBusiness(self.orchestrator), bank_id, policy,
                                 business_dispatch=self.business_dispatch)

    def config_role(self, actor):
        return "worker" if actor == "writer" else super().config_role(actor)

    def check_tools(self, actor, tools_schema):
        if actor == "user_simulator":
            return super().check_tools(actor, tools_schema)
        phase = "post_task_write" if actor == "writer" else "solve"
        expected = memory_tools(self.policy.tools(phase))
        actual = [tool for tool in (tools_schema or []) if tool["function"]["name"].startswith("memory_")]
        if actual != expected:
            raise ContractError("Memory schemas differ from the allowed phase tools")
        if actor == "writer" and tools_schema != expected:
            raise ContractError("The post-task writer has memory tools only")

    def publish(self, wire, origin):
        if self.ctx.phase == "solve":
            self.journal.append([self.ctx.event("business.transition", dict(origin=origin,
                public_message_ref=self.journal.blobs.put(wire)))])

    def remember(self, index, native_wire, source):
        fingerprint = digest(canonical(native_wire))
        if index in self.history_hashes and self.history_hashes[index] != fingerprint:
            raise ContractError("Native worker history was rewritten")
        self.history_hashes[index] = fingerprint
        self.history_sources[index] = source

    def message_entries(self, actor, messages):
        if actor == "user_simulator":
            return super().message_entries(actor, messages)
        entries = []
        for index, wire in enumerate(messages):
            if index in self.history_sources:
                self.remember(index, wire, self.history_sources[index])
            elif wire["role"] == "system" and index == 0:
                source = self.registry.template(dict(message=wire), roles=("worker", "writer"))
                self.remember(index, wire, (source, "/message"))
            elif wire["role"] == "tool":
                source = self.tool_sources.get(digest(canonical(wire)))
                if source is None:
                    raise ContractError("A native tool result has no recorded producer")
                self.remember(index, wire, source)
            elif wire["role"] == "user" or (wire["role"] == "assistant" and not self.history_hashes.keys() - {0}):
                event = self.ctx.event("business.transition", dict(origin="tau2_worker_input",
                    public_message_ref=self.journal.blobs.put(wire)))
                self.journal.append([event])
                source = self.registry.add("business_result", event["event_id"])
                self.remember(index, wire, (source, ""))
            else:
                raise ContractError("An untracked worker message was blocked")
            entries.append(self.history_sources[index])
        self.input_prefix = self.journal.public_prefix(self.ctx)
        return entries

    def invoke(self, actor, *, messages, tools=None, **kwargs):
        if actor == "user_simulator":
            return super().invoke(actor, messages=messages, tools=tools, **kwargs)
        if not messages or messages[0].content != self.native_system:
            raise ContractError("Native worker policy changed")
        phase = "post_task_write" if actor == "writer" else "solve"
        wrapped = list(messages)
        wrapped[0] = messages[0].model_copy(update=dict(content=self.systems[actor]))
        approved_tools = list(tools or []) + [SimpleNamespace(openai_schema=schema)
            for schema in memory_tools(self.policy.tools(phase))]
        answer = super().invoke(actor, messages=wrapped, tools=approved_tools or None, **kwargs)
        raw, source, _ = self.last_response
        # Native parsing reserializes arguments. Reuse the provider's original text
        # on subsequent requests so self-authored spans retain their exact producer.
        self.remember(len(messages), self.utils.to_litellm_messages([answer])[0], (source, ""))
        self.publish(raw, "tau2_worker_output")
        return answer

    def business_dispatch(self, wire_call, arguments):
        call = self.native_calls[wire_call["id"]]
        if call.name != wire_call["function"]["name"] or call.arguments != arguments:
            raise ContractError("Native call arguments differ from the recorded response")
        result = self.original_execute([call])[0]
        self.native_results[call.id] = result
        return self.utils.to_litellm_messages([result])[0]

    def dispatch(self, answer, request_id):
        from tau2.data_model.message import ToolMessage
        raw = self.last_response[0]
        self.native_calls = {call.id: call for call in answer.tool_calls}
        self.native_results = {}
        before = (self.orchestrator.step_count, self.orchestrator.num_errors)
        results = self.router.dispatch(self.ctx, request_id, raw["tool_calls"], public_prefix=self.input_prefix)
        full = []
        for wire, kind, producer in results:
            source = self.registry.add(kind, producer)
            native = self.native_results.get(wire["tool_call_id"])
            if native is None:
                native = ToolMessage(id=wire["tool_call_id"], role="tool", requestor="assistant",
                    content=wire["content"], error='"status":"error"' in wire["content"])
                self.publish(wire, "tau2_memory_result")
            self.tool_sources[digest(canonical(self.utils.to_litellm_messages([native])[0]))] = (source, "")
            full.append(native)
        after = (self.orchestrator.step_count, self.orchestrator.num_errors)
        self.journal.append([self.ctx.event("business.transition", dict(origin="tau2_memory_dispatch",
            request_id=request_id, native_step_before=before[0], native_step_after=after[0],
            native_errors_before=before[1], native_errors_after=after[1],
            call_order=[call["id"] for call in raw["tool_calls"]],
            memory_only=all(call["function"]["name"].startswith("memory_") for call in raw["tool_calls"])))])
        return full

    @staticmethod
    def flattened(message):
        return message.tool_messages if hasattr(message, "tool_messages") else [message]

    def generate_next_message(self, message, state):
        from tau2.data_model.message import MultiToolMessage
        if self.pending is not None:
            if self.pending.get("results") is None:
                raise ContractError("Worker resumed before the mixed batch finished")
            received = self.utils.to_litellm_messages(self.flattened(message))
            expected = self.utils.to_litellm_messages(self.pending["business_results"])
            if received != expected:
                raise ContractError("Native business results differ from the routed batch")
            message = MultiToolMessage(role="tool", tool_messages=self.pending["results"])
            self.pending = None
        while True:
            answer, state = self.original_next(message, state)
            answer.validate()  # Protocol violations must precede every side effect.
            calls = answer.tool_calls or []
            if not calls:
                return answer, state
            request_id = self.last_response[2]
            stop_batch = any(call.name in self.router.benchmark.stop_tools for call in calls) and len(calls) > 1
            if stop_batch:
                self.dispatch(answer, request_id)  # Router preflight rejects the entire batch.
                # Native tau2 creates its own rejected results without entering
                # the environment. Bind those to the router's recorded denials.
                from tau2.data_model.message import ToolMessage
                for call in calls:
                    rejected = ToolMessage(id=call.id, role="tool", requestor=call.requestor, error=True,
                        execution_status="rejected", content="Call rejected: a terminating tool must be "
                        "called on its own turn. No bundled calls executed.")
                    canonical_result = dict(role="tool", tool_call_id=call.id,
                        content=canonical(dict(status="error", error=dict(code="TOOL_BATCH_REJECTED"))).decode())
                    source = self.tool_sources[digest(canonical(canonical_result))]
                    self.tool_sources[digest(canonical(self.utils.to_litellm_messages([rejected])[0]))] = source
                return answer, state  # Retain tau2's own terminating-batch rejection and trace.
            business = [call for call in calls if not call.name.startswith("memory_")]
            if business:
                self.pending = dict(answer=answer, request_id=request_id, business_calls=business,
                                    results=None, prefix=self.input_prefix)
                return answer.model_copy(update=dict(tool_calls=business)), state
            message = MultiToolMessage(role="tool", tool_messages=self.dispatch(answer, request_id))

    def execute_tool_calls(self, calls):
        if self.pending is None:
            if any(call.name.startswith("memory_") for call in calls):
                raise ContractError("A memory tool escaped the worker")
            return self.original_execute(calls)
        if calls != self.pending["business_calls"]:
            raise ContractError("Native environment received a different batch")
        self.input_prefix = self.pending["prefix"]
        full = self.dispatch(self.pending["answer"], self.pending["request_id"])
        ids = {call.id for call in calls}
        business = [result for result in full if result.id in ids]
        self.pending.update(results=full, business_results=business)
        return business

    def post_write(self):
        from tau2.data_model.message import UserMessage
        if self.policy.write_timing not in {"post_task", "during_and_post"}:
            return "not_requested"
        if not self.orchestrator.done:
            raise ContractError("Post-task writing requires a frozen business episode")
        self.ctx = self.ctx.at("post_task_write")
        messages = list(self.orchestrator.agent_state.system_messages + self.orchestrator.agent_state.messages)
        if self.orchestrator.to_role.value == "agent":
            tail = self.flattened(self.orchestrator.message)
            if self.pending is not None:
                tail = self.pending["results"]
                self.pending = None
            messages.extend(tail)
        instruction = UserMessage(role="user", content=self.post_instruction)
        source = self.registry.template(dict(message=dict(role="user", content=self.post_instruction)), roles=("writer",))
        self.remember(len(messages), dict(role="user", content=self.post_instruction), (source, "/message"))
        messages.append(instruction)
        while True:
            answer = self.invoke("writer", model=self.config["worker"]["model_id"], messages=messages,
                                 **native_parameters(self.config, "worker"))
            answer.validate()
            messages.append(answer)
            if not answer.tool_calls:
                return "completed"
            messages.extend(self.dispatch(answer, self.last_response[2]))

    def __enter__(self):
        super().__enter__()
        self.stack.enter_context(patch.object(self.orchestrator.agent, "generate_next_message", self.generate_next_message))
        self.stack.enter_context(patch.object(self.orchestrator, "_execute_tool_calls", self.execute_tool_calls))
        return self
