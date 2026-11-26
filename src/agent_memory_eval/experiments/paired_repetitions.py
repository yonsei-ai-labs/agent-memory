"""Arrange independent writing attempts and repeated fresh readers of each sealed bank."""

from dataclasses import dataclass

from agent_memory_eval.contracts.core import ContractError


@dataclass(frozen=True)
class PairedRepetitions:
    writer_draws: int = 1
    readers_per_bank: int = 1

    def __post_init__(self):
        if any(type(n) is not int or n < 1 for n in (self.writer_draws, self.readers_per_bank)):
            raise ContractError("Writer and reader repetition counts must be positive integers")

    @classmethod
    def from_spec(cls, spec):
        if "repetitions" not in spec:
            return cls()
        values = spec["repetitions"]
        if not isinstance(values, dict) or set(values) != {"writer_draws", "readers_per_bank"}:
            raise ContractError("Register both repetition axes")
        return cls(**values)

    def stages(self):
        """Physical stage folder, operation, and reader repetition (zero for writing)."""
        return [("write", "write", 0), ("solve", "solve", 1)] + [
            (f"solve-repeat-{n}", "solve", n) for n in range(2, self.readers_per_bank + 1)
        ]

    def writer_cells(self, case, policies):
        for policy in policies:
            for draw in range(1, self.writer_draws + 1):
                label = policy if self.writer_draws == 1 else f"{policy}__draw-{draw}"
                cell = dict(case, task=case["task_id"] + "__" + label, writer_policy=policy)
                if self.writer_draws > 1 or self.readers_per_bank > 1:
                    cell["writer_draw"] = draw
                yield label, cell
