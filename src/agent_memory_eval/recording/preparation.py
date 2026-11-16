"""Validate and record a request draft without recording a model attempt."""

from copy import deepcopy

from agent_memory_eval.contracts.core import ContractError, canonical


def record_prepared_request(journal, ctx, registry, actor, template, messages, *, kind, configuration,
                            max_input_bytes=None):
    if max_input_bytes is not None and (type(max_input_bytes) is not int or max_input_bytes < 1):
        raise ContractError("Preparation input cap must be positive or explicitly unresolved")
    request, manifest, validation_event = registry.prepare(ctx, actor, template, messages)
    size = len(canonical(request))
    prepared = dict(format="agent-memory-request-preparation/v1", kind=kind, run_id=ctx.run_id,
        actor_role=actor, configuration_ref=journal.blobs.put(configuration),
        request_ref=manifest["request_ref"], manifest_ref=journal.blobs.put(manifest),
        validation_event_ref=journal.blobs.put(validation_event), sources_ref=journal.blobs.put(registry.sources),
        approved_templates=deepcopy(registry.approved_templates), input_bytes=size,
        max_input_bytes=max_input_bytes, fits_input_limit=None if max_input_bytes is None else size <= max_input_bytes,
        planned_memory_spans=len(manifest["memory_spans"]), execution_mode="prepare_only", model_requests=0)
    state = deepcopy(journal.state)
    state.setdefault("request_preparations", {})[ctx.run_id] = prepared
    journal.append([ctx.event("business.transition", dict(origin="request.prepared",
        preparation_ref=journal.blobs.put(prepared)))], state=state)
    return prepared
