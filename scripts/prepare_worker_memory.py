"""Prepare recorded-experience writer and empty-control replay inputs; never send HTTP."""

import argparse
import json
from pathlib import Path

from agent_memory_eval.analysis.preparation import audit_preparations
from agent_memory_eval.contracts.core import MemoryPolicy, canonical, digest
from agent_memory_eval.memory.preparation import prepare_snapshot_replay, prepare_writer_request
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.memory.writing import WRITER_MODEL_FIELDS, WriterFeedback
from agent_memory_eval.recording.experience import (
    capture_recorded_context, import_recorded_experience, read_recorded_context,
)
from agent_memory_eval.recording.journal import Journal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--source-log-sha256", required=True)
    parser.add_argument("--decision-request-id", required=True)
    parser.add_argument("--writer-instruction", type=Path, required=True)
    parser.add_argument("--replay-instruction", type=Path, required=True)
    parser.add_argument("--critique", type=Path)
    parser.add_argument("--critique-origin")
    parser.add_argument("--page-size", type=int, required=True)
    parser.add_argument("--max-input-bytes", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve().is_relative_to(args.source.resolve()):
        parser.error("Use a new output directory outside the preserved source")
    if bool(args.critique) != bool(args.critique_origin):
        parser.error("Critique requires both an explicit file and its origin")
    if args.max_input_bytes is not None and args.max_input_bytes < 1:
        parser.error("Input-byte cap must be positive or omitted as unresolved")
    policy = MemoryPolicy(True, "none", False, args.page_size, read_mode="preload")
    writer_instruction = args.writer_instruction.read_text(encoding="utf-8")
    replay_instruction = args.replay_instruction.read_text(encoding="utf-8")
    if not writer_instruction.strip() or not replay_instruction.strip():
        parser.error("Writer and replay instructions must be nonempty")
    feedbacks = [WriterFeedback("none"), WriterFeedback("result_only")]
    if args.critique:
        feedbacks.append(WriterFeedback("critique", args.critique.read_text(encoding="utf-8"), args.critique_origin))
    with Journal(args.output, "worker-memory-request-preparation", contract_version="1.2") as journal:
        experience = import_recorded_experience(journal, args.source,
            source_run_id=args.source_run, log_sha256=args.source_log_sha256)
        decision = capture_recorded_context(journal, args.source, source_run_id=args.source_run,
            log_sha256=args.source_log_sha256, request_id=args.decision_request_id)
        recorded = read_recorded_context(journal.blobs, decision)
        config = {key:value for key,value in recorded["wire"].items() if key in WRITER_MODEL_FIELDS}
        for feedback in feedbacks:
            prepare_writer_request(journal, experience, feedback, instruction=writer_instruction,
                model_config=config, condition_id="writer-" + feedback.level, max_input_bytes=args.max_input_bytes)
        memory = MemoryService(journal, record_kind=recorded["archive"]["record_kind"])
        bank = memory.create_bank("empty-control-preparation", policy)
        snapshot = memory.freeze(bank)
        memory.seal(bank)
        prepare_snapshot_replay(journal, decision, snapshot, policy=policy, instruction=replay_instruction,
            condition_id="empty-control", max_input_bytes=args.max_input_bytes)
    report = dict(audit_preparations(args.output), source_run_id=args.source_run,
        source_log_sha256=args.source_log_sha256, decision_request_id=args.decision_request_id,
        feedback_levels=[f.level for f in feedbacks], model_configuration_origin="copied_from_historical_worker_request",
        input_cap_scope="offline_compatibility_check_only_not_an_approved_experiment_limit",
        copied_blob_bytes=sum(p.stat().st_size for p in (args.output / "blobs/sha256").iterdir()),
        journal_sha256=digest((args.output / "log.jsonl").read_bytes()),
        instruction_sha256=dict(writer=digest(writer_instruction.encode()), replay=digest(replay_instruction.encode())),
        live_ready=False, pending=["worker_generated_memory_snapshot_and_replay", "primary_comparison_and_controls",
            "sampling_and_independent_annotation", "per_role_and_bank_limits", "paid_budget_and_authorization",
            "current_model_endpoint_verification"])
    if not args.critique:
        report["pending"].append("critique_text_origin_and_information_access")
    (args.output / "preparation.json").write_bytes(canonical(report) + b"\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
