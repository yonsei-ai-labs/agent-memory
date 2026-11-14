"""Recompute synthetic summaries from committed records, without another execution."""

from collections import Counter
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.contracts.validation import validate_manifest
from agent_memory_eval.recording.journal import BlobStore, read_experiment


def summarize(root):
    root = Path(root)
    blobs = BlobStore(root)
    scan, state, experiment = read_experiment(root)
    events = scan["events"]
    if any(e["record_kind"] != "illustrative" for e in events.values()):
        raise ContractError("This analyzer accepts synthetic evidence only")
    expected, actual, requests = {}, {}, {}
    synthetic, official, terminations = {}, {}, {}
    for event in events.values():
        payload, kind = event["payload"], event["type"]
        if kind == "model.request_started":
            requests[payload["request_id"]] = payload["request_ref"]
            manifest = blobs.get(payload["input_manifest_ref"])
            provenance = state["provenance"][payload["request_id"]]
            catalog = blobs.get(provenance["sources_ref"])
            spans = validate_manifest(root, manifest, event, catalog, events, provenance["approved_templates"])
            for span in spans:
                source = catalog[span["source_id"]]
                expected[span["span_id"]] = (event["run_id"], span, source)
        elif kind == "memory.input_included":
            key = payload["manifest_span_id"]
            if key in actual or key not in expected:
                raise ContractError("Unexpected or duplicate inclusion event")
            owner, span, source = expected[key]
            origin = "returned_event_id" if source["kind"] == "memory_return" else "response_event_id"
            wanted = dict(request_id=key.rsplit(":m", 1)[0], manifest_span_id=key,
                source=dict(kind=source["kind"], **{origin: source["producer_event_id"]}),
                request_ref=requests[key.rsplit(":m", 1)[0]],
                **{field: span[field] for field in ("item_id", "version_id", "field", "pointer", "char_start", "char_end", "rendered_sha256")})
            if payload != wanted or owner != event["run_id"]:
                raise ContractError("Inclusion differs from request provenance")
            actual[key] = payload
        elif kind == "business.transition" and "synthetic_result_ref" in payload:
            synthetic[event["run_id"]] = blobs.get(payload["synthetic_result_ref"])["synthetic_success"]
        elif kind == "outcome.scored":
            official[event["run_id"]] = payload["official_success"]
        elif kind == "run.ended":
            terminations[event["run_id"]] = payload["termination_reason"]
    if set(expected) != set(actual):
        raise ContractError("Missing input inclusion events")
    types = Counter(e["type"] for e in events.values())
    pairs = []
    for run, assignment in state["assignments"].items():
        if assignment["stage"] == "probe" and assignment["mode"] != "no_memory":
            baseline = assignment.get("baseline_run")
            if baseline is None:
                continue
            base_assignment = state["assignments"].get(baseline)
            if base_assignment is None or base_assignment["mode"] != "no_memory" or base_assignment["task_key"] != assignment["task_key"]:
                raise ContractError("Invalid assigned baseline pair")
            pairs.append(dict(task=assignment["task_key"], mode=assignment["mode"], baseline_run=baseline, memory_run=run,
                              synthetic_outcomes=[synthetic.get(baseline), synthetic.get(run)]))
    return dict(record_kind="illustrative", scope="offline_synthetic_execution_only", benchmark_or_model_calls=0,
        run_count=len(scan["starts"]), scheduled_runs=len(state.get("schedule", [])),
        incomplete_runs=scan["incomplete_runs"], pending_requests=scan["pending_requests"], tail_bytes=scan["tail_bytes"],
        requests=types["model.request_started"], memory_stores=types["memory.stored"],
        memory_returns=types["memory.returned"], input_spans=types["memory.input_included"],
        self_authored_spans=sum(v["source"]["kind"] == "self_authored" for v in actual.values()),
        retrieved_spans=sum(v["source"]["kind"] == "memory_return" for v in actual.values()),
        official_outcomes=official, synthetic_outcomes=synthetic, synthetic_pairs=pairs, terminations=terminations,
        cost_usd=None, cost_status="not_applicable")
