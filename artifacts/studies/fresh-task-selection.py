"""Pick study tasks by a rule fixed before any result: eligible tasks in SHA-256 order of a salted id."""
import hashlib, json, sys
from pathlib import Path
SALT = "fresh-loop-20261127"
def order(domain, ids):
    return sorted(ids, key=lambda t: hashlib.sha256(f"{SALT}|{domain}|{t}".encode()).hexdigest())
from agent_memory_eval.study.adapters.tau_banking import modules_for, DOMAINS
modules_for(str(Path.cwd()))
from agent_memory_eval.adapters.tau2_task_start import greeting_start
import importlib
out = {}
for adapter, split in (("tau-banking", None), ("tau2-airline", "train"), ("tau2-retail", "train")):
    spec = DOMAINS[adapter]
    env = importlib.import_module(f"tau2.domains.{spec['domain']}.environment")
    tasks = {t.id: t for t in env.get_tasks()}
    pool = env.get_tasks_split()[split] if split else list(tasks)
    eligible = [t for t in pool if {r.value for r in tasks[t].evaluation_criteria.reward_basis} in spec["basis"]
                and (spec["nl_assertions"] or not tasks[t].evaluation_criteria.nl_assertions) and greeting_start(tasks[t].initial_state)]
    chosen = order(spec["domain"], eligible)[:8]
    out[adapter] = dict(eligible=len(eligible), development=chosen[:5], confirmation=chosen[5:])
bfcl = json.loads(Path("protocols/diagnostics/bfcl-memory-cohort-transfer-v1.json").read_text())["tasks"]
live, irr = order("bfcl", bfcl["live_relevance"])[:8], order("bfcl", bfcl["irrelevance"])[:8]
out["bfcl-relevance"] = dict(eligible=len(bfcl["live_relevance"]) + len(bfcl["irrelevance"]),
                             development=live[:5] + irr[:5], confirmation=live[5:] + irr[5:])
print(json.dumps(out, indent=1))
