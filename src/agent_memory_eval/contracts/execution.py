"""Explicit opt-in limits; legacy profiles retain their original aggregate cap."""

from dataclasses import dataclass

from agent_memory_eval.contracts.core import ContractError


@dataclass(frozen=True)
class ExecutionLimits:
    worker_requests: int
    writer_requests: int
    user_simulator_requests: int
    max_input_bytes: int

    def __post_init__(self):
        for field in ("worker_requests", "writer_requests", "user_simulator_requests"):
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise ContractError("Each role needs an explicit nonnegative request limit")
        if type(self.max_input_bytes) is not int or self.max_input_bytes < 1:
            raise ContractError("An explicit positive input-byte limit is required")

    def cap(self, actor):
        if actor not in {"worker", "writer", "user_simulator"}:
            raise ContractError("Unknown role in execution limits")
        return getattr(self, actor + "_requests")
