"""Read-only checks of v1.1 contract examples; no runner, writer, or model calls."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "schemas" / "v1.1"
FIXTURE = ROOT / "tests" / "fixtures" / "contracts-v1.1"


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique_pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key}")
        value[key] = child
    return value


def read_json(data: bytes):
    return json.loads(data, object_pairs_hook=_unique_pairs)


def validators():
    schemas = {path.stem.split(".")[0]: read_json(path.read_bytes())
               for path in SCHEMAS.glob("*.schema.json")}
    registry = Registry()
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return {name: Draft202012Validator(schema, registry=registry,
                                      format_checker=FormatChecker())
            for name, schema in schemas.items()}


def checked_path(base: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Artifact path must stay inside its bundle")
    path = base / relative
    if any(part.is_symlink() for part in (path, *path.parents) if part != base.parent):
        raise ValueError("Symlink artifact")
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("Artifact outside bundle")
    return path


def artifact(base: Path, ref):
    data = checked_path(base, ref["path"]).read_bytes()
    if digest(data) != ref["sha256"]:
        raise ValueError(f"Artifact hash mismatch: {ref['path']}")
    return read_json(data)


def verify_references(base, value):
    """Check even references not consumed by later requests (for example final responses)."""
    if isinstance(value, dict):
        if {"path", "sha256", "media_type"}.issubset(value):
            nested = artifact(base, value)
            verify_references(base, nested)
        else:
            for child in value.values():
                verify_references(base, child)
    elif isinstance(value, list):
        for child in value:
            verify_references(base, child)


def pointer(value, location: str):
    if not location:
        return value
    if not location.startswith("/"):
        raise ValueError("Invalid JSON pointer")
    for part in location[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def leaves(value, location=""):
    if isinstance(value, dict) and value:
        return {p for key, child in value.items()
                for p in leaves(child, location + "/" + key.replace("~", "~0").replace("/", "~1"))}
    if isinstance(value, list) and value:
        return {p for i, child in enumerate(value) for p in leaves(child, location + f"/{i}")}
    return {location}


def record_hash(record) -> str:
    return digest(canonical({key: value for key, value in record.items()
                             if key != "record_sha256"}))


def inspect_journal(data: bytes, checks=None, *, validated_prefix=0):
    """Read a valid prefix. Never truncate, append, repair, or execute operations."""
    checks = checks or validators()
    records, events, starts, ends, requests, finished, operations = [], {}, {}, set(), {}, set(), set()
    previous, experiment = None, None
    tail = b""
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            tail = line
            break
        try:
            row = read_json(line)
            if len(records) >= validated_prefix:
                checks["journal" if row["journal_version"] == "1.0" else "journal-state"].validate(row)
            if row["commit_seq"] != len(records) + 1 or row["prev_sha256"] != previous:
                raise ValueError("Journal sequence or previous hash mismatch")
            if record_hash(row) != row["record_sha256"]:
                raise ValueError("Journal record checksum mismatch")
        except Exception as error:
            raise ValueError(f"Corrupt complete journal record {len(records) + 1}: {error}") from error
        if row.get("experiment_id") is not None:
            experiment = experiment or row["experiment_id"]
            if experiment != row["experiment_id"]:
                raise ValueError("Multiple experiments in one journal")
        operation = row["operation"]
        if operation is not None:
            if operation["operation_id"] in operations:
                raise ValueError("An operation retry must not append another commit")
            operations.add(operation["operation_id"])
        memory_ops = [event["payload"]["operation_id"] for event in row["events"]
                      if event["type"] in ("memory.stored", "memory.returned")]
        if memory_ops and (operation is None or any(op != operation["operation_id"] for op in memory_ops)):
            raise ValueError("Memory events must share their operation commit")
        returned_refs = [event["payload"]["result_ref"] for event in row["events"]
                         if event["type"] == "memory.returned"]
        if returned_refs and any(ref != operation["result_ref"] for ref in returned_refs):
            raise ValueError("Operation result differs from returned result")
        for event in row["events"]:
            if event["event_seq"] != len(events) + 1 or event["event_id"] in events:
                raise ValueError("Event sequence or duplicate ID")
            if any(cause not in events for cause in event["caused_by"]):
                raise ValueError("Missing or future causal event")
            experiment = experiment or event["experiment_id"]
            if event["experiment_id"] != experiment:
                raise ValueError("Multiple experiments in one journal")
            run = event["run_id"]
            kind, payload = event["type"], event["payload"]
            if run in ends:
                raise ValueError("Event after run ended")
            if kind == "run.started":
                if run is None or run in starts or event["session_id"] is None:
                    raise ValueError("Duplicate or absent run ID")
                if event["stage"] == "probe" and not payload["fresh_session"]:
                    raise ValueError("Probe requires a fresh session")
                if event["session_id"] in {e["session_id"] for e in starts.values()}:
                    raise ValueError("Session ID shared across runs")
                starts[run] = event
            elif run is not None and run not in starts:
                raise ValueError("Event before run start")
            if run is not None and event["session_id"] != starts[run]["session_id"]:
                raise ValueError("Event session differs from its run")
            if kind == "model.request_started":
                request = payload["request_id"]
                if request in requests:
                    raise ValueError("Duplicate request ID")
                requests[request] = event
            if kind == "model.request_finished":
                request = payload["request_id"]
                if request not in requests or request in finished or requests[request]["run_id"] != run:
                    raise ValueError("Unmatched request completion")
                finished.add(request)
            if kind == "run.ended":
                if any(start["run_id"] == run and req not in finished for req, start in requests.items()):
                    raise ValueError("Completed run has an unfinished model request")
                ends.add(run)
            events[event["event_id"]] = event
        records.append(row)
        previous = row["record_sha256"]
    return {"records": records, "events": events, "starts": starts,
            "incomplete_runs": sorted(set(starts) - ends), "tail_bytes": len(tail),
            "pending_requests": sorted(set(requests) - finished)}


def read_public_prefix(base, descriptor, events, before_seq):
    lines = checked_path(base, descriptor["path"]).read_bytes().splitlines(keepends=True)
    count = descriptor["entry_count"]
    if count > len(lines):
        raise ValueError("Public prefix is shorter than declared")
    prefix = b"".join(lines[:count])
    if digest(prefix) != descriptor["prefix_sha256"]:
        raise ValueError("Public prefix hash mismatch")
    if descriptor["through_event_seq"] >= before_seq:
        raise ValueError("Public prefix reaches a future event")
    messages = []
    for number, line in enumerate(lines[:count], 1):
        if not line.endswith(b"\n"):
            raise ValueError("Incomplete public prefix entry")
        entry = read_json(line)
        event = events[entry["event_id"]]
        if entry["seq"] != number or entry["run_id"] != descriptor["run_id"]:
            raise ValueError("Public prefix order or ownership mismatch")
        if event["run_id"] != descriptor["run_id"] or event["event_seq"] > descriptor["through_event_seq"]:
            raise ValueError("Public entry is outside the allowed run or cutoff")
        if event["type"] != "business.transition":
            raise ValueError("Unapproved public message producer")
        if artifact(base, event["payload"]["public_message_ref"]) != entry["message"]:
            raise ValueError("Public entry differs from its producer")
        messages.append(entry["message"])
    return {"messages": messages}


def resolve_source(base, source, request_event, events, approved_templates):
    run, role = request_event["run_id"], request_event["payload"]["actor_role"]
    if role not in source["roles"] or source["run_id"] not in (None, run):
        raise ValueError("Source role or run is not allowed")
    kind = source["kind"]
    if kind == "public_experience":
        if source["prefix"]["run_id"] != run:
            raise ValueError("Public experience belongs to another run")
        return read_public_prefix(base, source["prefix"], events, request_event["event_seq"])
    if kind == "template":
        if source["artifact_ref"] not in approved_templates:
            raise ValueError("Unapproved template")
    else:
        event = events[source["producer_event_id"]]
        if event["run_id"] != run or event["event_seq"] >= request_event["event_seq"]:
            raise ValueError("Source is from another run or a future event")
        allowed = {"task_initial": ("run.started", "task_ref"),
                   "self_authored": ("model.request_finished", "response_ref"),
                   "memory_return": ("memory.returned", "result_ref"),
                   "business_result": ("business.transition", "public_message_ref"),
                   "policy_result": ("policy.denied", "tool_result_ref"),
                   "benchmark_message": ("business.transition", "model_message_ref")}
        if kind not in allowed:
            raise ValueError("Unsupported source kind")
        expected_type, field = allowed[kind]
        if event["type"] != expected_type or event["payload"][field] != source["artifact_ref"]:
            raise ValueError("Source does not match its recorded producer")
        if kind == "benchmark_message" and event["payload"].get("actor_role") != role:
            raise ValueError("Benchmark message has a different recipient role")
    return pointer(artifact(base, source["artifact_ref"]), source.get("pointer", ""))


def check_source_memory(source, value, events):
    spans = source.get("memory_spans", [])
    if source["kind"] == "memory_return":
        event = events[source["producer_event_id"]]
        expected = {(item["item_id"], item["version_id"], field)
                    for item in event["payload"]["items"] for field in item["fields"]}
        actual = {(span["item_id"], span["version_id"], span["field"]) for span in spans}
        if actual != expected or len(actual) != len(spans):
            raise ValueError("Memory source fields are not completely annotated")
        result = read_json(value["content"].encode("utf-8"))
        items = result.get("items", [result["item"]] if "item" in result else [])
        for span in spans:
            item = next(item for item in items if item["id"] == span["item_id"]
                        and item["version"] == span["version_id"])
            encoded = pointer(value, span["pointer"])[span["char_start"]:span["char_end"]]
            if json.loads('"' + encoded + '"') != item[span["field"]]:
                raise ValueError("Memory span does not identify its returned field")
    elif source["kind"] == "self_authored":
        calls = value.get("tool_calls", [])
        expected = set()
        for i, call in enumerate(calls):
            if call["function"]["name"] != "memory_store":
                continue
            try:
                arguments = read_json(call["function"]["arguments"].encode())
            except (ValueError, TypeError, AttributeError):
                continue
            if isinstance(arguments, dict):
                expected.update((f"/tool_calls/{i}/function/arguments", field)
                                for field in ("title", "body")
                                if isinstance(arguments.get(field), str) and arguments[field])
        actual = {(span["pointer"], span["field"]) for span in spans}
        if actual != expected or len(actual) != len(spans):
            raise ValueError("Self-authored store arguments are not completely annotated")
        for span in spans:
            if span["item_id"] is not None or span["version_id"] is not None:
                raise ValueError("Uncommitted self-authored text must not invent memory IDs")
            text = pointer(value, span["pointer"])
            encoded = text[span["char_start"]:span["char_end"]]
            if json.loads('"' + encoded + '"') != read_json(text.encode())[span["field"]]:
                raise ValueError("Self-authored span is not its named argument")
    elif spans:
        raise ValueError("Memory spans require a memory return or a self-authored producer")


def validate_public_return(wire, payload):
    result = read_json(wire["content"].encode())
    def contains_revision(value):
        if isinstance(value, dict):
            return "head_revision" in value or any(contains_revision(child) for child in value.values())
        if isinstance(value, list):
            return any(contains_revision(child) for child in value)
        return False
    if contains_revision(result):
        raise ValueError("Internal bank revision leaked in a public return")
    items = result.get("items", [result["item"]] if "item" in result else [])
    if payload["operation"] == "list" and any(set(item) - {"id", "title", "version"} for item in items):
        raise ValueError("List returned content beyond its public fields")
    expected = [{"item_id": item["id"], "version_id": item["version"],
                 "fields": [field for field in ("title", "body") if field in item]}
                for item in items]
    if expected != payload["items"]:
        raise ValueError("Returned item annotations differ from the public result")


def validate_manifest(base, manifest, request_event, catalog, events, approved_templates, checks=None):
    checks = checks or validators()
    checks["input-manifest"].validate(manifest)
    payload = request_event["payload"]
    if (manifest["request_id"], manifest["run_id"], manifest["actor_role"], manifest["request_ref"]) != (
            payload["request_id"], request_event["run_id"], payload["actor_role"], payload["request_ref"]):
        raise ValueError("Manifest does not belong to this request")
    request = artifact(base, manifest["request_ref"])
    if any(key in request for key in ("previous_response_id", "conversation", "thread_id")):
        raise ValueError("The initial stateless profile does not support implicit provider state")
    covered, derived = set(), []
    for binding in manifest["bindings"]:
        source = catalog[binding["source_id"]]
        value = resolve_source(base, source, request_event, events, approved_templates)
        check_source_memory(source, value, events)
        source_value = pointer(value, binding["source_pointer"])
        target = pointer(request, binding["pointer"])
        if canonical(source_value) != canonical(target):
            raise ValueError("Request subtree differs from its approved source")
        paths = leaves(target, binding["pointer"])
        if covered & paths:
            raise ValueError("Overlapping request provenance")
        covered.update(paths)
        for span in source.get("memory_spans", []):
            prefix = binding["source_pointer"]
            if prefix and span["pointer"] != prefix and not span["pointer"].startswith(prefix + "/"):
                continue
            item = deepcopy(span)
            item["pointer"] = binding["pointer"] + span["pointer"][len(prefix):]
            text = pointer(request, item["pointer"])
            begin, end = item["char_start"], item["char_end"]
            if not 0 <= begin < end <= len(text):
                raise ValueError("Invalid rendered memory range")
            item.update(span_id=f"{manifest['request_id']}:m{len(derived)}",
                        source_id=binding["source_id"], rendered_sha256=digest(text[begin:end].encode()))
            derived.append(item)
    if covered != leaves(request):
        raise ValueError("Request contains an uncovered field")
    if derived != manifest["memory_spans"]:
        raise ValueError("Memory span manifest is missing or inconsistent")
    return derived


def validate_batch(batch):
    """Validate a recorded synthetic dispatch trace; do not execute any tool."""
    calls = batch["calls"]
    ids = [call["id"] for call in calls]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate tool call ID")
    stop = any(call["name"] in batch["configured_stop_tools"] for call in calls)
    rejected = stop and len(calls) > 1
    if batch["result_order"] != ids:
        raise ValueError("Tool results must correspond to original calls")
    if rejected:
        if batch["disposition"] != "reject_all" or batch["execution_order"]:
            raise ValueError("Stop preflight must reject every call before any effect")
    elif batch["disposition"] != "execute" or batch["execution_order"] != ids:
        raise ValueError("Execution order differs from call order")


def validate_fixture(base=FIXTURE):
    base = Path(base)
    checks = validators()
    bundle = read_json((base / "fixture.json").read_bytes())
    catalog = artifact(base, bundle["sources_ref"])
    scan = inspect_journal(checked_path(base, bundle["journal_path"]).read_bytes(), checks)
    if scan["tail_bytes"] or scan["incomplete_runs"] or scan["pending_requests"]:
        raise ValueError("This closed illustrative fixture must have complete records")
    events, starts = scan["events"], scan["starts"]
    if any(event["record_kind"] != "illustrative" for event in events.values()):
        raise ValueError("Contract fixture must stay illustrative")
    expected, actual, snapshots, owners = {}, {}, {}, {}
    for record in scan["records"]:
        if record["operation"] is not None:
            artifact(base, record["operation"]["result_ref"])
        for event in record["events"]:
            payload, kind = event["payload"], event["type"]
            verify_references(base, payload)
            if kind == "bank.snapshot_created":
                snapshots[payload["snapshot_id"]] = artifact(base, payload["manifest_ref"])
            if kind == "run.started" and event["stage"] == "probe":
                snapshot = snapshots[payload["start_snapshot_id"]]
                if snapshot["snapshot_id"] != payload["start_snapshot_id"]:
                    raise ValueError("Snapshot identity mismatch")
            if kind == "memory.stored":
                if payload["bank_id"] != starts[event["run_id"]]["payload"]["bank_id"]:
                    raise ValueError("Memory store belongs to another bank")
                memory = artifact(base, payload["body_ref"])
                if digest(memory["body"].encode()) != payload["body_sha256"]:
                    raise ValueError("Stored body hash mismatch")
                read_public_prefix(base, payload["public_context_prefix"], events, event["event_seq"])
            if kind == "memory.returned":
                run = starts[event["run_id"]]["payload"]
                if payload["bank_id"] != run["bank_id"]:
                    raise ValueError("Memory return belongs to another bank")
                if run["start_snapshot_id"] is not None:
                    heads = snapshots[run["start_snapshot_id"]]["heads"]
                    if any(heads.get(item["item_id"]) != item["version_id"] for item in payload["items"]):
                        raise ValueError("Returned item is outside the fixed read view")
                wire = artifact(base, payload["result_ref"])
                validate_public_return(wire, payload)
            if kind == "model.request_started":
                manifest = artifact(base, payload["input_manifest_ref"])
                spans = validate_manifest(base, manifest, event, catalog, events, bundle["approved_templates"], checks)
                for span in spans:
                    source = catalog[span["source_id"]]
                    origin_key = "returned_event_id" if source["kind"] == "memory_return" else "response_event_id"
                    expected[span["span_id"]] = {
                        "request_id": payload["request_id"], "item_id": span["item_id"],
                        "version_id": span["version_id"], "manifest_span_id": span["span_id"],
                        "source": {"kind": source["kind"], origin_key: source["producer_event_id"]},
                        "field": span["field"], "request_ref": payload["request_ref"],
                        **{key: span[key] for key in ("pointer", "char_start", "char_end", "rendered_sha256")}}
                    owners[span["span_id"]] = event["run_id"]
            if kind == "memory.input_included":
                key = payload["manifest_span_id"]
                if key in actual:
                    raise ValueError("Duplicate input inclusion")
                if key not in expected or event["run_id"] != owners[key]:
                    raise ValueError("Input inclusion precedes its request or belongs to another run")
                actual[key] = payload
    if actual != expected:
        raise ValueError("Input inclusion events do not cover the request manifests")
    batches = artifact(base, bundle["tool_batches_ref"])
    for batch in batches:
        validate_batch(batch)
    return {"schemas_valid": len(checks), "journal_records_valid": len(scan["records"]),
            "synthetic_events_valid": len(events), "input_spans_verified": len(expected),
            "tool_batches_verified": len(batches), "runtime_readiness_verified": False,
            "benchmark_or_model_calls": 0}


if __name__ == "__main__":
    print(json.dumps(validate_fixture(), ensure_ascii=False, indent=2))
