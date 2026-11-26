"""Compare registered endpoints in an archived result and retain references to the scored observations."""

import argparse
import sys
from pathlib import Path

from agent_memory_eval.adapters.bfcl_control_results import BFCL_ANSWER, BFCL_EXPOSURE, control_trials
from agent_memory_eval.adapters.retail_revisit_results import FINAL_DB, FIRST_TARGET, revisit_trials
from agent_memory_eval.adapters.tau_banking_results import BANKING_SUCCESS, banking_trials
from agent_memory_eval.analysis.efficiency import METRICS
from agent_memory_eval.analysis.paired_trials import compare_trials
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import pointer


READERS = {
    metric.name: (metric, reader, kind, effort)
    for metric, reader, kind, effort in (
        (FIRST_TARGET, revisit_trials, "observed", ()),
        (FINAL_DB, revisit_trials, "observed", ()),
        (BANKING_SUCCESS, banking_trials, "observed", METRICS),
        (BFCL_ANSWER, control_trials, "scripted_control", ()),
        (BFCL_EXPOSURE, control_trials, "scripted_control", ()),
    )
}


def _observations(trials, archive):
    records = []
    for trial in trials:
        if not trial.evidence_pointer:
            raise ContractError("An archived observation needs an evidence pointer")
        pointer(archive, trial.evidence_pointer)
        records.append(dict(task=trial.task, slot=trial.slot, source=trial.source, started=trial.started,
                            success=trial.success, pointer=trial.evidence_pointer))
    return records


def archive_report(path, metric_name, reference, candidate, *, mode=None, expected_sha256=None):
    """Use one immutable archive for both arms. The adapter binds each native field to its metric."""
    if metric_name not in READERS:
        raise ContractError(f"Unknown outcome metric: {metric_name}")
    if not all(isinstance(arm, str) and arm.strip() for arm in (reference, candidate)):
        raise ContractError("Both policy conditions need a name")
    path = Path(path)
    data = path.read_bytes()
    checksum = digest(data)
    if expected_sha256 is not None and checksum != expected_sha256:
        raise ContractError("Archived outcome file differs from its expected SHA-256")
    archive = read_json(data)
    if not isinstance(archive, dict):
        raise ContractError("Expected a JSON object containing the archived results")
    metric, read_trials, kind, effort = READERS[metric_name]
    left = read_trials(archive, reference, metric, mode)
    right = read_trials(archive, candidate, metric, mode)
    return dict(format="agent-memory-paired-outcomes/v1", archive_name=path.name, archive_sha256=checksum, record_kind=kind,
                conditions=dict(reference=reference, candidate=candidate), selection=dict(mode=mode) if mode else {},
                comparison=compare_trials(left, right, metrics=effort, test_signs=kind == "observed"),
                observations=dict(reference=_observations(left, archive), candidate=_observations(right, archive)))


def recording_main(argv):
    parser = argparse.ArgumentParser(description="Replay source opportunities, first decisions and stopping-state progress.")
    parser.add_argument("--adapter", choices=("banking-opening", "bfcl-relevance"), required=True)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace containing runs/ and relative paths")
    parser.add_argument("--checkout", type=Path, required=True, help="Pinned native benchmark checkout")
    parser.add_argument("--recording", type=Path, help="BFCL recording directory; banking uses the registered study names")
    parser.add_argument("--sources-only", action="store_true", help="Read source outcomes before validation or writing")
    parser.add_argument("--summary", action="store_true", help="Omit individual rows and receipts while keeping their checksums")
    parser.add_argument("--policy-evidence", action="store_true", help="Trace public cues, no-call decisions, paired outcomes and costs")
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    profile = (workspace / args.profile).resolve()
    checkout = (workspace / args.checkout).resolve()
    from agent_memory_eval.analysis.policy_evidence import PolicyEvidence
    collector = PolicyEvidence() if args.policy_evidence else None
    try:
        if args.adapter == "banking-opening":
            if args.recording:
                parser.error("Banking recording names are fixed by --profile")
            from agent_memory_eval.adapters.banking_trajectories import banking_report
            result = banking_report(profile, workspace, checkout, args.sources_only, collector=collector)
        else:
            if not args.recording:
                parser.error("--recording is required for BFCL")
            from agent_memory_eval.adapters.bfcl_trajectories import bfcl_report
            result = bfcl_report(profile, (workspace / args.recording).resolve(), checkout, args.sources_only, collector=collector)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    if collector is not None:
        result = collector.report(result, summary=args.summary)
    elif args.summary:
        from agent_memory_eval.analysis.trajectory import compact_report
        result = compact_report(result)
    print(canonical(result).decode())


def main():
    if sys.argv[1:2] == ["recording"]:
        return recording_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__, epilog="Use recording --help for a trajectory report from native logs.")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--metric", choices=sorted(READERS), required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--mode", help="Registered customer request mode for retail revisits")
    parser.add_argument("--sha256", help="Expected SHA-256 of the archived result")
    args = parser.parse_args()
    try:
        result = archive_report(args.archive, args.metric, args.reference, args.candidate,
                                mode=args.mode, expected_sha256=args.sha256)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(canonical(result).decode())


if __name__ == "__main__":
    main()
