"""Shared read-only request provenance and exposure audit across experiment types."""

from collections import Counter
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.contracts.validation import validate_manifest
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from agent_memory_eval.recording.provenance import exposure_payload


def audit_requests(root, *, evidence=None):
    root = Path(root)
    scan, _, _ = evidence or read_experiment(root)
    if scan["tail_bytes"] or scan["incomplete_runs"] or scan["pending_requests"]:
        raise ContractError("Request evidence is incomplete")
    blobs, expected, actual, roles = BlobStore(root), {}, {}, Counter()
    for row in scan["records"]:
        requests = [e for e in row["events"] if e["type"] == "model.request_started"]
        if not requests:
            continue
        # Request-local state also works when a later runner drops old provenance.
        origins = blobs.get(row["state_ref"])["provenance"]
        for event in requests:
            payload = event["payload"]
            roles[payload["actor_role"]] += 1
            manifest = blobs.get(payload["input_manifest_ref"])
            origin = origins[payload["request_id"]]
            sources = blobs.get(origin["sources_ref"])
            spans = validate_manifest(root, manifest, event, sources, scan["events"], origin["approved_templates"])
            for span in spans:
                expected[span["span_id"]] = (event["run_id"], exposure_payload(manifest, span, sources[span["source_id"]]))
    for event in scan["events"].values():
        if event["type"] == "memory.input_included":
            key = event["payload"]["manifest_span_id"]
            if key in actual:
                raise ContractError("Duplicate memory exposure")
            actual[key] = (event["run_id"], event["payload"])
    if expected != actual:
        raise ContractError("Memory exposure differs from request provenance")
    return dict(status="passed", requests=sum(roles.values()), requests_by_role=dict(roles),
                memory_input_spans=len(actual))
