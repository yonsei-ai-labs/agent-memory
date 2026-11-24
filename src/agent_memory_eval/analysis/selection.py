"""Reconstruct the evidence available at each worker choice from its submitted request."""

from collections import defaultdict
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import pointer, validate_manifest
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from agent_memory_eval.recording.provenance import exposure_payload, public_assistant_message


def _public_inputs(wire, manifest, sources):
    bindings = {b["pointer"]: sources[b["source_id"]] for b in manifest["bindings"]}
    inputs = []
    tool_names = {}
    for i, message in enumerate(wire["messages"]):
        role, content = message["role"], message.get("content")
        tool_names.update((c["id"], c["function"]["name"]) for c in message.get("tool_calls") or [])
        if role not in {"user", "tool", "system"} or not isinstance(content, str):
            continue
        path = f"/messages/{i}"
        source = next((bindings[p] for p in (path + "/content", path, "/messages", "") if p in bindings), None)
        if source is None or source["kind"] not in {"benchmark_message", "business_result", "task_initial", "template"}:
            continue
        entry = dict(pointer=path + "/content", role=role, source=source["kind"], sha256=digest(content.encode()),
                     producer_event=source.get("producer_event_id"))
        if role == "user":
            entry["text"] = content
        elif role == "tool":
            entry["tool"] = tool_names.get(message.get("tool_call_id"))
        inputs.append(entry)
    return inputs


def _visible_fields(wire, spans, sources, events):
    fields = []
    for span in spans:
        source = sources[span["source_id"]]
        event = events[source["producer_event_id"]]
        via = event["payload"]["operation"] if source["kind"] == "memory_return" else source["kind"]
        field = {k: span[k] for k in ("item_id", "version_id", "field", "pointer",
                                      "char_start", "char_end", "rendered_sha256")}
        field.update(via=via, producer_event=event["event_id"])
        if field["field"] == "title" and source["kind"] in {"memory_return", "memory_snapshot"}:
            encoded = pointer(wire, field["pointer"])[field["char_start"]:field["char_end"]]
            field["title"] = read_json(('"' + encoded + '"').encode())
        fields.append(field)
    fields.sort(key=lambda f: (int(f["pointer"].split("/")[2]), f["pointer"], f["char_start"]))
    list_positions = defaultdict(int)
    for field in fields:
        if field["field"] == "title" and field["via"] == "list":
            list_positions[field["pointer"]] += 1
            field["position_on_page"] = list_positions[field["pointer"]]
    return fields


def worker_choices(folder, *, run_id=None, through_request=None, evidence=None):
    """Keep lookup attempts, actual returns, pre-choice input and same-response business actions distinct."""
    folder = Path(folder)
    scan, _, _ = evidence if evidence is not None else read_experiment(folder)
    events, blobs = scan["events"], BlobStore(folder)
    worker_starts = [e for e in events.values() if e["type"] == "model.request_started"
                     and e["payload"]["actor_role"] == "worker"]
    if run_id is None:
        owners = {e["run_id"] for e in worker_starts if through_request is None or
                  through_request == e["payload"]["request_id"]}
        if len(owners) != 1:
            raise ContractError("Select one worker run or one recorded worker request")
        run_id, = owners
    if run_id not in scan["starts"]:
        raise ContractError("Unknown worker run")
    finishes, returns, logged = {}, {}, defaultdict(list)
    for e in events.values():
        if e["run_id"] != run_id:
            continue
        p = e["payload"]
        if e["type"] == "model.request_finished":
            finishes[p["request_id"]] = p
        elif e["type"] == "memory.returned":
            returns[p["operation_id"]] = e
        elif e["type"] == "memory.input_included":
            logged[p["request_id"]].append(p)
    states = {e["event_id"]: record["state_ref"] for record in scan["records"] for e in record["events"]
              if e["type"] == "model.request_started" and e["run_id"] == run_id}
    choices = []
    for start in worker_starts:
        if start["run_id"] != run_id:
            continue
        p = start["payload"]
        rid = p["request_id"]
        wire, manifest = blobs.get(p["request_ref"]), blobs.get(p["input_manifest_ref"])
        origin = blobs.get(states[start["event_id"]])["provenance"][rid]
        sources = blobs.get(origin["sources_ref"])
        spans = validate_manifest(folder, manifest, start, sources, events, origin["approved_templates"])
        if [exposure_payload(manifest, s, sources[s["source_id"]]) for s in spans] != logged[rid]:
            raise ContractError("Worker choice exposure differs from its submitted request")
        finish = finishes.get(rid)
        step = dict(request_id=rid, request_ref=p["request_ref"], manifest_ref=p["input_manifest_ref"],
                    response_ref=finish and finish["response_ref"], delivery=finish["delivery_status"] if finish else "pending",
                    status=finish["status"] if finish else "pending",
                    available_reads=[t["function"]["name"] for t in wire.get("tools") or []
                                     if t["function"]["name"] in {"memory_get", "memory_list"}],
                    public_inputs=_public_inputs(wire, manifest, sources),
                    visible_memory=_visible_fields(wire, spans, sources, events), response=None, reads=[], choice="unobserved")
        if finish and finish["status"] == "response":
            message = public_assistant_message(blobs.get(finish["response_ref"])["choices"][0]["message"])
            step["response"] = message
            calls = message.get("tool_calls") or []
            names = [c["function"]["name"] for c in calls]
            memory = any(n.startswith("memory_") for n in names)
            business = any(not n.startswith("memory_") for n in names)
            public = bool(message.get("content"))
            if memory:
                step["choice"] = "mixed" if business or public else "memory_tools"
            else:
                step["choice"] = "business_tools" if business else "public_message" if public else "empty_response"
            for i, call in enumerate(calls):
                name = call["function"]["name"]
                if name not in {"memory_list", "memory_get"}:
                    continue
                op_id = "op_" + digest(canonical([run_id, rid, call["id"], i]))
                returned = returns.get(op_id)
                result = None
                if returned:
                    result = returned["payload"]
                    if result["operation"] != name.removeprefix("memory_") or blobs.get(result["result_ref"])["tool_call_id"] != call["id"]:
                        raise ContractError("Memory return differs from its worker call")
                    result = dict(event=returned["event_id"], status=result["status"], items=result["items"])
                read = dict(call_index=i, name=name, arguments=call["function"]["arguments"], returned=result)
                if name == "memory_get":
                    try:
                        args = read_json(read["arguments"].encode())
                    except ValueError:
                        args = None
                    item = args.get("id") if isinstance(args, dict) else None
                    read["requested_item_id"] = item if isinstance(item, str) else None
                    read["visible_title_indices"] = [j for j, field in enumerate(step["visible_memory"])
                                                     if read["requested_item_id"] is not None and field["item_id"] == item
                                                     and "title" in field]
                step["reads"].append(read)
        choices.append(step)
        if rid == through_request:
            break
    if through_request is not None and (not choices or choices[-1]["request_id"] != through_request):
        raise ContractError("The requested cutoff is outside this worker run")
    ended = any(e["type"] == "run.ended" and e["run_id"] == run_id for e in events.values())
    scope = "through_request" if through_request is not None else "ended_run" if ended else "recorded_prefix"
    trace = dict(format="worker-choices/v1", run_id=run_id, record_kind=scan["starts"][run_id]["record_kind"],
                 scope=scope, through_request=through_request, choices=choices)
    trace["first_body_choice"] = first_body_choice(trace)
    return trace


def first_body_choice(trace):
    """Locate the first observed get on the list page visible at that moment, including pages with one item."""
    coverage = bool(trace["choices"])
    for step in trace["choices"]:
        if step["status"] != "response":
            coverage = coverage and step["delivery"] == "not_submitted"
        for read in step["reads"]:
            if read["name"] != "memory_get":
                continue
            fields = step["visible_memory"]
            titles = [fields[i] for i in read["visible_title_indices"] if fields[i]["via"] == "list"]
            pages = [dict(position=t["position_on_page"], size=sum(f["via"] == "list" and f["field"] == "title"
                          and f["pointer"] == t["pointer"] for f in fields), pointer=t["pointer"], version=t["version_id"],
                          title=t["title"]) for t in titles]
            return dict(earlier_responses_observed=coverage, get_requested=True, request_id=step["request_id"],
                        item_id=read["requested_item_id"], matched_list_entries=pages,
                        returned_status=(read["returned"] or {}).get("status"))
    return dict(earlier_responses_observed=coverage, get_requested=False if coverage else None, matched_list_entries=[])
