"""Serial mixed dispatch with batch preflight and preserved side-effect order."""

from copy import deepcopy

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json


class ToolRouter:
    def __init__(self, journal, memory, benchmark, bank_id, policy, *, business_dispatch=None):
        self.journal, self.memory, self.benchmark = journal, memory, benchmark
        self.bank_id, self.policy = bank_id, policy
        self.business_dispatch = business_dispatch

    def _denied(self, ctx, call, code):
        wire = dict(role="tool", tool_call_id=call["id"],
                    content=canonical(dict(status="error", error=dict(code=code))).decode())
        event = ctx.event("policy.denied", dict(code=code, tool=call["function"]["name"],
            tool_result_ref=self.journal.blobs.put(wire)))
        self.journal.append([event])
        return wire, "policy_result", event["event_id"]

    def dispatch(self, ctx, request_id, calls, *, public_prefix=None):
        ids = [call["id"] for call in calls]
        if not calls or len(set(ids)) != len(ids):
            raise ContractError("A dispatch needs distinct call IDs")
        finish = next((event for event in self.journal.events.values()
            if event["type"] == "model.request_finished" and event["run_id"] == ctx.run_id
            and event["payload"]["request_id"] == request_id), None)
        if finish is None or finish["payload"]["status"] != "response" or finish["phase"] != ctx.phase:
            raise ContractError("Dispatch requires a completed response in this run and phase")
        response = self.journal.blobs.get(finish["payload"]["response_ref"])
        if canonical(response["choices"][0]["message"].get("tool_calls", [])) != canonical(calls):
            raise ContractError("Tool calls differ from the recorded response")
        key = ctx.run_id + ":" + request_id
        state = deepcopy(self.journal.state)
        batches = state.setdefault("batches", {})
        if key in batches:
            raise ContractError("A dispatched batch cannot be automatically replayed")
        batches[key] = dict(status="started", calls_sha256=digest(canonical(calls)))
        self.journal.append([], state=state)
        stop = any(call["function"]["name"] in self.benchmark.stop_tools for call in calls)
        if stop and len(calls) > 1:
            results = [self._denied(ctx, call, "TOOL_BATCH_REJECTED") for call in calls]
        else:
            results = []
            for index, call in enumerate(calls):
                name = call["function"]["name"]
                try:
                    arguments = read_json(call["function"]["arguments"].encode())
                except (ValueError, AttributeError):
                    results.append(self._denied(ctx, call, "INVALID_ARGUMENT"))
                    continue
                if not isinstance(arguments, dict):
                    results.append(self._denied(ctx, call, "INVALID_ARGUMENT"))
                    continue
                if name.startswith("memory_"):
                    if self.bank_id is None or name not in {"memory_store", "memory_list", "memory_get"}:
                        results.append(self._denied(ctx, call, "POLICY_DENIED"))
                        continue
                    op_id = "op_" + digest(canonical([ctx.run_id, request_id, call["id"], index]))
                    denial = "POLICY_DENIED" if ctx.phase == "solve" and self.benchmark.ended else None
                    result = self.memory.execute(ctx, self.bank_id, op_id, name, arguments,
                                                 call_id=call["id"], denial=denial, prefix=public_prefix)
                    results.append((result.wire, "memory_return", result.returned_event_id))
                elif ctx.phase != "solve" or self.benchmark.ended:
                    results.append(self._denied(ctx, call, "POLICY_DENIED"))
                else:
                    # Only this branch advances the business environment/user projection.
                    if self.business_dispatch is None:
                        value = self.benchmark.execute(name, arguments)
                        wire = dict(role="tool", tool_call_id=call["id"], content=canonical(value).decode())
                    else:
                        wire = self.business_dispatch(call, arguments)
                    event = ctx.event("business.transition", dict(action=name, arguments=arguments,
                        public_message_ref=self.journal.blobs.put(wire),
                        business_step_index=self.benchmark.steps))
                    self.journal.append([event])
                    results.append((wire, "business_result", event["event_id"]))
        state = deepcopy(self.journal.state)
        state["batches"][key]["status"] = "finished"
        self.journal.append([], state=state)
        return results
