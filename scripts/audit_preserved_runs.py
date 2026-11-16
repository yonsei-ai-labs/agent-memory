"""Compare complete current-code audits with preserved, commit-based baselines."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.recording.journal import safe_path


def auditors():
    from agent_memory_eval.analysis.connection import audit_connection
    from agent_memory_eval.analysis.tau2_autonomy import audit_pilot
    from agent_memory_eval.analysis.tau2_baseline import audit_baseline
    from agent_memory_eval.analysis.tau2_delivery import audit_delivery
    from agent_memory_eval.analysis.tau2_memory import audit_memory_check
    from agent_memory_eval.runner.decision_replay import audit_replay
    return dict(connection=audit_connection, autonomy=audit_pilot, baseline=audit_baseline,
                delivery=audit_delivery, memory=audit_memory_check, decision_replay=audit_replay)


def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ContractError("Preserved input must not contain symlinks")
        if path.is_file():
            info = path.stat()
            result[str(path.relative_to(root))] = [info.st_size, info.st_mtime_ns]
    return result


def audit_all(registry, root):
    if registry["format"] != "agent-memory-preserved-audit-baselines/v1":
        raise ContractError("Unsupported audit baseline registry")
    functions, results = auditors(), []
    names = [entry["id"] for entry in registry["runs"]]
    if not names or len(names) != len(set(names)):
        raise ContractError("Audit baseline IDs must be nonempty and unique")
    for entry in registry["runs"]:
        folder = safe_path(root, entry["path"])
        before = inventory(folder)
        for ref in entry["input_refs"]:
            if digest(safe_path(folder, ref["path"]).read_bytes()) != ref["sha256"]:
                raise ContractError("Preserved input changed: " + entry["id"])
        result = functions[entry["auditor"]](folder)
        checksum = digest(canonical(result))
        if checksum != entry["audit_sha256"]:
            raise ContractError("Complete audit output differs: " + entry["id"])
        if inventory(folder) != before:
            raise ContractError("Audit changed the preserved input inventory")
        results.append(dict(id=entry["id"], status="passed", audit_sha256=checksum,
                            input_files=len(before), input_inventory_preserved=True))
    return dict(format="agent-memory-preserved-audit-suite/v1", status="passed",
                runs=results, complete_audit_output_compared=True, new_model_or_catalog_calls=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path,
                        default=Path("protocols/reproductions/preserved-run-audits-v1.json"))
    parser.add_argument("--inputs-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.inputs_root.absolute()
    if args.output and args.output.resolve().is_relative_to((root / "runs").resolve()):
        parser.error("Audit output must be outside preserved runs")
    result = audit_all(read_json(args.registry.read_bytes()), root)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as stream:
            stream.write(canonical(result) + b"\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
