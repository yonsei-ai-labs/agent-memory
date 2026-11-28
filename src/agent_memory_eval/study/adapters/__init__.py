"""Benchmark adapters for declared studies. Each prepares tasks, runs stages and reads outcome rows."""


def adapter_for(plan_or_name):
    if isinstance(plan_or_name, str):
        name, transfer = plan_or_name, False
    else:
        name, transfer = plan_or_name["benchmark"]["adapter"], "transfer" in plan_or_name
    if transfer:
        if name == "bfcl-relevance":
            from agent_memory_eval.study.adapters.bfcl_transfer import BfclTransfer
            return BfclTransfer()
        if name in ("tau2-airline", "tau2-retail"):
            from agent_memory_eval.study.adapters.tau_transfer import TauTransfer
            return TauTransfer(name)
        raise ValueError(f"Unsupported transfer benchmark: {name}")
    if name == "tau2-retail-revisit":
        from agent_memory_eval.study.adapters.retail_revisit import RetailRevisit
        return RetailRevisit()
    if name == "bfcl-relevance":
        from agent_memory_eval.study.adapters.bfcl import BfclRelevance
        return BfclRelevance()
    if name in ("tau-banking", "tau2-airline", "tau2-retail"):
        from agent_memory_eval.study.adapters import tau_banking
        return {"tau-banking": tau_banking.TauBanking, "tau2-airline": tau_banking.Tau2Airline,
                "tau2-retail": tau_banking.Tau2Retail}[name]()
    raise ValueError(f"Unknown benchmark adapter: {name}")
