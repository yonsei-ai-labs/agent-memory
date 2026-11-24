"""Trace cited memory clauses and count observed changes on binary outcome pairs."""

from pathlib import Path

from agent_memory_eval.analysis.memory_writing import audit_memory_writes
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import leaves, pointer, validate_manifest
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from agent_memory_eval.recording.provenance import exposure_payload, public_assistant_message


def paired_counts(pairs):
    """Count allocated (memory, control) pairs for one named outcome, including unobserved pairs."""
    labels = {(True, False): "gain", (False, True): "loss",
              (True, True): "both_success", (False, False): "both_failure"}
    result = dict.fromkeys([*labels.values(), "missing"], 0)
    for memory, control in pairs:
        if any(x is not None and type(x) is not bool for x in (memory, control)):
            raise ContractError("Expected binary outcomes or missing values")
        result["missing" if None in (memory, control) else labels[memory, control]] += 1
    return dict(assigned=sum(result.values()), **result)


def _one(values, description):
    matches = list(values)
    if len(matches) != 1:
        raise ContractError(f"Expected one {description}, found {len(matches)}")
    return matches[0]


def _citation(document, location, quote):
    if not isinstance(location, str) or not location.startswith("/") or location not in leaves(document):
        raise ContractError(f"Invalid citation pointer {location}")
    value = pointer(document, location)
    if not isinstance(value, str) or not isinstance(quote, str) or not quote or value.count(quote) != 1:
        raise ContractError(f"Missing or ambiguous excerpt at {location}")
    start = value.index(quote)
    return dict(pointer=location, quote=quote, char_start=start, char_end=start + len(quote))


def _writer_citation(supplied, location, quote):
    citation = _citation(supplied, location, quote)
    parts = location.split("/")
    if parts[1:3] == ["experience", "messages"]:
        message = supplied["experience"]["messages"][int(parts[3])]
        citation.update(source_kind="past_message", role=message["role"], message_sha256=digest(canonical(message)))
    elif parts[1:3] == ["experience", "task_context"]:
        citation["source_kind"] = "past_task_context"
    elif parts[1] in ("instruction", "feedback"):
        citation["source_kind"] = parts[1]
    else:
        raise ContractError("Unsupported source citation in the writer input")
    return citation


def _storage_call(blobs, events, store):
    run = store["run_id"]
    for event in events:
        p = event["payload"]
        if (event["type"] != "model.request_finished" or event["run_id"] != run
                or event["event_seq"] >= store["event_seq"] or p["status"] != "response"):
            continue
        calls = blobs.get(p["response_ref"])["choices"][0]["message"].get("tool_calls") or []
        for i, call in enumerate(calls):
            # The operation ID identifies the call even if identical text was written twice.
            if "op_" + digest(canonical([run, p["request_id"], call["id"], i])) == store["payload"]["operation_id"]:
                return p, call
    raise ContractError("No provider storage call produced the cited version")


def _writing_evidence(folder, item, version, field, clause, citations):
    writing_audit = audit_memory_writes(folder)
    scan, state, _ = read_experiment(folder)
    blobs = BlobStore(folder)
    events = scan["events"].values()
    store = _one((e for e in events if e["type"] == "memory.stored"
                  and (e["payload"]["item_id"], e["payload"]["version_id"]) == (item, version)), "stored version")
    run = store["run_id"]
    if not any(w["run_id"] == run for w in writing_audit["writes"]):
        raise ContractError("Stored version lacks an audited post-task writer")
    stored = store["payload"]
    memory = blobs.get(stored["body_ref"])
    end = _one((e for e in events if e["type"] == "run.ended" and e["run_id"] == run), "writer end")
    frozen = end["payload"]["end_snapshot_id"]
    if field not in {"title", "body"} or (memory["id"], memory["version"]) != (item, version):
        raise ContractError("Stored field or version differs")
    if state["snapshots"][frozen]["heads"].get(item) != version:
        raise ContractError("Cited version is absent from the author's frozen bank")
    note = _citation(memory, "/" + field, clause)
    p, call = _storage_call(blobs, events, store)
    args = read_json(call["function"]["arguments"].encode())
    if (call["function"]["name"] != "memory_store" or args["title"] != memory["title"]
            or args["body"] != memory["body"] or args.get("expected_version") != memory["parent_version_id"]
            or args.get("id") != (item if memory["parent_version_id"] else None)):
        raise ContractError("Provider storage call differs from the saved version")
    request = _one((e for e in events if e["type"] == "model.request_started" and e["run_id"] == run
                    and e["payload"]["request_id"] == p["request_id"]), "authoring request")
    wire = blobs.get(request["payload"]["request_ref"])
    system, experience, feedback = wire["messages"][:3]
    supplied = dict(instruction=system["content"], experience=read_json(experience["content"].encode()),
                    feedback=read_json(feedback["content"].encode()))
    if supplied["experience"]["kind"] != "past_public_experience":
        raise ContractError("Writer lacks its public source")
    return dict(writer_run=run, record_kind=store["record_kind"], model=wire["model"],
                source_runs=scan["starts"][run]["payload"]["source_run_ids"],
                experience_sha256=state["memory_writes"][run]["experience_ref"]["sha256"],
                public_context_sha256=digest(canonical(supplied["experience"])),
                instruction_sha256=digest(system["content"].encode()), feedback=supplied["feedback"],
                request_id=p["request_id"], request_sha256=request["payload"]["request_ref"]["sha256"],
                response_sha256=p["response_ref"]["sha256"], store_event=store["event_id"],
                snapshot_id=frozen, item=item, version=version, field_sha256=digest(memory[field].encode()), note=note,
                source_citations=[_writer_citation(supplied, path, text) for path, text in citations]), memory


def _current_citation(wire, bindings, sources, location, quote):
    binding = _one((b for b in bindings if location == b["pointer"] or location.startswith(b["pointer"] + "/")),
                   "source binding for current evidence")
    origin = sources[binding["source_id"]]
    if origin["kind"] not in {"benchmark_message", "business_result", "task_initial"}:
        raise ContractError("Current public evidence points to another input source")
    return dict(**_citation(wire, location, quote), source_kind=origin["kind"],
                producer_event=origin["producer_event_id"])


def _exposed_clause(wire, span, field_text, note):
    # The submitted message embeds the fields as JSON strings, so escaping changes clause offsets.
    container = pointer(wire, span["pointer"])
    begin, end = span["char_start"], span["char_end"]
    if container[begin:end] != canonical(field_text).decode()[1:-1]:
        raise ContractError("Exposed field differs from the cited immutable version")
    prefix = canonical(field_text[:note["char_start"]]).decode()[1:-1]
    quoted = canonical(note["quote"]).decode()[1:-1]
    clause_begin = begin + len(prefix)
    if container[clause_begin:clause_begin + len(quoted)] != quoted:
        raise ContractError("Quoted clause differs in the provider input")
    return dict(source=span["source"], pointer=span["pointer"], char_start=begin, char_end=end,
                rendered_sha256=span["rendered_sha256"], clause_char_start=clause_begin,
                clause_char_end=clause_begin + len(quoted))


def trace_claim(writer_folder, worker_folder, *, item, version, field, clause, source_citations,
                request_id, current_citations=(), action_citations=()):
    """Check literal evidence. Applicability, semantic agreement and policy effects are separate judgments.

    Source pointers address the actual writer's experience, feedback or instruction. Current pointers
    address the worker wire. Action pointers address only the public assistant reply. An empty exposure
    list means this field of this version did not reach this request through a recorded memory channel.
    """
    worker_folder = Path(worker_folder)
    writer, memory = _writing_evidence(Path(writer_folder), item, version, field, clause, source_citations)
    scan, _, _ = read_experiment(worker_folder)
    blobs = BlobStore(worker_folder)
    record, request = _one(((row, e) for row in scan["records"] for e in row["events"]
                            if e["type"] == "model.request_started" and e["payload"]["request_id"] == request_id),
                           "decision request")
    run = request["run_id"]
    start = scan["starts"][run]
    p = request["payload"]
    state = blobs.get(record["state_ref"])
    snapshot_id = start["payload"]["start_snapshot_id"]
    snapshot = state["snapshots"].get(snapshot_id, {})
    declared_writer = writer["writer_run"] in start["payload"]["source_run_ids"]
    owned_snapshot = (snapshot_id == writer["snapshot_id"]
                      and state["banks"][snapshot["bank_id"]]["owner_run"] == writer["writer_run"])
    if (p["actor_role"] != "worker" or not (declared_writer or owned_snapshot)
            or snapshot.get("heads", {}).get(item) != version):
        raise ContractError("Selected decision did not receive the assigned writer's frozen bank")
    assigned = blobs.get(state["versions"][version])
    if any(assigned[k] != memory[k] for k in ("id", "version", "title", "body")):
        raise ContractError("The imported memory differs from the original writer's version")
    finish = _one((e for e in scan["events"].values() if e["type"] == "model.request_finished"
                   and e["run_id"] == run and e["payload"]["request_id"] == request_id), "decision response")
    if finish["payload"]["status"] != "response" or finish["event_seq"] <= request["event_seq"]:
        raise ContractError("The decision requires a completed model response")
    wire = blobs.get(p["request_ref"])
    response_ref = finish["payload"]["response_ref"]
    message = public_assistant_message(blobs.get(response_ref)["choices"][0]["message"])
    manifest = blobs.get(p["input_manifest_ref"])
    origin = state["provenance"][request_id]
    sources = blobs.get(origin["sources_ref"])
    spans = validate_manifest(worker_folder, manifest, request, sources, scan["events"], origin["approved_templates"])
    expected = [exposure_payload(manifest, s, sources[s["source_id"]]) for s in spans]
    logged = [e["payload"] for e in scan["events"].values() if e["type"] == "memory.input_included"
              and e["run_id"] == run and e["payload"]["request_id"] == request_id]
    if expected != logged:
        raise ContractError("Decision exposure events differ from the submitted request manifest")
    exposures = [_exposed_clause(wire, s, memory[field], writer["note"]) for s in expected
                 if (s["item_id"], s["version_id"], s["field"]) == (item, version, field)]
    return dict(writing=writer, decision=dict(
        worker_run=run, record_kind=request["record_kind"], model=wire["model"],
        starting_snapshot_id=snapshot_id, writer_lineage="declared_writer" if declared_writer else "writer_owned_snapshot",
        request_id=request_id, request_sha256=p["request_ref"]["sha256"],
        response_sha256=response_ref["sha256"], exposure=exposures,
        current_citations=[_current_citation(wire, manifest["bindings"], sources, path, text)
                           for path, text in current_citations],
        action_citations=[_citation(message, path, text) for path, text in action_citations]))
