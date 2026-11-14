"""Audit recorded memory-tool visibility and raw choices without any model calls."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.memory_visibility import inspect_visibility
from agent_memory_eval.contracts.core import digest
from agent_memory_eval.contracts.validation import ROOT, validators


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", nargs="+", type=Path, default=[Path("runs") / name for name in (
        "tau2-delivery-luna-deepseek1017-development-01", "tau2-autonomy-luna-deepseek1017-pilot-02",
        "tau2-memory-luna-deepseek1017-check-01", "tau2-autonomy-luna-deepseek1017-pilot-01")])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    checks = validators()
    sources = ["src/agent_memory_eval/analysis/memory_visibility.py", "scripts/audit_tau2_memory_visibility.py",
        "src/agent_memory_eval/adapters/tau2.py", "src/agent_memory_eval/adapters/tau2_memory.py",
        "src/agent_memory_eval/models/openrouter.py", "src/agent_memory_eval/memory/tools.py",
        "src/agent_memory_eval/memory/service.py", "src/agent_memory_eval/runner/router.py"]
    result = dict(format="agent-memory-tool-visibility-audit/v1", new_model_calls=0,
        inference="mechanical_exposure_and_dispatch_checks_not_a_causal_model_or_benchmark_comparison",
        implementation=[dict(path=p, sha256=digest((ROOT / p).read_bytes())) for p in sources],
        experiments=[inspect_visibility(p, checks=checks) for p in args.roots])
    if args.verify:
        if json.loads(args.verify.read_text()) != result:
            raise ValueError("Visibility audit differs from saved evidence")
    if args.output:
        with args.output.open("x") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(dict(new_model_calls=0, experiments=[{k: e[k] for k in (
        "root", "requests_checked", "summary", "dispatch_parity", "issue_counts")} for e in result["experiments"]]),
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
