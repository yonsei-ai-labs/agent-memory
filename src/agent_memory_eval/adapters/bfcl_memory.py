"""Observe BFCL rec_sum's native append/retrieve ABI without exposing framework item IDs."""

from copy import deepcopy

from agent_memory_eval.contracts.core import ContractError, MemoryFault


class RecSumMemory:
    format = "bfcl-rec-sum/v1"
    tools = {"memory_append": "memory_store", "memory_retrieve": "memory_get"}

    def __init__(self, backend):
        self.backend = backend

    def evaluate(self, name, arguments, live, visible):
        if self.backend.memory != live:
            raise ContractError("BFCL native state differs from the bank bound to this run")
        # Run on a private copy. Only a committed write can advance the native session.
        copy = deepcopy(self.backend)
        if name == "memory_append":
            if set(arguments) != {"text"} or not isinstance(arguments["text"], str):
                raise MemoryFault("INVALID_ARGUMENT")
            result = copy.memory_append(arguments["text"])
            return result, copy.memory
        if name != "memory_retrieve" or arguments:
            raise MemoryFault("INVALID_ARGUMENT")
        copy.memory = visible
        return copy.memory_retrieve(), None

    def commit(self, body):
        self.backend.memory = body

    def recover_committed(self, previous, committed):
        """Finish an interrupted native update after the matching journal transaction is durable."""
        if self.backend.memory == committed:
            return
        if self.backend.memory != previous:
            raise ContractError("BFCL retry found an unrelated native checkpoint")
        self.commit(committed)
