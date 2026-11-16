"""Audit feedback-limited writing without leaking source grading into writer input."""

from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.memory.writing import WriterFeedback, read_public_experience, writer_inputs
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from agent_memory_eval.recording.provenance import public_assistant_message


def audit_memory_writes(root):
    evidence = read_experiment(root)
    scan, state, _ = evidence
    common = audit_requests(root, evidence=evidence)
    blobs, writes = BlobStore(root), []
    for run, config in state.get("memory_writes", {}).items():
        start = scan["starts"][run]
        events = [e for e in scan["events"].values() if e["run_id"] == run]
        experience = read_public_experience(blobs, scan, config["experience_ref"])
        if experience.get("local_ready_seq", experience["source_end_seq"]) >= start["event_seq"]:
            raise ContractError("Writing precedes its closed source experience")
        if start["payload"]["source_run_ids"] != [experience["source_run_id"]]:
            raise ContractError("Writer source run differs")
        expected = [dict(role="system", content=config["instruction"])] + writer_inputs(
            blobs, scan, experience, WriterFeedback(**config["feedback"]))
        policy = MemoryPolicy(**config["policy"])
        if (not policy.enabled or policy.write_timing != "post_task" or policy.read_mode != "none"
                or policy.same_episode_read):
            raise ContractError("Writer policy is not isolated post-task-only writing")
        projection = config.get("response_projection")
        if projection not in (None, "public-assistant/v1"):
            raise ContractError("Unknown writer response projection")
        requests = [e for e in events if e["type"] == "model.request_started"]
        limits = ExecutionLimits(**config["execution_limits"]) if "execution_limits" in config else None
        cap = limits.writer_requests if limits else config["max_requests"]
        if len(requests) > cap:
            raise ContractError("Writer exceeded the request limit")
        history, pending, terminal = list(expected), [], False
        for event in events:
            payload, kind = event["payload"], event["type"]
            if kind == "model.request_started":
                wire = blobs.get(payload["request_ref"])
                wanted = dict(config["model_config"], messages=history,
                    tools=memory_tools(policy.tools("post_task_write")), tool_choice="auto")
                if (pending or terminal or payload["actor_role"] != "writer" or event["phase"] != "post_task_write"
                        or wire != wanted):
                    raise ContractError("Complete writer history, tools or configuration differs from its recorded producers")
                if limits and len(canonical(wire)) > limits.max_input_bytes:
                    finish = next(e["payload"] for e in events if e["type"] == "model.request_finished"
                                  and e["payload"]["request_id"] == payload["request_id"])
                    if finish["delivery_status"] != "not_submitted" or finish["response_ref"] is not None:
                        raise ContractError("An oversized writer request was submitted")
            elif kind == "model.request_finished":
                if payload["status"] != "response":
                    terminal = True
                    continue
                message = blobs.get(payload["response_ref"])["choices"][0]["message"]
                history.append(public_assistant_message(message) if projection else message)
                calls = message.get("tool_calls") or []
                pending = [call["id"] for call in calls]
                terminal = not pending
            elif kind == "memory.returned" or (kind == "policy.denied" and "tool_result_ref" in payload):
                ref = payload["result_ref"] if kind == "memory.returned" else payload["tool_result_ref"]
                message = blobs.get(ref)
                if not pending or message["tool_call_id"] != pending.pop(0):
                    raise ContractError("Writer tool results differ from the response call order")
                history.append(message)
            elif kind == "business.transition" and "action" in payload:
                raise ContractError("A post-task writer executed a business action")
        bank = state["banks"][config["bank_id"]]
        end = next(e for e in events if e["type"] == "run.ended")
        snapshot = state["snapshots"][end["payload"]["end_snapshot_id"]]
        if (start["payload"]["bank_id"] != config["bank_id"] or snapshot["bank_id"] != config["bank_id"]
                or not bank["sealed"] or bank["owner_run"] != run or bank["heads"] != snapshot["heads"]
                or bank["parent_snapshot"] is not None or bank["start_heads"]):
            raise ContractError("Writer bank is shared, unsealed or changed after freezing")
        stores = [e for e in events if e["type"] == "memory.stored"]
        if any(e["phase"] != "post_task_write" or e["payload"]["bank_id"] != config["bank_id"] for e in stores):
            raise ContractError("Memory was written outside its writer bank and phase")
        failed = any(e["type"] == "runtime.error" or (e["type"] == "model.request_finished"
            and (e["payload"]["status"] != "response" or e["payload"]["cost_status"] == "unknown")) for e in events)
        expected_status = "error" if failed else "completed" if stores else "empty"
        if not failed and (pending or not terminal):
            raise ContractError("Writer completion has no final reply or unfinished tool calls")
        if end["payload"]["termination_reason"] != "memory_write_" + expected_status:
            raise ContractError("Writer termination differs from recorded responses and errors")
        writes.append(dict(run_id=run, feedback_level=config["feedback"]["level"],
            requests=len(requests), memory_stores=len(stores),
            termination_reason=end["payload"]["termination_reason"]))
    return dict(status="passed", request_audit=common, writes=writes,
                scope="writing_and_provenance_only_no_memory_effect_claim")
