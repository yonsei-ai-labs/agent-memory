"""Offline writer and snapshot replay preparation using the production request builders."""

from copy import deepcopy

from agent_memory_eval.contracts.core import ContractError, RunContext, canonical, new_id
from agent_memory_eval.memory.context import request_with_past_memory
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.memory.writing import read_public_experience, writer_inputs, writer_template
from agent_memory_eval.recording.experience import read_recorded_context
from agent_memory_eval.recording.query_variant import replay_decision
from agent_memory_eval.recording.preparation import record_prepared_request
from agent_memory_eval.recording.provenance import SourceRegistry


def _start(journal, ctx, task_ref, source_run_id, *, bank=None, parent=None):
    journal.append([ctx.at("prepare").event("run.started", dict(bank_id=bank, start_snapshot_id=parent,
        fresh_session=True, source_run_ids=[source_run_id], task_ref=task_ref))])


def _end(journal, ctx, snapshot=None):
    journal.append([ctx.at("seal").event("run.ended", dict(termination_reason="request_preparation_only",
        grading_status="not_available", end_snapshot_id=snapshot))])


def prepare_writer_request(journal, experience_ref, feedback, *, instruction, model_config,
                           condition_id, max_input_bytes=None):
    if max_input_bytes is not None and (type(max_input_bytes) is not int or max_input_bytes < 1):
        raise ContractError("Preparation input cap must be positive or explicitly unresolved")
    experience = read_public_experience(journal.blobs, journal.scan, experience_ref)
    template_value = writer_template(model_config, instruction)
    inputs = writer_inputs(journal.blobs, journal.scan, experience, feedback)
    ctx = RunContext(journal.experiment_id, new_id("prep"), new_id("s"), condition_id,
        "diagnostic", "post_task_write", experience["record_kind"])
    _start(journal, ctx, experience["task_ref"], experience["source_run_id"])
    registry = SourceRegistry(journal, ctx)
    template = registry.template(template_value, roles=("writer",))
    history = [(template, "/system")]
    for message in inputs:
        event = ctx.event("business.transition", dict(origin="memory_writer.input",
            experience_ref=experience_ref, public_message_ref=journal.blobs.put(message)))
        journal.append([event])
        history.append((registry.add("business_result", event["event_id"], roles=("writer",)), ""))
    prepared = record_prepared_request(journal, ctx, registry, "writer", template, history, kind="writer",
        configuration=dict(experience_ref=experience_ref, feedback=feedback.__dict__, instruction=instruction,
            model_config=deepcopy(model_config)), max_input_bytes=max_input_bytes)
    _end(journal, ctx)
    return prepared


def snapshot_items(blobs, state, snapshot_id):
    snapshot = state["snapshots"][snapshot_id]
    values = [blobs.get(state["versions"][snapshot["heads"][item]]) for item in snapshot["order"]]
    return [{k:value[k] for k in ("id", "version", "title", "body")} for value in values]


class SnapshotBudgetExceeded(ContractError):
    code = "SNAPSHOT_BUDGET_EXCEEDED"


def snapshot_replay_input(blobs, state, decision_ref, snapshot_id, *, policy, instruction, max_input_bytes, context_reader=None):
    """Inspect the complete snapshot and rendered request without changing a bank."""
    read = context_reader.read if context_reader is not None else read_recorded_context
    source = replay_decision(blobs, decision_ref, read)
    original = source["wire"]
    items = snapshot_items(blobs, state, snapshot_id)
    if policy.write_timing != "none" or policy.same_episode_read:
        raise ContractError("Prepared replay uses a frozen read-only starting view")
    expected = request_with_past_memory(original, policy, items, instruction=instruction)
    if max_input_bytes is not None and (type(max_input_bytes) is not int or max_input_bytes < 1):
        raise ContractError("Preparation input cap must be positive or explicitly unresolved")
    if policy.max_return_bytes is not None and len(canonical(dict(items=items))) > policy.max_return_bytes:
        raise SnapshotBudgetExceeded("Snapshot exceeds the declared return-byte budget")
    payload = sum(len(canonical({k:item[k] for k in ("title", "body")})) for item in items)
    if ((policy.max_items is not None and len(items) > policy.max_items)
            or (policy.max_payload_bytes is not None and payload > policy.max_payload_bytes)):
        raise SnapshotBudgetExceeded("Snapshot exceeds the declared bank budget")
    if any(t["function"]["name"].startswith("memory_") for t in original.get("tools", [])):
        raise ContractError("Recorded source already contains memory tools")
    return dict(source=source, request=expected, input_bytes=len(canonical(expected)),
        fits_input_limit=None if max_input_bytes is None else len(canonical(expected)) <= max_input_bytes)


def prepare_snapshot_replay(journal, decision_ref, snapshot_id, *, policy, instruction,
                            condition_id, max_input_bytes=None):
    if journal.contract_version != "1.2":
        raise ContractError("Snapshot replay preparation requires the existing v1.2 contract")
    planned = snapshot_replay_input(journal.blobs, journal.state, decision_ref, snapshot_id,
        policy=policy, instruction=instruction, max_input_bytes=max_input_bytes)
    source, expected = planned["source"], planned["request"]
    ctx = RunContext(journal.experiment_id, new_id("prep"), new_id("s"), condition_id,
        "diagnostic", "solve", source["archive"]["record_kind"])
    memory = MemoryService(journal, record_kind=ctx.record_kind)
    bank = memory.create_bank(ctx.run_id, policy, snapshot_id)
    _start(journal, ctx, source["archive"]["task_ref"], source["archive"]["source_run_id"], bank=bank, parent=snapshot_id)
    preload = memory.preload(ctx, bank)
    registry = SourceRegistry(journal, ctx)
    template = registry.template({k:v for k,v in expected.items() if k != "messages"}, roles=("worker",))
    entries = []
    for index, message in enumerate(expected["messages"]):
        if index == 1:
            entries.append((registry.add("memory_snapshot", preload["event_id"]), ""))
        else:
            source_id = registry.template(dict(message=message), roles=("worker",))
            entries.append((source_id, "/message"))
    prepared = record_prepared_request(journal, ctx, registry, "worker", template, entries, kind="snapshot_replay",
        configuration=dict(decision_ref=decision_ref, snapshot_id=snapshot_id, policy=policy.__dict__,
            instruction=instruction), max_input_bytes=max_input_bytes)
    if journal.blobs.get(prepared["request_ref"]) != expected:
        raise ContractError("Prepared replay differs from the shared native context renderer")
    final_snapshot = memory.freeze(bank)
    memory.seal(bank)
    _end(journal, ctx, final_snapshot)
    return prepared
