"""Benchmark adapters for declared studies. Each prepares tasks, runs stages and reads outcome rows."""


def adapter_for(name):
    if name == "bfcl-relevance":
        from agent_memory_eval.study.adapters.bfcl import BfclRelevance
        return BfclRelevance()
    if name == "tau-banking":
        from agent_memory_eval.study.adapters.tau_banking import TauBanking
        return TauBanking()
    raise ValueError(f"Unknown benchmark adapter: {name}")
