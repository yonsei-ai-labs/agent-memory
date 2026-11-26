"""Trace a literal through an assigned writer and a particular worker request in the shared journal."""

import argparse
from pathlib import Path

from agent_memory_eval.analysis.action_grounding import literal_citations
from agent_memory_eval.analysis.claim_evidence import _exposed_clause, _storage_call
from agent_memory_eval.analysis.memory_writing import audit_memory_writes
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.analysis.selection import worker_choices
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.snapshot_replay import replay_response_error
from agent_memory_eval.contracts.validation import pointer, validate_manifest
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.memory.writing import public_message, read_public_experience
from agent_memory_eval.recording.journal import BlobStore, read_experiment


def _message_field(path):
    """Public text and the chosen function or its arguments, excluding transport IDs and provider metadata."""
    return path == ["content"] or (len(path) == 4 and path[0] == "tool_calls" and path[2] == "function"
                                   and path[3] in {"name", "arguments"})


def _source_matches(experience, literal):
    result = []
    for hit in literal_citations(experience, literal):
        path = hit["pointer"].split("/")[1:]
        if path[0] == "task_context" and _message_field(path[1:]):
            message = experience["task_context"]
        elif path[0] == "messages" and _message_field(path[2:]):
            message = experience["messages"][int(path[1])]
        else:
            continue
        result.append(dict(**hit, role=message["role"]))
    return result


def _input_matches(wire, manifest, sources, literal):
    result = []
    document = {k: wire[k] for k in ("messages", "tools") if k in wire}
    for hit in literal_citations(document, literal):
        path = hit["pointer"].split("/")[1:]
        if path[0] == "messages":
            if not _message_field(path[2:]):
                continue
            kind = wire["messages"][int(path[1])]["role"]
        else:
            name = wire["tools"][int(path[1])]["function"]["name"]
            kind = "memory_tool_schema" if name.startswith("memory_") else "business_tool_schema"
        origin, = [sources[b["source_id"]] for b in manifest["bindings"]
                   if hit["pointer"] == b["pointer"] or hit["pointer"].startswith(b["pointer"] + "/")]
        if origin["kind"] in {"memory_return", "memory_snapshot"}:
            continue
        result.append(dict(**hit, input_kind=kind, source=origin["kind"], producer_event=origin.get("producer_event_id")))
    return result


def writer_material(folder, blobs, scan, state, request_event):
    """Recover the writer's inputs from their bound producers in the submitted request."""
    req = request_event["payload"]
    wire = blobs.get(req["request_ref"])
    manifest = blobs.get(req["input_manifest_ref"])
    origin = state["provenance"][req["request_id"]]
    catalog = blobs.get(origin["sources_ref"])
    events = scan["events"]
    validate_manifest(folder, manifest, request_event, catalog, events, origin["approved_templates"])
    instructions, experiences, feedback = [], [], []
    for binding in manifest["bindings"]:
        path = binding["pointer"].split("/")
        if len(path) != 3 or path[1] != "messages":
            continue
        message = pointer(wire, binding["pointer"])
        producer = catalog[binding["source_id"]]
        if producer["kind"] == "template" and message["role"] == "system":
            instructions.append(message["content"])
            continue
        require(producer["kind"] == "business_result" and message["role"] == "user", "Unknown writer input origin")
        event = events[producer["producer_event_id"]]["payload"]
        require(event.get("origin") == "memory_writer.input", "Writer received an unrelated public message")
        value = read_json(message["content"].encode())
        if value["kind"] == "past_public_experience":
            experience = read_public_experience(blobs, scan, event["experience_ref"])
            require(value["task_context"] == public_message(blobs.get(experience["task_ref"])) and
                    value["messages"] == [item["message"] for item in experience["observations"]],
                    "Writer experience differs from its public source")
            experiences.append((int(path[2]), dict(run=experience["source_run_id"], context=value,
                                                   public_context_sha256=digest(message["content"].encode()))))
        else:
            require(value["kind"] == "evaluation_feedback", "Unknown writer input document")
            feedback.append(value)
    require(len(instructions) == len(feedback) == 1 and experiences, "Incomplete or ambiguous writer inputs")
    return instructions[0], [item for _, item in sorted(experiences, key=lambda pair: pair[0])], feedback[0]


def _writer_evidence(folder, evidence, run, literal, assigned):
    scan, state, _ = evidence
    blobs = BlobStore(folder)
    events = [e for e in scan["events"].values() if e["run_id"] == run]
    requests = {e["payload"]["request_id"]: e["payload"] for e in events if e["type"] == "model.request_started"}
    first = next(iter(requests.values()))
    first_event = next(e for e in events if e["type"] == "model.request_started")
    instruction, experiences, feedback = writer_material(folder, blobs, scan, state, first_event)
    end, = [e["payload"] for e in events if e["type"] == "run.ended"]
    snapshot = end["end_snapshot_id"]
    bank = snapshot_items(blobs, state, snapshot)
    stores = {(e["payload"]["item_id"], e["payload"]["version_id"]): e for e in events if e["type"] == "memory.stored"}
    items = []
    for item in bank:
        event = stores[item["id"], item["version"]]
        finish, call = _storage_call(blobs, events, event)
        args = read_json(call["function"]["arguments"].encode())
        parent = event["payload"]["parent_version_id"]
        require(call["function"]["name"] == "memory_store" and all(args.get(k) == item[k] for k in ("body", "title"))
                and args.get("expected_version") == parent and args.get("id") == (item["id"] if parent else None)
                and event["payload"]["body_ref"] == state["versions"][item["version"]],
                "Saved item differs from the writer's storage call")
        key = item["id"], item["version"]
        if key in assigned:
            require(assigned[key] == item, "Reader's memory differs from the original writer's version")
        items.append(dict(item=key[0], version=key[1], assigned=key in assigned, store_event=event["event_id"],
                          item_sha256=event["payload"]["body_ref"]["sha256"],
                          request_sha256=requests[finish["request_id"]]["request_ref"]["sha256"],
                          response_sha256=finish["response_ref"]["sha256"],
                          matches=literal_citations({k: item[k] for k in ("title", "body")}, literal)))
    require([e["run"] for e in experiences] == scan["starts"][run]["payload"]["source_run_ids"],
            "Writer input and declared source runs differ")
    require(len(experiences) == 1, "The v1 case report requires one public experience")
    original = experiences[0]
    source = dict(run=original["run"], public_context_sha256=original["public_context_sha256"],
                  matches=_source_matches(original["context"], literal))
    guidance = dict(instruction=instruction, feedback=feedback)
    return dict(run=run, condition=scan["starts"][run]["condition_id"], snapshot=snapshot, source=source,
                journal_head_sha256=scan["records"][-1]["record_sha256"],
                first_request_sha256=first["request_ref"]["sha256"], instruction_sha256=digest(instruction.encode()),
                feedback=guidance["feedback"], guidance_matches=literal_citations(guidance, literal), items=items)


def _assigned_writers(worker_folder, worker_data, writer_folders, state, run, literal):
    start = worker_data[0]["starts"][run]["payload"]
    local_snapshot = start["start_snapshot_id"]
    if local_snapshot is None:
        require(not writer_folders and not start["source_run_ids"], "This sourced run lacks a traceable starting snapshot")
        return [], None, {}
    owner = state["banks"][state["snapshots"][local_snapshot]["bank_id"]]["owner_run"]
    parents = set(start["source_run_ids"]) | {owner}
    bank = snapshot_items(BlobStore(worker_folder), state, local_snapshot)
    indexed = {(i["id"], i["version"]): i for i in bank}
    writers = {}
    for folder in sorted({Path(p).resolve() for p in writer_folders} or {worker_folder}):
        data = worker_data if folder == worker_folder else read_experiment(folder)
        runs = parents & data[1].get("memory_writes", {}).keys()
        require(runs and not runs.intersection(writers), "Writer is unrelated to this reader or supplied twice")
        audited = audit_memory_writes(folder, evidence=data)
        completed = {w["run_id"] for w in audited["writes"]
                     if w["termination_reason"] in {"memory_write_completed", "memory_write_empty"}}
        require(runs <= completed, "Writer has no completed frozen bank")
        writers.update((w, _writer_evidence(folder, data, w, literal, indexed)) for w in runs)
    linked = [(item["item"], item["version"]) for w in writers.values() for item in w["items"] if item["assigned"]]
    require(len(linked) == len(indexed) and set(linked) == indexed.keys(), "Every assigned version needs one original writer")
    assignment = state.get("assignments", {}).get(run, {})
    if "source_writer_snapshot_id" in assignment:
        original = writers.get(assignment["source_writer_run_id"])
        require(original and original["snapshot"] == assignment["source_writer_snapshot_id"]
                and [(i["item"], i["version"]) for i in original["items"]] == list(indexed),
                "Reader's imported snapshot differs from the assigned writer")
    return [writers[k] for k in sorted(writers)], local_snapshot, indexed


def _exposures(events, request_id, wire, indexed, literal):
    spans = [e["payload"] for e in events if e["type"] == "memory.input_included" and e["payload"]["request_id"] == request_id]
    exposed = []
    for span in spans:
        key = span["item_id"], span["version_id"]
        require(key in indexed, "This trace requires the assigned frozen memory version")
        field = indexed[key][span["field"]]
        rendered = pointer(wire, span["pointer"])[span["char_start"]:span["char_end"]]
        require(rendered == canonical(field).decode()[1:-1] and digest(rendered.encode()) == span["rendered_sha256"],
                "Memory field in submitted input differs from its assigned version")
        offset = field.find(literal)
        if offset >= 0:
            shown = _exposed_clause(wire, span, field, dict(quote=literal, char_start=offset))
            exposed.append(dict(item=key[0], version=key[1], field=span["field"], **shown))
    return exposed


def _action(blobs, step, literal):
    if step["status"] != "response":
        return step["status"], None
    raw = blobs.get(step["response_ref"])
    if (raw.get("local_generation") or {}).get("decode_error"):
        return "decode_error", None
    if raw["choices"][0].get("finish_reason") == "length":
        return "truncated", None
    error = replay_response_error(raw, None)
    if error:
        return error, None
    message = step["response"]
    calls = message.get("tool_calls") or []
    matches = [h for h in literal_citations(message, literal) if _message_field(h["pointer"].split("/")[1:])]
    return "observed", dict(choice=step["choice"], calls=[c["function"]["name"] for c in calls], matches=matches)


def trace_decision_cue(worker_folder, request_id, literal, *, writer_folders=()):
    """Analysis-only. Return the first exact occurrence per string field, with positions in the actual input."""
    require(isinstance(literal, str) and literal.strip(), "A cue must be a nonempty literal")
    worker_folder = Path(worker_folder).resolve()
    worker_data = read_experiment(worker_folder)
    scan, _, _ = worker_data
    trace = worker_choices(worker_folder, through_request=request_id, evidence=worker_data)
    run = trace["run_id"]
    step = trace["choices"][-1]
    blobs = BlobStore(worker_folder)
    state_ref, = [r["state_ref"] for r in scan["records"] if any(
        e["type"] == "model.request_started" and e["run_id"] == run and e["payload"]["request_id"] == request_id
        for e in r["events"])]
    state = blobs.get(state_ref)
    writers, local_snapshot, indexed = _assigned_writers(worker_folder, worker_data, writer_folders, state, run, literal)
    wire = blobs.get(step["request_ref"])
    manifest = blobs.get(step["manifest_ref"])
    sources = blobs.get(state["provenance"][request_id]["sources_ref"])
    events = [e for e in scan["events"].values() if e["run_id"] == run]
    received = step["status"] == "response"
    require(not received or step["delivery"] == "response_received", "Worker response lacks a confirmed delivery")
    exposed = _exposures(events, request_id, wire, indexed, literal) if received else None
    action_status, action = _action(blobs, step, literal)
    finish = next((e["payload"] for e in events if e["type"] == "model.request_finished"
                   and e["payload"]["request_id"] == request_id), None)
    grades = [e["payload"] for e in events if e["type"] == "outcome.scored"]
    require(len(grades) <= 1, "Ambiguous recorded outcome")
    grade = grades[0] if grades else None
    outcome = None if grade is None else dict(grading_status=grade["grading_status"], success=grade["official_success"],
                                               artifact_sha256=(grade.get("official_artifact_ref") or {}).get("sha256"))
    reads = [dict(request_id=s["request_id"], request_sha256=s["request_ref"]["sha256"],
                  visible_before_read=s["visible_memory"], calls=s["reads"]) for s in trace["choices"] if s["reads"]]
    return dict(format="agent-memory-decision-cue/v1", literal=literal, record_kind=trace["record_kind"],
                writers=writers, reader=dict(run=run, condition=scan["starts"][run]["condition_id"],
                    snapshot=local_snapshot, request_id=request_id,
                    assigned_versions=[dict(item=i, version=v) for i, v in indexed],
                    journal_head_sha256=scan["records"][-1]["record_sha256"], request_sha256=step["request_ref"]["sha256"],
                    manifest_sha256=step["manifest_ref"]["sha256"], response_sha256=(step["response_ref"] or {}).get("sha256"),
                    status=step["status"], delivery=step["delivery"], available_reads=step["available_reads"],
                    reads_through_response=reads, other_input_matches=_input_matches(wire, manifest, sources, literal) if received else None,
                    visible_fields=step["visible_memory"] if received else None, exposed_matches=exposed,
                    action_status=action_status, action=action, request_usage=finish and finish.get("usage"),
                    cost_status=finish and finish.get("cost_status"), request_cost_usd=finish and finish.get("cost_usd")),
                outcome=outcome)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True, type=Path, help="Directory containing the worker journal")
    parser.add_argument("--writer", action="append", default=[], type=Path, help="Original writer journal (repeat for a pooled bank)")
    parser.add_argument("--request", required=True, help="Exact worker request ID to examine")
    parser.add_argument("--literal", required=True, help="Case-sensitive cue to locate in public evidence")
    args = parser.parse_args()
    try:
        result = trace_decision_cue(args.worker, args.request, args.literal, writer_folders=args.writer)
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    print(canonical(result).decode())


if __name__ == "__main__":
    main()
