"""Read-only validation of design schemas and synthetic examples; no model calls."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib.metadata import version
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from contract_v11 import validate_fixture, validators as validators_v11


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "tests" / "fixtures" / "contracts"


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
        "plan": read_json(ROOT / "schemas" / "run-plan.schema.json"),
        "policy-round": read_json(ROOT / "schemas" / "policy-round-v1.schema.json"),
        "study-plan-v1": read_json(ROOT / "schemas" / "study-plan-v1.schema.json"),
        "policy-evidence": read_json(ROOT / "schemas" / "policy-evidence-v1.schema.json"),
        "event": read_json(ROOT / "schemas" / "event.schema.json"),
        "connection-check": read_json(ROOT / "schemas" / "connection-check.schema.json"),
        "connection-check-v1.1": read_json(ROOT / "schemas" / "connection-check-v1.1.schema.json"),
        "memory-connection-check": read_json(ROOT / "schemas" / "memory-connection-check.schema.json"),
        "autonomy-pilot": read_json(ROOT / "schemas" / "autonomy-pilot.schema.json"),
        "autonomy-pilot-v1.1": read_json(ROOT / "schemas" / "autonomy-pilot-v1.1.schema.json"),
        "delivery-comparison": read_json(ROOT / "schemas" / "delivery-comparison.schema.json"),
        "execution-limits": read_json(ROOT / "schemas" / "execution-limits.schema.json"),
        "task-start-v1": read_json(ROOT / "schemas" / "task-start-v1.schema.json"),
        "paired-outcomes-v1": read_json(ROOT / "schemas" / "paired-outcomes-v1.schema.json"),
        "trajectory-outcomes-v1": read_json(ROOT / "schemas" / "trajectory-outcomes-v1.schema.json"),
        "decision-cue-v1": read_json(ROOT / "schemas" / "decision-cue-v1.schema.json"),
        "source-opportunity-v1": read_json(ROOT / "schemas" / "source-opportunity-v1.schema.json"),
        "task-room-v1": read_json(ROOT / "schemas" / "task-room-v1.schema.json"),
        "task-room-review-v1": read_json(ROOT / "schemas" / "task-room-review-v1.schema.json"),
        "recorded-preparation": read_json(ROOT / "schemas" / "recorded-preparation.schema.json"),
        "writing-exploration": read_json(ROOT / "schemas" / "writing-exploration.schema.json"),
        "prompt-comparison-v2": read_json(ROOT / "schemas" / "prompt-comparison-v2.schema.json"),
        "prompt-comparison-v3": read_json(ROOT / "schemas" / "prompt-comparison-v3.schema.json"),
        "written-comparison-v2": read_json(ROOT / "schemas" / "written-memory-comparison-v2.schema.json"),
        "written-comparison-rubrics": read_json(ROOT / "schemas" / "written-comparison-rubrics.schema.json"),
        "snapshot-replay": read_json(ROOT / "schemas" / "snapshot-replay.schema.json"),
        "written-replay-batch": read_json(ROOT / "schemas" / "written-replay-batch.schema.json"),
    }
    validators = {}
    for name, schema in schemas.items():
        Draft202012Validator.check_schema(schema)
        validators[name] = Draft202012Validator(schema, format_checker=FormatChecker())

    validators["paired-outcomes-v1"].validate(read_json(ROOT / "tests/fixtures/paired-outcomes-v1.json"))
    execution_fixture = read_json(ROOT / "tests/fixtures/execution-limits-v1.json")
    if execution_fixture["contract"] != "per-role-limits/v1":
        raise ValueError("Unknown execution limit fixture contract")
    validators["policy-round"].validate(read_json(ROOT / "protocols/diagnostics/portable-writer-round-v1.json"))
    from agent_memory_eval.study.plan import load_plan
    for plan_path in sorted(p for p in (ROOT / "protocols/studies").rglob("*.json") if "setups" not in p.parts):
        validators["study-plan-v1"].validate(read_json(plan_path))
        load_plan(plan_path, ROOT)
    reject(validators["study-plan-v1"], dict(read_json(ROOT / "protocols/studies/bank-outcome-first-01.json"),
                                             conditions=[]), "study plan without conditions")
    for result in read_json(ROOT / "artifacts/analyses/20261126-policy-evidence-summary.json").values():
        validators["policy-evidence"].validate(result)
    validators["task-room-v1"].validate(read_json(ROOT / "artifacts/analyses/20261127-banking-task-room.json"))
    validators["task-room-review-v1"].validate(read_json(ROOT / "artifacts/analyses/20261127-banking-task-room-review.json"))
    validators["execution-limits"].validate(execution_fixture["limits"])
    preparation_fixture = read_json(ROOT / "tests/fixtures/recorded-preparation-v1.json")
    for artifact in preparation_fixture["artifacts"]:
        validators["recorded-preparation"].validate(artifact)
    submitted = dict(preparation_fixture["artifacts"][-1], model_requests=1)
    reject(validators["recorded-preparation"], submitted, "preparation claiming a model attempt")
    from agent_memory_eval.contracts.writing import writing_profile
    writing_draft = read_json(ROOT / "protocols/drafts/tau2-memory-writing-exploration-v1.json")
    validators["writing-exploration"].validate(writing_draft)
    if writing_profile(writing_draft)["ready"]:
        raise ValueError("Unresolved writing exploration must remain a draft")
    policy_path = ROOT / "protocols/drafts/tau2-airline-payment-memory-v2.json"
    policy = read_json(policy_path)
    proposal = read_json(ROOT / "tests/fixtures/airline-payment-writer-proposal.json")
    if proposal["parent_config_sha256"] != digest(policy_path.read_bytes()):
        raise ValueError("The writer proposal fixture cites a different parent configuration")
    policy_validator = validators["prompt-comparison-v2"]
    policy_validator.validate(policy)
    candidate = dict(policy["conditions"][1], prompt=proposal["writer_prompt"])
    policy_validator.validate(dict(policy, conditions=[policy["conditions"][0], candidate], candidate_proposal=proposal))
    reject(policy_validator, dict(policy, status="ready"), "unbudgeted writer comparison promoted to live")
    pair_policy = read_json(ROOT / "protocols/drafts/tau2-airline-writer-pairs-v3.json")
    validators["prompt-comparison-v3"].validate(pair_policy)
    reject(validators["prompt-comparison-v3"], dict(pair_policy, forms={}), "unassigned representations")
    from agent_memory_eval.analysis.written_comparison import validate_rubrics
    from agent_memory_eval.experiments.writing_comparison import _profile
    comparison = read_json(ROOT / "protocols/drafts/tau2-written-memory-replay-v2.json")
    _profile(comparison)
    reject(validators["written-comparison-v2"], dict(comparison, status="ready"), "offline comparison promoted to live")
    comparison_fixture = read_json(ROOT / "tests/fixtures/written-comparison-validity-v1.json")
    validate_rubrics(comparison_fixture["packet"], comparison_fixture["rubrics"])
    from agent_memory_eval.contracts.snapshot_replay import validate_replay_record
    replay_fixture = read_json(ROOT / "tests/fixtures/snapshot-replay-v1.json")
    for artifact in replay_fixture["artifacts"]:
        validate_replay_record(artifact)
    reject(validators["snapshot-replay"], dict(replay_fixture["artifacts"][-1], official_success=True), "single reply claiming official success")
    from agent_memory_eval.experiments.replay_schedule import validate_profile as validate_replay_batch
    from agent_memory_eval.recording.assets import asset_text
    batch = read_json(ROOT / "protocols/drafts/tau2-written-memory-batch-v1.json")
    if not validate_replay_batch(batch):
        raise ValueError("The unapproved written replay batch must retain its unresolved settings")
    for ref in (batch["comparison_ref"], batch["rubrics_ref"]):
        asset_text(ROOT, ref)
    reject(validators["written-replay-batch"], dict(batch, status="ready"), "unresolved four-arm batch promoted to live")
    for field, value in (("worker_requests", -1), ("writer_requests", None), ("max_input_bytes", 0)):
        invalid_limits = dict(execution_fixture["limits"], **{field: value})
        reject(validators["execution-limits"], invalid_limits, "invalid explicit execution limits")

    plan = read_json(ROOT / "protocols" / "drafts" / "tau2-two-pass.json")
    validators["connection-check"].validate(read_json(ROOT / "protocols" / "checks" / "openrouter-tau2-smoke-v1.0.json"))
    validators["connection-check-v1.1"].validate(read_json(ROOT / "protocols" / "checks" / "openrouter-tau2-smoke.json"))
    memory_check = read_json(ROOT / "protocols" / "checks" / "openrouter-tau2-memory-smoke.json")
    validators["memory-connection-check"].validate(memory_check)
    for ref in [memory_check["connection_profile_ref"], *memory_check["instructions"].values()]:
        path = (ROOT / ref["path"]).resolve()
        if not path.is_relative_to(ROOT) or digest(path.read_bytes()) != ref["sha256"]:
            raise ValueError("Memory connection profile has a changed or external asset")
    pilot = read_json(ROOT / "protocols" / "pilots" / "tau2-autonomous-memory-v1.json")
    validators["autonomy-pilot"].validate(pilot)
    for ref in [pilot["connection_profile_ref"], *pilot["instructions"].values()]:
        path = (ROOT/ref["path"]).resolve()
        if not path.is_relative_to(ROOT) or digest(path.read_bytes()) != ref["sha256"]:
            raise ValueError("Autonomy pilot has a changed or external asset")
    frozen_pilot = read_json(ROOT / "protocols" / "pilots" / "tau2-autonomous-memory-v1.1.json")
    validators["autonomy-pilot-v1.1"].validate(frozen_pilot)
    for ref in [frozen_pilot["connection_profile_ref"], *frozen_pilot["instructions"].values()]:
        path = (ROOT/ref["path"]).resolve()
        if not path.is_relative_to(ROOT) or digest(path.read_bytes()) != ref["sha256"]:
            raise ValueError("Frozen-bank pilot has a changed or external asset")
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

    invalid = deepcopy(records[0])
    invalid["timestamp_utc"] = "not-a-dateZ"
    reject(validators["event"], invalid, "invalid UTC date-time")

    checks_v11 = validators_v11()
    draft_v11 = read_json(ROOT / "protocols" / "drafts" / "tau2-two-pass-v1.1.json")
    checks_v11["run-plan"].validate(draft_v11)
    if draft_v11["plan_status"] != "draft":
        raise ValueError("The v1.1 example must remain a draft")
    result_v11 = validate_fixture()
    # The v1.2 fixture is a real illustrative file-core trace, never a model result.
    from agent_memory_eval.recording.journal import BlobStore, read_experiment
    from agent_memory_eval.contracts.validation import canonical, validate_manifest, validate_public_return
    from agent_memory_eval.recording.provenance import exposure_payload
    from agent_memory_eval.runner.tau2_delivery import compile_delivery
    compile_delivery(read_json(ROOT / "protocols/drafts/tau2-delivery-comparison-v1.json"), require_ready=False)
    fixture_v12 = ROOT / "tests/fixtures/contracts-v1.2"
    scan_v12, state_v12, _ = read_experiment(fixture_v12)
    if scan_v12["incomplete_runs"] or scan_v12["pending_requests"] or scan_v12["tail_bytes"]:
        raise ValueError("The preload fixture must be closed")
    blobs_v12 = BlobStore(fixture_v12)
    expected_v12, actual_v12 = {}, {}
    for event in scan_v12["events"].values():
        if event["record_kind"] != "illustrative":
            raise ValueError("The preload fixture must remain illustrative")
        if event["schema_version"] == "1.2":
            reject(checks_v11["event"], event, "snapshot extension passed as v1.1")
        if event["type"] == "model.request_started":
            origin = state_v12["provenance"][event["payload"]["request_id"]]
            catalog = blobs_v12.get(origin["sources_ref"])
            manifest = blobs_v12.get(event["payload"]["input_manifest_ref"])
            spans = validate_manifest(fixture_v12, manifest, event, catalog, scan_v12["events"], origin["approved_templates"])
            for span in spans:
                expected_v12[span["span_id"]] = exposure_payload(manifest, span, catalog[span["source_id"]])
        if event["type"] == "memory.input_included":
            actual_v12[event["payload"]["manifest_span_id"]] = event["payload"]
    if expected_v12 != actual_v12 or not expected_v12:
        raise ValueError("Preload fixture exposure coverage differs")

    native = read_json(ROOT / "tests/fixtures/contracts-v1.3/native-return.json")
    native_event, native_wire = native["event"], native["wire"]
    checks_v11["event-v1.3"].validate(native_event)
    reject(checks_v11["event"], native_event, "native return accepted as v1.1")
    if digest(canonical(native_wire)) != native_event["payload"]["result_ref"]["sha256"]:
        raise ValueError("Native fixture wire digest differs")
    validate_public_return(native_wire, native_event["payload"])

    print(json.dumps({
        "status": "passed",
        "scope": "design_schemas_and_synthetic_references_only",
        "jsonschema_version": version("jsonschema"),
        "schemas_valid": len(schemas),
        "synthetic_events_valid": len(records),
        "artifact_files_hash_verified": len(artifacts),
        "input_spans_verified": exposed,
        "invalid_contract_cases_rejected": 6,
        "v1_1": result_v11,
        "v1_2": dict(schemas_valid=3, synthetic_events_valid=len(scan_v12["events"]), input_spans_verified=len(expected_v12)),
        "v1_3": dict(schemas_valid=2, illustrative_native_returns_valid=1),
        "execution_limits": dict(contract="per-role-limits/v1", fixtures_valid=1, invalid_cases_rejected=3),
        "snapshot_replay": dict(fixtures_valid=len(replay_fixture["artifacts"]), invalid_cases_rejected=1,
            runtime_readiness_verified=False),
        "written_replay_batch": dict(draft_valid=True, anchored_assets=2, unresolved_ready_rejected=True),
        "prompt_comparison_v2": dict(draft_valid=True, proposal_fixture_valid=True, unbudgeted_ready_rejected=True),
        "runtime_readiness_verified": False,
        "benchmark_or_model_calls": 0,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
