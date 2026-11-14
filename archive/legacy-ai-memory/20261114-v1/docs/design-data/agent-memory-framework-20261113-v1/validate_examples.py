"""Read-only validation of design schemas and synthetic examples; no model calls."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib.metadata import version
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


BASE = Path(__file__).resolve().parent


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def reject(validator: Draft202012Validator, value, label: str) -> None:
    if not list(validator.iter_errors(value)):
        raise ValueError(f"Invalid contract example was accepted: {label}")


def pointer_value(document, pointer: str):
    value = document
    for part in pointer.lstrip("/").split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def main() -> None:
    schemas = {
        "plan": read_json(BASE / "run-plan.schema.json"),
        "event": read_json(BASE / "event.schema.json"),
    }
    validators = {}
    for name, schema in schemas.items():
        Draft202012Validator.check_schema(schema)
        validators[name] = Draft202012Validator(schema, format_checker=FormatChecker())

    plan = read_json(BASE / "run-plan.example.json")
    validators["plan"].validate(plan)
    if plan["plan_status"] != "draft":
        raise ValueError("The committed example must remain a draft")
    if plan["protocol"]["collection_bank_scope"] != "per_source_task":
        raise ValueError("Pool comparison fixture must start from independent source banks")

    records = [json.loads(line) for line in (BASE / "example-events.jsonl").read_text().splitlines() if line.strip()]
    events = {}
    artifacts = {}
    requests = {}
    snapshots = set()
    exposed = 0

    def artifact(ref):
        relative = Path(ref["path"])
        if relative.is_absolute():
            raise ValueError("Absolute artifact path")
        path = (BASE / relative).resolve()
        if not path.is_relative_to(BASE):
            raise ValueError("Artifact outside the fixture directory")
        content = path.read_bytes()
        if digest(content) != ref["sha256"]:
            raise ValueError(f"Artifact hash mismatch: {relative}")
        prior = artifacts.setdefault(ref["path"], ref["sha256"])
        if prior != ref["sha256"]:
            raise ValueError("Conflicting hashes for one artifact")
        return json.loads(content)

    def references(value):
        if isinstance(value, dict):
            if {"path", "sha256", "media_type"}.issubset(value):
                nested = artifact(value)
                references(nested)
            else:
                for child in value.values():
                    references(child)
        elif isinstance(value, list):
            for child in value:
                references(child)

    for seq, row in enumerate(records, 1):
        validators["event"].validate(row)
        if row["record_kind"] != "illustrative" or row["event_seq"] != seq:
            raise ValueError("Expected an ordered synthetic example trace")
        if row["event_id"] in events or any(ref not in events for ref in row["caused_by"]):
            raise ValueError("Duplicate event or forward causal reference")
        references(row["payload"])
        payload = row["payload"]
        kind = row["type"]
        if kind == "memory.stored":
            memory = artifact(payload["body_ref"])
            if digest(memory["body"].encode()) != payload["body_sha256"]:
                raise ValueError("Memory body hash mismatch")
            if payload["public_context_cutoff_seq"] >= seq:
                raise ValueError("Future source context")
        elif kind == "bank.snapshot_created":
            manifest = artifact(payload["manifest_ref"])
            if manifest["snapshot_id"] != payload["snapshot_id"]:
                raise ValueError("Snapshot reference mismatch")
            snapshots.add(payload["snapshot_id"])
        elif kind == "run.started" and row["stage"] == "probe":
            if payload["start_snapshot_id"] not in snapshots or not payload["fresh_session"]:
                raise ValueError("Probe must start with a known snapshot and fresh session")
        elif kind == "model.request_started":
            if payload["request_id"] in requests:
                raise ValueError("Duplicate request ID")
            request = artifact(payload["request_ref"])
            if "previous_response_id" in request:
                raise ValueError("Unexpected prior-session provider link")
            requests[payload["request_id"]] = payload["request_ref"]
        elif kind == "model.request_finished":
            if payload["request_id"] not in requests:
                raise ValueError("Request finish without start")
            if payload["cost_usd"] is not None or payload["usage"] is not None:
                raise ValueError("Synthetic trace must not invent observed usage")
        elif kind == "memory.input_included":
            if requests.get(payload["request_id"]) != payload["request_ref"]:
                raise ValueError("Input inclusion has a different request artifact")
            returned = events[payload["returned_event_id"]]
            if returned["type"] != "memory.returned":
                raise ValueError("Input source must be a memory return")
            matching = [item for item in returned["payload"]["items"]
                        if item["item_id"] == payload["item_id"] and item["version_id"] == payload["version_id"]]
            if not matching or payload["field"] not in matching[0]["fields"]:
                raise ValueError("Input field/version was not returned")
            text = pointer_value(artifact(payload["request_ref"]), payload["pointer"])
            begin, end = payload["char_start"], payload["char_end"]
            if not isinstance(text, str) or not 0 <= begin < end <= len(text):
                raise ValueError("Invalid input string range")
            if digest(text[begin:end].encode()) != payload["rendered_sha256"]:
                raise ValueError("Rendered input hash mismatch")
            exposed += 1
        elif kind == "outcome.scored":
            raise ValueError("The partial synthetic trace must not claim benchmark outcomes")
        events[row["event_id"]] = row

    # These are schema-shape checks; the fake references never make a runnable plan.
    ready = deepcopy(plan)
    ready["plan_status"] = "ready"
    reject(validators["plan"], ready, "unresolved draft promoted to ready")
    invalid = deepcopy(plan)
    invalid["conditions"][0]["read_mode"] = "on_demand"
    reject(validators["plan"], invalid, "memory disabled with a read policy")
    invalid = deepcopy(plan)
    invalid["sampling"]["trials_per_probe"] = 0
    reject(validators["plan"], invalid, "zero trials")
    ready["worker"]["parameters_confirmed"] = True
    ready["benchmark"].update(source_revision="0" * 40, domain="fixture", task_manifest_ref="fixture.json", task_manifest_sha256="0" * 64)
    ready["sampling"].update(independent_bank_replicates=1, order_count=1, trials_per_probe=1, schedule_seed=0)
    ready["catalog"]["page_size"] = 1
    ready["limits"]["max_model_requests_per_run"] = 1
    ready["analysis"].update(pairing_ref="fixture", uncertainty_method="fixture", annotation_protocol_ref="fixture", cost_allocation_ref="fixture", analysis_confirmed=True)
    for condition in ready["conditions"]:
        if condition["memory_enabled"]:
            condition["instruction_ref"] = "fixture"
    validators["plan"].validate(ready)
    invalid = deepcopy(ready)
    invalid["limits"]["max_model_requests_per_run"] = None
    reject(validators["plan"], invalid, "ready plan without finite call or time guard")
    invalid = deepcopy(records[0])
    invalid["type"] = "outcome.scored"
    invalid["payload"] = {"grading_status": "graded", "official_success": None, "official_score": 1, "official_artifact_ref": records[1]["payload"]["body_ref"]}
    reject(validators["event"], invalid, "graded result without a boolean success")

    print(json.dumps({
        "status": "passed",
        "scope": "design_schemas_and_synthetic_references_only",
        "jsonschema_version": version("jsonschema"),
        "schemas_valid": len(schemas),
        "synthetic_events_valid": len(records),
        "artifact_files_hash_verified": len(artifacts),
        "input_spans_verified": exposed,
        "invalid_contract_cases_rejected": 5,
        "runtime_readiness_verified": False,
        "benchmark_or_model_calls": 0,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
