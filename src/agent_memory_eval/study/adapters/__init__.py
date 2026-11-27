"""Benchmark adapters for declared studies. Each prepares tasks, runs stages and reads outcome rows."""


def adapter_for(name):
    if name == "bfcl-relevance":
        from agent_memory_eval.study.adapters.bfcl import BfclRelevance
        return BfclRelevance()
    if name in ("tau-banking", "tau2-airline", "tau2-retail"):
        from agent_memory_eval.study.adapters import tau_banking
        return {"tau-banking": tau_banking.TauBanking, "tau2-airline": tau_banking.Tau2Airline,
                "tau2-retail": tau_banking.Tau2Retail}[name]()
    raise ValueError(f"Unknown benchmark adapter: {name}")
