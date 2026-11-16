"""Audit planned payloads without counting them as model attempts or memory exposure."""

from pathlib import Path

from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical
from agent_memory_eval.contracts.validation import validate_manifest
from agent_memory_eval.memory.context import request_with_past_memory
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.memory.writing import WriterFeedback, read_public_experience, writer_inputs, writer_template
from agent_memory_eval.recording.experience import read_recorded_context
from agent_memory_eval.recording.journal import BlobStore, read_experiment


def audit_preparations(root):
    root = Path(root)
    evidence = read_experiment(root)
    scan, state, experiment = evidence
    common, blobs, results = audit_requests(root, evidence=evidence), BlobStore(root), []
    recorded = {e["run_id"] for e in scan["events"].values() if e["type"] == "business.transition"
                and e["payload"].get("origin") == "request.prepared"}
    if recorded != set(state.get("request_preparations", {})):
        raise ContractError("Prepared request producers and indexed preparations differ")
    for run, value in state.get("request_preparations", {}).items():
        start = scan["starts"][run]
        events = [e for e in scan["events"].values() if e["run_id"] == run]
        producers = [e for e in events if e["type"] == "business.transition" and e["payload"].get("origin") == "request.prepared"]
        ends = [e for e in events if e["type"] == "run.ended"]
        if (value["format"] != "agent-memory-request-preparation/v1" or value["run_id"] != run
                or value["execution_mode"] != "prepare_only" or value["model_requests"] != 0
                or len(producers) != 1 or blobs.get(producers[0]["payload"]["preparation_ref"]) != value
                or len(ends) != 1 or ends[0]["payload"]["termination_reason"] != "request_preparation_only"):
            raise ContractError("Preparation lifecycle or metadata differs from recorded producers")
        if any(e["type"] in {"model.request_started", "model.request_finished", "memory.stored", "memory.input_included", "outcome.scored"} for e in events):
            raise ContractError("Preparation acquired a model attempt, memory write, exposure or task score")
        event = blobs.get(value["validation_event_ref"])
        manifest = blobs.get(value["manifest_ref"])
        if (event["event_id"] in scan["events"] or event["event_seq"] != producers[0]["event_seq"]
                or event["run_id"] != run or event["session_id"] != start["session_id"]
                or event["payload"]["actor_role"] != value["actor_role"]):
            raise ContractError("Hypothetical request validation context is not its preparation point")
        spans = validate_manifest(root, manifest, event, blobs.get(value["sources_ref"]),
            scan["events"], value["approved_templates"])
        request = blobs.get(value["request_ref"])
        if manifest["request_ref"] != value["request_ref"]:
            raise ContractError("Prepared manifest refers to another request")
        config = blobs.get(value["configuration_ref"])
        if value["kind"] == "writer":
            experience = read_public_experience(blobs, scan, config["experience_ref"])
            if experience.get("local_ready_seq", experience["source_end_seq"]) >= start["event_seq"]:
                raise ContractError("Writer preparation precedes its recorded experience")
            source_run, task_ref = experience["source_run_id"], experience["task_ref"]
            expected = writer_template(config["model_config"], config["instruction"])
            system = expected.pop("system")
            expected["messages"] = [system] + writer_inputs(blobs, scan, experience, WriterFeedback(**config["feedback"]))
            if value["actor_role"] != "writer" or start["payload"]["bank_id"] is not None or spans:
                raise ContractError("Initial writer preparation acquired a memory bank or memory exposure")
        elif value["kind"] == "snapshot_replay":
            source = read_recorded_context(blobs, config["decision_ref"])
            policy = MemoryPolicy(**config["policy"])
            if policy.write_timing != "none" or policy.same_episode_read:
                raise ContractError("Replay preparation is not read-only")
            source_run, task_ref = source["archive"]["source_run_id"], source["archive"]["task_ref"]
            expected = request_with_past_memory(source["wire"], policy,
                snapshot_items(blobs, state, config["snapshot_id"]), instruction=config["instruction"])
            if value["actor_role"] != "worker" or start["payload"]["start_snapshot_id"] != config["snapshot_id"]:
                raise ContractError("Replay preparation changed its worker role or starting snapshot")
            bank = state["banks"][start["payload"]["bank_id"]]
            if not bank["sealed"] or bank["heads"] != state["snapshots"][config["snapshot_id"]]["heads"]:
                raise ContractError("Preparation modified or failed to seal the forked bank")
        else:
            raise ContractError("Unknown prepared request kind")
        size, cap = len(canonical(request)), value["max_input_bytes"]
        if ((cap is not None and (type(cap) is not int or cap < 1))
                or start["payload"]["source_run_ids"] != [source_run] or start["payload"]["task_ref"] != task_ref):
            raise ContractError("Preparation limits or source run differ")
        if (request != expected or value["input_bytes"] != size or value["planned_memory_spans"] != len(spans)
                or value["fits_input_limit"] != (None if cap is None else size <= cap)):
            raise ContractError("Prepared wire, size or planned span count differs")
        results.append(dict(run_id=run, kind=value["kind"], input_bytes=size,
            fits_input_limit=value["fits_input_limit"], planned_memory_spans=len(spans),
            request_ref=value["request_ref"]))
    return dict(status="passed", experiment_id=experiment, preparations=results,
        preparation_model_requests=0, preparation_memory_exposures=0,
        all_model_requests=common["requests"], scope="offline_request_preparation_not_a_model_experiment")
