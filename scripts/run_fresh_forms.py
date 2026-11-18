"""Run either stage of the preregistered new-customer four-form comparison."""
import argparse
import json
from pathlib import Path

from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.fresh_forms import run_study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("source", "comparison"))
    parser.add_argument("--config", type=Path, default=ROOT / "protocols/pilots/tau2-new-customer-forms-v1.json")
    parser.add_argument("--workspace", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--live", action="store_true")
    a = parser.parse_args()
    result = run_study(ROOT, a.workspace, a.workspace / "data/tau2-bench", a.config, a.stage, live=a.live)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
