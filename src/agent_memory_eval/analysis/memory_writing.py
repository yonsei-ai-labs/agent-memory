"""Audit feedback-limited writing without leaking source grading into writer input."""

from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.memory.writing import WriterFeedback, read_public_experience, writer_inputs
from agent_memory_eval.recording.journal import BlobStore, read_experiment


def audit_memory_writes(root):
    evidence = read_experiment(root)
    scan, state, _ = evidence
    common = audit_requests(root, evidence=evidence)
    blobs, writes = BlobStore(root), []
    for run, config in state.get("memory_writes", {}).items():
        start = scan["starts"][run]
        events = [e for e in scan["events"].values() if e["run_id"] == run]
        experience = read_public_experience(blobs, scan, config["experience_ref"])
        if experience["source_end_seq"] >= start["event_seq"]:
            raise ContractError("Writing precedes its closed source experience")
        if start["payload"]["source_run_ids"] != [experience["source_run_id"]]:
            raise ContractError("Writer source run differs")
        expected = [dict(role="system", content=config["instruction"])] + writer_inputs(
            blobs, scan, experience, WriterFeedback(**config["feedback"]))
        policy = MemoryPolicy(**config["policy"])
        requests = [e for e in events if e["type"] == "model.request_started"]
        if len(requests) > config["max_requests"]:
            raise ContractError("Writer exceeded the request limit")
        for event in requests:
            wire = blobs.get(event["payload"]["request_ref"])
            if (event["payload"]["actor_role"] != "writer" or event["phase"] != "post_task_write"
                    or wire["messages"][:len(expected)] != expected
                    or wire["tools"] != memory_tools(policy.tools("post_task_write"))):
                raise ContractError("Writer input, tools or feedback differs from the recorded configuration")
            for key, value in config["model_config"].items():
                if wire.get(key) != value:
                    raise ContractError("Writer model configuration changed")
        bank = state["banks"][config["bank_id"]]
        end = next(e for e in events if e["type"] == "run.ended")
        snapshot = state["snapshots"][end["payload"]["end_snapshot_id"]]
        if (not bank["sealed"] or bank["owner_run"] != run or bank["heads"] != snapshot["heads"]
                or bank["parent_snapshot"] is not None or bank["start_heads"]):
            raise ContractError("Writer bank is shared, unsealed or changed after freezing")
        stores = [e for e in events if e["type"] == "memory.stored"]
        if any(e["phase"] != "post_task_write" or e["payload"]["bank_id"] != config["bank_id"] for e in stores):
            raise ContractError("Memory was written outside its writer bank and phase")
        writes.append(dict(run_id=run, feedback_level=config["feedback"]["level"],
            requests=len(requests), memory_stores=len(stores),
            termination_reason=end["payload"]["termination_reason"]))
    return dict(status="passed", request_audit=common, writes=writes,
                scope="writing_and_provenance_only_no_memory_effect_claim")
