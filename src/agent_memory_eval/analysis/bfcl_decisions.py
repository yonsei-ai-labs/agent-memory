"""Adapt BFCL's decoded function calls to a decision over its public, pinned task start."""

from agent_memory_eval.analysis.decision_attribution import Action, Step
from agent_memory_eval.contracts.core import ContractError, digest


def decoded_choice(start, request_id, calls, *, visible_fields=None):
    if start.task.benchmark != "bfcl":
        raise ContractError("Expected a BFCL task start")
    actions = []
    for call in calls:
        if not isinstance(call, dict) or len(call) != 1:
            raise ContractError("Expected a single function per decoded BFCL call")
        name, args = next(iter(call.items()))
        if not isinstance(name, str) or not name or not isinstance(args, dict):
            raise ContractError("Invalid decoded BFCL call")
        actions.append(Action.from_function(dict(name=name, arguments=args)))
    fields = frozenset(visible_fields) if visible_fields is not None else None
    return Step("worker", request_id, start.controls_sha256, digest(start.public_input), None, tuple(actions), fields)
