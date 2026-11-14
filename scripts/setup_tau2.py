"""Fetch a clean pinned checkout for benchmark data; never change an existing checkout."""

import argparse
from pathlib import Path
import subprocess

from agent_memory_eval.adapters.tau2 import PIN


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path("data/tau2-bench"))
    args = parser.parse_args()
    path = args.checkout
    if path.exists():
        commit = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        if commit != PIN or dirty:
            parser.error("Existing checkout differs or is dirty; choose a new checkout directory")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout", "https://github.com/sierra-research/tau2-bench.git", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "fetch", "--depth", "1", "origin", PIN], check=True)
        subprocess.run(["git", "-C", str(path), "checkout", "--detach", PIN], check=True)
    print("Pinned tau2 data checkout ready: " + str(path))


if __name__ == "__main__":
    main()
