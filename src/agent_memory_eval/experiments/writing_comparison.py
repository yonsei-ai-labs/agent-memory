"""Read-only preparation of a draft comparison; no model or benchmark execution."""

from collections import Counter
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from agent_memory_eval.analysis.writing_export import collect
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.memory.context import request_with_past_memory
from agent_memory_eval.memory.preparation import SnapshotBudgetExceeded
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path


ARMS = ("empty", "generic", "model_written", "researcher")
PROFILE_V2 = "agent-memory-written-replay-draft/v2"


def _profile(profile):
    if profile.get("format") == PROFILE_V2:
        try:
            canonical(profile)
            schema = read_json((ROOT / "schemas/written-memory-comparison-v2.schema.json").read_bytes())
            Draft202012Validator(schema).validate(profile)
        except (ValueError, TypeError, ValidationError) as error:
            raise ContractError("Invalid written-memory comparison v2 profile") from error
    if (profile.get("format") not in {"agent-memory-written-replay-draft/v1", PROFILE_V2}
            or profile.get("status") != "draft" or profile.get("executable") is not False
            or profile.get("arms") != list(ARMS)):
        raise ContractError("Only the explicit four-arm offline draft is supported")
    if (not profile.get("cases") or len({c["case_id"] for c in profile["cases"]}) != len(profile["cases"])
            or any(c["task_id"] in {"7", "39"} for c in profile["cases"])):
        raise ContractError("Cases must be distinct and exclude the conflicting primary tasks")
    if (profile["delivery"]["format"] != "past-memory-json-context/v1"
            or profile["delivery"]["memory_role"] != "user"
            or type(profile["delivery"]["position"]) is not int or profile["delivery"]["position"] != 1):
        raise ContractError("Supported delivery is the shared user context immediately after system")
    cap = profile["delivery"]["max_input_bytes"]
    if cap is not None and (type(cap) is not int or cap < 1):
        raise ContractError("Input cap is positive or explicitly unresolved")
    policy = MemoryPolicy(**profile["delivery"]["policy"])
    if not policy.preloads or policy.tools("solve") or policy.write_timing != "none" or policy.same_episode_read:
        raise ContractError("Comparison uses the same frozen read-only preload policy in all arms")
    for arm in ("generic", "researcher"):
        memo = profile["control_notes"][arm]
        if (set(memo) != {"title", "body", "information_access", "origin"}
                or any(not isinstance(v, str) or not v.strip() for v in memo.values())):
            raise ContractError("Each control note requires content and explicit analysis-only provenance")
    return policy


def selected_action(events, blobs, case):
    """Bind a v2 candidate to its declared occurrence, not just a valid request hash."""
    selection = case["selection"]
    ordinal, total = selection["request_ordinal"], selection["expected_request_count"]
    if type(ordinal) is not int or type(total) is not int or not 1 <= ordinal <= total:
        raise ContractError("Target action selection needs positive ordinal and total")
    starts = {e["payload"]["request_id"]: e for e in events if e["type"] == "model.request_started"}
    occurrences = []
    for event in events:
        if event["type"] != "model.request_finished" or event["payload"]["status"] != "response":
            continue
        start = starts.get(event["payload"]["request_id"])
        if start is None or start["payload"]["actor_role"] != "worker":
            continue
        raw = blobs.get(event["payload"]["response_ref"])
        choices = raw.get("choices", [])
        if len(choices) != 1:
            raise ContractError("Candidate source has ambiguous response choices")
        calls = choices[0]["message"].get("tool_calls") or []
        targets = [c for c in calls if c.get("function", {}).get("name") == selection["tool"]]
        if targets:
            if choices[0].get("finish_reason") != "tool_calls" or len(targets) != 1:
                raise ContractError("Selected action must be one complete target call per response")
            occurrences.append((event["payload"]["request_id"], targets[0]))
    if (len(occurrences) != total or ordinal > len(occurrences)
            or occurrences[ordinal - 1][0] != case["request_id"]):
        raise ContractError("Candidate is not the declared target-action occurrence")
    return dict(tool=selection["tool"], request_ordinal=ordinal, observed_request_count=len(occurrences),
                call_id=occurrences[ordinal - 1][1]["id"])


def _cases(profile, root):
    source = profile["decision_source"]
    path = safe_path(root, source["path"])
    before = (path / "log.jsonl").read_bytes()
    if digest(before) != source["log_sha256"]:
        raise ContractError("Decision source log changed")
    scan, state, _ = read_experiment(path)
    if scan["tail_bytes"] or scan["pending_requests"] or scan["incomplete_runs"]:
        raise ContractError("Decision source is incomplete")
    blobs = BlobStore(path)
    runtime = state.get("baseline_screening", {})
    if "control_ref" not in runtime:
        raise ContractError("This draft requires the verified baseline allocation table")
    control = blobs.get(runtime["control_ref"])
    cases = []
    for case in profile["cases"]:
        allocation = [a for a in control["schedule"] if a["run_id"] == case["run_id"]]
        if (len(allocation) != 1 or allocation[0]["task_id"] != case["task_id"]
                or allocation[0]["repetition"] != case["repetition"]):
            raise ContractError("Decision task/repetition differs from its frozen allocation")
        events = [e for e in scan["events"].values() if e["run_id"] == case["run_id"]]
        starts = [e for e in events if e["type"] == "model.request_started"
                  and e["payload"]["request_id"] == case["request_id"]]
        finishes = [e for e in events if e["type"] == "model.request_finished"
                    and e["payload"]["request_id"] == case["request_id"]]
        ends = [e for e in events if e["type"] == "run.ended"]
        outcomes = [e for e in events if e["type"] == "outcome.scored"]
        if (len(starts) != 1 or starts[0]["payload"]["actor_role"] != "worker"
                or len(finishes) != 1 or finishes[0]["payload"]["status"] != "response"
                or len(ends) != 1 or ends[0]["payload"]["grading_status"] != "graded"
                or len(outcomes) != 1 or outcomes[0]["payload"]["grading_status"] != "graded"
                or any(e["type"].startswith("memory.") for e in events)):
            raise ContractError("Decision must belong to a closed graded no-memory worker run")
        request_ref, response_ref = starts[0]["payload"]["request_ref"], finishes[0]["payload"]["response_ref"]
        if (request_ref["sha256"] != case["request_sha256"]
                or response_ref["sha256"] != case["response_sha256"]):
            raise ContractError("Decision artifact differs from the selected candidate")
        wire, raw = blobs.get(request_ref), blobs.get(response_ref)
        if (raw["choices"][0].get("finish_reason") not in {"stop", "tool_calls"}
                or not wire.get("messages") or wire["messages"][0]["role"] != "system"
                or any(m["role"] == "system" for m in wire["messages"][1:])
                or any(t["function"]["name"].startswith("memory_") for t in wire.get("tools", []))):
            raise ContractError("Candidate requires a normal reply and an unmodified no-memory context")
        cases.append(dict(case, wire=wire, request_ref=request_ref, response_ref=response_ref,
            request_event_id=starts[0]["event_id"], response_event_id=finishes[0]["event_id"],
            original_finish_reason=raw["choices"][0]["finish_reason"],
            original_episode_success=outcomes[0]["payload"]["official_success"],
            decision_policy_adjudication="pending_independent_review"))
        if profile["format"] == PROFILE_V2:
            from agent_memory_eval.memory.writing import public_message
            cases[-1]["verified_selection"] = selected_action(events, blobs, case)
            cases[-1]["original_public_reply"] = public_message(raw["choices"][0]["message"])
    if (path / "log.jsonl").read_bytes() != before:
        raise ContractError("Decision source changed during preparation")
    return cases


def _note(memo):
    """Opaque IDs match the item shape; arm/provenance labels stay outside the request."""
    payload = {k:memo[k] for k in ("title", "body")}
    key = digest(canonical(payload))
    return dict(id="m_" + key[:32], version="v_" + key[32:], **payload)


def comparison_requests(cases, banks, policy, instruction, control_notes, *, max_input_bytes=None):
    """Pure renderer. A preparation row is not an independent paid replicate or an exposure event."""
    requests, rows = {}, []
    if not policy.preloads or policy.tools("solve") or policy.write_timing != "none" or policy.same_episode_read:
        raise ContractError("Comparison requires a frozen read-only preload policy")
    if max_input_bytes is not None and (type(max_input_bytes) is not int or max_input_bytes < 1):
        raise ContractError("Input cap is positive or explicitly unresolved")
    if not cases or len({c["case_id"] for c in cases}) != len(cases):
        raise ContractError("At least one distinct candidate decision is required")
    if len({b["run_id"] for b in banks}) != len(banks) or not banks:
        raise ContractError("Every writing allocation appears exactly once")
    for case in cases:
        original = case["wire"]
        for bank in banks:
            for arm in ARMS:
                items = ([] if arm == "empty" else deepcopy(bank["items"]) if arm == "model_written"
                         else [_note(control_notes[arm])])
                payload_bytes = sum(len(canonical({k:item[k] for k in ("title", "body")})) for item in items)
                if ((policy.max_items is not None and len(items) > policy.max_items)
                        or (policy.max_payload_bytes is not None and payload_bytes > policy.max_payload_bytes)
                        or (policy.max_return_bytes is not None
                            and len(canonical(dict(items=items))) > policy.max_return_bytes)):
                    raise SnapshotBudgetExceeded("A complete comparison arm exceeds the declared memory budget")
                wire = request_with_past_memory(original, policy, items, instruction=instruction)
                if ({k:v for k,v in wire.items() if k != "messages"}
                        != {k:v for k,v in original.items() if k != "messages"}
                        or wire["messages"][2:] != original["messages"][1:]):
                    raise ContractError("Preparation changed original tools, parameters or conversation")
                data = canonical(wire)
                key = digest(data)
                requests[key] = wire
                rows.append(dict(case_id=case["case_id"], writer_run_id=bank["run_id"],
                    feedback_level=bank["feedback_level"], writer_repetition=bank["repetition"],
                    writing_status=bank["status"], snapshot_id=bank["snapshot_id"], arm=arm,
                    pair_id=case["case_id"] + ":" + bank["run_id"], request_sha256=key,
                    identical_input_group=key, input_bytes=len(data), item_count=len(items),
                    fits_input_limit=None if max_input_bytes is None else len(data) <= max_input_bytes,
                    original_tools_and_parameters_preserved=True, original_conversation_preserved=True,
                    business_tools_executed=False, response_observed=False, outcome="not_measured"))
    return rows, requests


def prepare_comparison(profile, root):
    """Read original logs, preserve all writing allocations, and build unexecuted inputs."""
    root = Path(root).resolve()
    policy = _profile(profile)
    receipt = read_json(asset_text(root, profile["writing_result_ref"]).encode())
    if receipt.get("format") != "agent-memory-writing-result/v1":
        raise ContractError("Expected the anchored writing result receipt")
    source = safe_path(root, receipt["source_root"])
    before = (source / "log.jsonl").read_bytes()
    if digest(before) != receipt["raw_log_sha256"]:
        raise ContractError("Writing source log changed")
    exported = collect(source)
    banks = exported["metrics"]["runs"]
    if (exported["metrics"]["audit"] != receipt["audit"]
            or exported["metrics"]["by_feedback"] != receipt["by_feedback"]):
        raise ContractError("Writing receipt differs from the complete source audit")
    if (len(banks) != receipt["audit"]["allocations"]
            or any(b["status"] not in {"completed", "empty"} or not b["replay_eligible"] for b in banks)
            or sum(b["final_items"] for b in banks) != sum(
                v["final_items"] for v in receipt["by_feedback"].values())):
        raise ContractError("All complete and empty source allocations must be preserved")
    cases = _cases(profile, root)
    instruction = asset_text(root, profile["replay_instruction_ref"])
    rows, requests = comparison_requests(cases, banks, policy, instruction, profile["control_notes"],
        max_input_bytes=profile["delivery"]["max_input_bytes"])
    counts = Counter(row["request_sha256"] for row in rows)
    if (source / "log.jsonl").read_bytes() != before:
        raise ContractError("Writing source changed during preparation")
    manifest = dict(format="agent-memory-written-replay-preparation/v1", status="prepared_offline_draft",
        live_ready=False, profile_sha256=digest(canonical(profile)), writing_log_sha256=receipt["raw_log_sha256"],
        decision_log_sha256=profile["decision_source"]["log_sha256"],
        writer_allocations=len(banks), empty_writer_allocations=sum(not b["items"] for b in banks),
        candidate_cases=[{k:v for k,v in c.items() if k != "wire"} for c in cases],
        preparation_rows=len(rows), unique_request_payloads=len(requests),
        identical_input_multiplicity=dict(sorted(counts.items())),
        rows=rows, max_input_bytes=profile["delivery"]["max_input_bytes"],
        input_bytes=dict(min=min(r["input_bytes"] for r in rows), max=max(r["input_bytes"] for r in rows)),
        new_model_or_catalog_calls=0, business_tools_executed=False, official_scores_computed=False,
        semantic_outcomes_measured=False, exposure_events_recorded=False,
        scope="unreplicated_candidate_request_preparation_not_a_run_schedule_or_memory_effect_measurement",
        unresolved=deepcopy(profile["unresolved"]))
    if profile["format"] == PROFILE_V2:
        from agent_memory_eval.analysis.written_comparison import case_packet
        manifest["format"] = "agent-memory-written-replay-preparation/v2"
        manifest["profile"] = deepcopy(profile)
        manifest["case_review_packet"] = case_packet(cases, profile)
        manifest["case_review_packet_sha256"] = digest(canonical(manifest["case_review_packet"]))
        manifest["claim_scope"] = "retrospective_single_reply_only"
        manifest["writing_allocations"] = deepcopy(banks)
        manifest["research_coverage"] = deepcopy(profile["research_coverage"])
    return manifest, requests


def export_comparison(profile, root, output):
    root, output = Path(root).resolve(), Path(output).absolute()
    receipt = read_json(asset_text(root, profile["writing_result_ref"]).encode())
    sources = [safe_path(root, receipt["source_root"]), safe_path(root, profile["decision_source"]["path"])]
    if output.exists() or any(output.resolve().is_relative_to(p.resolve()) for p in sources):
        raise ContractError("Use a new output directory outside every preserved source")
    if output.resolve().is_relative_to(root) and output.resolve().relative_to(root).parts[0] != "outputs":
        raise ContractError("Repository preparation exports belong in ignored outputs/")
    manifest, requests = prepare_comparison(profile, root)
    output.mkdir(parents=True)
    folder = output / "requests"
    folder.mkdir()
    for key, wire in requests.items():
        (folder / (key + ".json")).write_bytes(canonical(wire))
    (output / "preparation.json").write_bytes(canonical(manifest) + b"\n")
    return comparison_summary(manifest)


def comparison_summary(manifest):
    return {k:v for k,v in manifest.items() if k not in {
        "rows", "candidate_cases", "identical_input_multiplicity", "case_review_packet", "profile", "writing_allocations"}}


def audit_comparison(profile, root, output):
    """Rebuild from anchored sources and compare every exported byte without writing."""
    output = Path(output)
    expected, requests = prepare_comparison(profile, root)
    data = safe_path(output, "preparation.json").read_bytes()
    if data != canonical(expected) + b"\n":
        raise ContractError("Prepared comparison manifest differs from source reconstruction")
    names = {"preparation.json", "requests"} | {"requests/" + k + ".json" for k in requests}
    actual = set()
    for path in output.rglob("*"):
        if path.is_symlink():
            raise ContractError("Preparation bundle contains a symlink")
        actual.add(str(path.relative_to(output)))
    if actual != names:
        raise ContractError("Preparation bundle has missing or unexpected files")
    for key, request in requests.items():
        if safe_path(output, "requests/" + key + ".json").read_bytes() != canonical(request):
            raise ContractError("Prepared request differs from its source and control arm")
    return dict(comparison_summary(expected), status="audit_passed", source_reconstructed=True,
                request_files_verified=len(requests))
