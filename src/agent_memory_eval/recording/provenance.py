"""Construct a request and its provenance together, before any transport."""

from copy import deepcopy
import json
import math

from agent_memory_eval.contracts.core import ContractError, canonical, digest, new_id, pointer, read_json
from agent_memory_eval.contracts.validation import resolve_source, validate_manifest


def json_string_ranges(text):
    """Return exact escaped string value ranges, including repeated identical values."""
    read_json(text.encode())  # reject duplicate keys before locating fields
    decoder, result = json.JSONDecoder(), {}

    def space(i):
        while i < len(text) and text[i].isspace():
            i += 1
        return i

    def visit(i, location):
        i = space(i)
        if text[i] == '"':
            _, end = decoder.raw_decode(text, i)
            result[location] = (i + 1, end - 1)
            return end
        if text[i] == "{":
            i = space(i + 1)
            while text[i] != "}":
                key, end = decoder.raw_decode(text, i)
                i = visit(space(end) + 1, location + "/" + key.replace("~", "~0").replace("/", "~1"))
                i = space(i)
                if text[i] == ",":
                    i = space(i + 1)
            return i + 1
        if text[i] == "[":
            i, index = space(i + 1), 0
            while text[i] != "]":
                i = space(visit(i, location + f"/{index}"))
                index += 1
                if text[i] == ",":
                    i = space(i + 1)
            return i + 1
        return decoder.raw_decode(text, i)[1]

    visit(0, "")
    return result


def source_spans(kind, value, producer):
    result = []
    if kind in {"memory_return", "memory_snapshot"}:
        content = read_json(value["content"].encode())
        ranges = json_string_ranges(value["content"])
        items = [(f"/items/{i}", item) for i, item in enumerate(content.get("items", []))]
        if "item" in content:
            items.append(("/item", content["item"]))
        for location, item in items:
            for field in ("title", "body"):
                if field in item:
                    begin, end = ranges[location + "/" + field]
                    result.append(dict(pointer="/content", char_start=begin, char_end=end,
                                       item_id=item["id"], version_id=item["version"], field=field))
    if kind == "self_authored":
        for index, call in enumerate(value.get("tool_calls", [])):
            if call["function"]["name"] != "memory_store":
                continue
            text = call["function"]["arguments"]
            try:
                ranges = json_string_ranges(text)
            except (ValueError, TypeError, IndexError):
                continue
            for field in ("title", "body"):
                if "/" + field in ranges:
                    begin, end = ranges["/" + field]
                    if end > begin:
                        result.append(dict(pointer=f"/tool_calls/{index}/function/arguments",
                            char_start=begin, char_end=end, item_id=None, version_id=None, field=field))
    return result


def exposure_payload(manifest, span, source):
    key = {"memory_return": "returned_event_id", "self_authored": "response_event_id",
           "memory_snapshot": "preloaded_event_id"}[source["kind"]]
    return dict(request_id=manifest["request_id"], manifest_span_id=span["span_id"],
        source=dict(kind=source["kind"], **{key: source["producer_event_id"]}), request_ref=manifest["request_ref"],
        **{k: span[k] for k in ("item_id", "version_id", "field", "pointer", "char_start", "char_end", "rendered_sha256")})


class SourceRegistry:
    def __init__(self, journal, ctx):
        self.journal, self.ctx = journal, ctx
        self.sources, self.approved_templates = {}, []

    def template(self, value, roles=("worker", "writer")):
        ref = self.journal.blobs.put(value)
        self.approved_templates.append(ref)
        source_id = new_id("source")
        self.sources[source_id] = dict(kind="template", run_id=None, roles=list(roles),
                                      artifact_ref=ref, pointer="", memory_spans=[])
        return source_id

    def add(self, kind, event_id, *, location="", roles=("worker", "writer")):
        event = self.journal.events[event_id]
        fields = {"task_initial": ("run.started", "task_ref"),
                  "business_result": ("business.transition", "public_message_ref"),
                  "self_authored": ("model.request_finished", "response_ref"),
                  "memory_return": ("memory.returned", "result_ref"),
                  "memory_snapshot": ("memory.preloaded", "public_message_ref"),
                  "policy_result": ("policy.denied", "tool_result_ref"),
                  "benchmark_message": ("business.transition", "model_message_ref")}
        if kind not in fields:
            raise ContractError("Unsupported source producer")
        event_type, field = fields[kind]
        if event["type"] != event_type or event["run_id"] != self.ctx.run_id:
            raise ContractError("Source producer does not belong to this run")
        if kind == "memory_snapshot":
            roles = ("worker",)
        ref = event["payload"][field]
        value = pointer(self.journal.blobs.get(ref), location)
        source_id = new_id("source")
        self.sources[source_id] = dict(kind=kind, run_id=self.ctx.run_id, roles=list(roles),
            artifact_ref=ref, producer_event_id=event_id, pointer=location,
            memory_spans=source_spans(kind, value, event))
        return source_id

    def value(self, source_id, actor, before_seq):
        # Use the same semantic checks when constructing and when auditing the request.
        event = dict(run_id=self.ctx.run_id, event_seq=before_seq, payload=dict(actor_role=actor))
        return resolve_source(self.journal.root, self.sources[source_id], event,
                              self.journal.events, self.approved_templates)

    def prepare(self, ctx, actor, template, messages):
        if ctx.run_id != self.ctx.run_id or ctx.session_id != self.ctx.session_id:
            raise ContractError("A registry cannot cross sessions")
        request_id = new_id("request")
        seq = len(self.journal.events) + 1
        config = self.value(template, actor, seq)
        request = deepcopy({key: value for key, value in config.items() if key not in {"system", "messages"}})
        request["messages"] = []
        bindings = [dict(pointer="/" + field, source_id=template, source_pointer="/" + field)
                    for field in request if field != "messages"]
        memory_spans = []
        for index, (source_id, source_pointer) in enumerate(messages):
            source = self.sources[source_id]
            value = self.value(source_id, actor, seq)
            request["messages"].append(deepcopy(pointer(value, source_pointer)))
            target = f"/messages/{index}"
            bindings.append(dict(pointer=target, source_id=source_id, source_pointer=source_pointer))
            for original in source["memory_spans"]:
                if source_pointer and not original["pointer"].startswith(source_pointer + "/"):
                    continue
                span = deepcopy(original)
                span["pointer"] = target + span["pointer"][len(source_pointer):]
                text = pointer(request, span["pointer"])[span["char_start"]:span["char_end"]]
                span.update(span_id=f"{request_id}:m{len(memory_spans)}", source_id=source_id,
                            rendered_sha256=digest(text.encode()))
                memory_spans.append(span)
        request_ref = self.journal.blobs.put(request)
        version = "1.2" if any(self.sources[s]["kind"] == "memory_snapshot" for s, _ in messages) else "1.1"
        manifest = dict(schema_version=version, request_id=request_id, run_id=ctx.run_id, actor_role=actor,
                        request_ref=request_ref, bindings=bindings, memory_spans=memory_spans)
        event = ctx.event("model.request_started", dict(request_id=request_id, actor_role=actor,
            request_ref=request_ref, input_manifest_ref=self.journal.blobs.put(manifest), delivery_status="prepared"))
        event["event_seq"] = seq
        validate_manifest(self.journal.root, manifest, event, self.sources, self.journal.events,
                          self.approved_templates, self.journal.checks)
        return request, manifest, event


class RequestLimit(ContractError):
    pass


class RecordingGateway:
    """Record manifest-checked requests around a transport, including observed usage."""
    synthetic_only = False
    def __init__(self, journal):
        self.journal = journal

    def call(self, ctx, registry, actor, template, messages, worker, *, max_requests, cancel=False, transform=None):
        if type(max_requests) is not int or max_requests < 1:
            raise ContractError("Explicit request limit required")
        count = sum(e["type"] == "model.request_started" and e["run_id"] == ctx.run_id
                    for e in self.journal.events.values())
        if count >= max_requests:
            self.journal.append([ctx.event("runtime.error", dict(code="REQUEST_LIMIT"))])
            raise RequestLimit("Policy-side request limit reached")
        offline = bool(getattr(worker, "offline", False))
        if self.synthetic_only and not offline:
            raise ContractError("This gateway accepts synthetic transports only")
        if ctx.record_kind != ("illustrative" if offline else "observed"):
            raise ContractError("Transport and record kind differ")
        request, manifest, start = registry.prepare(ctx, actor, template, messages)
        if offline and request["model"] != "synthetic-offline":
            raise ContractError("An offline request must identify its synthetic model")
        final_wire = deepcopy(request) if transform is None else transform(deepcopy(request))
        if canonical(final_wire) != canonical(request):
            self.journal.append([ctx.event("runtime.error", dict(code="UNAPPROVED_WIRE_CHANGE"))])
            raise ContractError("Final request differs from its approved manifest")
        events = [start]
        for span in manifest["memory_spans"]:
            source = registry.sources[span["source_id"]]
            exposure = ctx.event("memory.input_included", exposure_payload(manifest, span, source),
                                 causes=[start["event_id"], source["producer_event_id"]])
            if source["kind"] == "memory_snapshot":
                exposure["schema_version"] = "1.2"
            events.append(exposure)
        state = deepcopy(self.journal.state)
        state["provenance"][manifest["request_id"]] = dict(sources_ref=self.journal.blobs.put(registry.sources),
                                                          approved_templates=deepcopy(registry.approved_templates))
        self.journal.append(events, state=state)
        status, delivery, ref, error = "error", "not_submitted", None, None
        usage, cost = None, None
        cost_status = "not_applicable" if offline else "unknown"
        response = None
        if not cancel:
            try:
                response = worker.respond(deepcopy(final_wire))
                ref = self.journal.blobs.put(response)
                delivery = "response_received"
                usage = deepcopy(response.get("usage"))
                raw_cost = usage.get("cost") if isinstance(usage, dict) else None
                if type(raw_cost) in (int, float) and math.isfinite(raw_cost) and raw_cost >= 0:
                    cost, cost_status = raw_cost, "observed"
                if response["choices"][0]["message"]["role"] != "assistant":
                    raise ContractError("Transport must return an assistant message")
                status, delivery = "response", "response_received"
            except Exception as caught:
                status = "timeout" if isinstance(caught, TimeoutError) else "error"
                error = caught
                if getattr(caught, "response", None) is not None:
                    ref = self.journal.blobs.put(caught.response)
                    delivery = "response_received"
                elif getattr(caught, "submitted", None) is False:
                    delivery = "not_submitted"
                elif getattr(caught, "submitted", None) is True:
                    delivery = "response_received"
                elif ref is None:
                    delivery = "unknown"
        if delivery == "not_submitted" and cost is None:
            cost_status = "not_applicable"
        finish = ctx.event("model.request_finished", dict(request_id=manifest["request_id"], status=status,
            delivery_status=delivery, response_ref=ref, usage=usage, cost_usd=cost,
            cost_status=cost_status), causes=[start["event_id"]])
        self.journal.append([finish])
        if error:
            raise error
        if cancel:
            return None, None, manifest["request_id"]
        roles = ("worker", "writer") if actor in {"worker", "writer"} else (actor,)
        source = registry.add("self_authored", finish["event_id"], location="/choices/0/message", roles=roles)
        return response["choices"][0]["message"], source, manifest["request_id"]


class OfflineGateway(RecordingGateway):
    """Preserve the original synthetic-only entry point."""
    synthetic_only = True
