"""Deterministic, synthetic business state, not a tau2/AppWorld/BFCL adapter."""

from copy import deepcopy
from dataclasses import dataclass

from agent_memory_eval.contracts.core import ContractError, new_id


@dataclass(frozen=True)
class SyntheticTask:
    key: str
    kind: str
    target: str

    def public_message(self):
        from agent_memory_eval.contracts.core import canonical
        return dict(role="user", content=canonical(dict(kind=self.kind, target=self.target,
            instruction="inspect로 확인한 뒤 apply하고 finish로 마친다.")).decode())


class FakeBenchmark:
    stop_tools = frozenset({"finish"})
    tool_names = ("inspect", "apply", "finish")

    def __init__(self, task):
        self.task = task
        self.state = dict(value=None, inspected=False)
        self.steps, self.user_turns = 0, 1
        self.ended = False
        self.public_trace = []
        self.private_marker = "GRADER_ONLY_" + new_id("marker")

    def execute(self, name, arguments):
        if self.ended:
            raise ContractError("Business task already ended")
        if name not in self.tool_names:
            return dict(status="error", error=dict(code="UNKNOWN_TOOL"))
        self.steps += 1
        self.public_trace.append(dict(name=name, arguments=deepcopy(arguments)))
        if name == "inspect" and arguments == {}:
            self.state["inspected"] = True
            return dict(status="ok", kind=self.task.kind, target=self.task.target)
        if name == "apply" and set(arguments) == {"value"}:
            if not self.state["inspected"]:
                return dict(status="error", error=dict(code="INSPECTION_REQUIRED"))
            self.state["value"] = arguments["value"]
            return dict(status="ok")
        if name == "finish" and arguments == {}:
            self.ended = True
            return dict(status="ok", ended=True)
        return dict(status="error", error=dict(code="INVALID_ARGUMENT"))

    def grade(self):
        self.ended = True
        return dict(record_kind="illustrative", synthetic_success=(self.state["value"] == self.task.target),
                    final_state=deepcopy(self.state), business_steps=self.steps, user_turns=self.user_turns,
                    private_marker=self.private_marker)
