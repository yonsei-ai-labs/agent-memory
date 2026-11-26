"""Public-evidence review and narrow checks, separate from any policy-effect claim."""

from copy import deepcopy
from math import isfinite

from jsonschema import Draft202012Validator, ValidationError

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT, SCHEMA_ROOT
from agent_memory_eval.memory.writing import public_message


SCREEN_VERSION = "public-booking-checks/v1"


def case_packet(cases, profile):
    """Do not expose episode grades, hidden task intentions, DB answers or reasoning."""
    entries = []
    for case in cases:
        messages = [public_message(m) for m in case["wire"]["messages"]]
        entries.append(dict(case_id=case["case_id"], source_request_sha256=case["request_sha256"],
            messages=messages, message_sha256=[digest(canonical(m)) for m in messages],
            original_public_reply=deepcopy(case["original_public_reply"]),
            verified_selection=deepcopy(case["verified_selection"])))
    return dict(format="agent-memory-written-case-packet/v1", cases=entries,
        profile_sha256=digest(canonical(profile)),
        control_notes=deepcopy(profile["control_notes"]),
        scope="public_decision_and_control_review_not_independent_adjudication",
        historical_grades_included=False, new_model_responses=False)


def validate_rubrics(packet, document):
    """Validate evidence binding, not the truth or independence of a semantic judgment."""
    try:
        canonical(document)
        schema = read_json((SCHEMA_ROOT / "written-comparison-rubrics.schema.json").read_bytes())
        Draft202012Validator(schema).validate(document)
    except (ValueError, TypeError, ValidationError) as error:
        raise ContractError("Invalid written-comparison rubric") from error
    if (packet["format"] != "agent-memory-written-case-packet/v1"
            or document["packet_sha256"] != digest(canonical(packet))):
        raise ContractError("Rubric belongs to a different public evidence packet")
    cases = {c["case_id"]: c for c in packet["cases"]}
    rows = document["cases"]
    if len(cases) != len(packet["cases"]) or len(rows) != len(cases) or {r["case_id"] for r in rows} != set(cases):
        raise ContractError("Review each candidate exactly once")
    for row in rows:
        case = cases[row["case_id"]]
        for key, role in (("baggage", "user"), ("certificates", "system")):
            evidence = row[key]["evidence"]
            index = evidence["message_index"]
            if index >= len(case["messages"]):
                raise ContractError("Rubric cites a future or missing message")
            message = case["messages"][index]
            if (message["role"] != role or evidence["message_sha256"] != digest(canonical(message))
                    or evidence["excerpt"] not in (message.get("content") or "")):
                raise ContractError("Rubric evidence is not in the declared public message")
        baggage = row["baggage"]
        if (baggage["interpretation"] == "explicit_quantity") != (baggage["expected_quantity"] is not None):
            raise ContractError("Unresolved quantity cannot acquire an assumed answer")
    return dict(status="evidence_bindings_verified", semantic_judgments_verified=False,
        independent_adjudication_verified=False, cases=len(cases))


def _object(value):
    value = read_json(value.encode()) if isinstance(value, str) else value
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    canonical(value)  # Reject nonfinite numbers even in unused arguments.
    return value


def _payment_sources(messages):
    """Use only already returned, call-linked public user profiles; never hidden DB."""
    pending, profiles = {}, {}
    for message in messages:
        if message["role"] == "assistant":
            for call in message.get("tool_calls") or []:
                if call["function"]["name"] == "get_user_details":
                    args = _object(call["function"]["arguments"])
                    pending[call["id"]] = args["user_id"]
        elif message["role"] == "tool" and message.get("tool_call_id") in pending:
            user = pending.pop(message["tool_call_id"])
            value = _object(message["content"])
            if value.get("user_id") == user and isinstance(value.get("payment_methods"), dict):
                profiles[user] = {key: method["source"] for key, method in value["payment_methods"].items()}
    return profiles


def screen_reply(case, rubric, message, *, finish_reason):
    """Check only quantity and certificate count. All approval/other policy review remains open."""
    if case["case_id"] != rubric["case_id"]:
        raise ContractError("Reply screen and rubric refer to different cases")
    result = dict(screen_version=SCREEN_VERSION, status="unparseable_or_unknown_arguments",
        target_write_calls=0, checks=[], other_tool_calls=[],
        approval_status="not_automatically_judged", full_policy_status="not_automatically_judged",
        business_tools_executed=False, official_success=None, episode_outcome="not_measured")
    if finish_reason not in {"stop", "tool_calls"}:
        return dict(result, status="incomplete_or_rejected_response")
    try:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError("Expected assistant reply")
        if message.get("refusal"):
            return dict(result, status="incomplete_or_rejected_response")
        calls = message.get("tool_calls")
        calls = [] if calls is None else calls
        if not isinstance(calls, list) or bool(calls) != (finish_reason == "tool_calls"):
            raise ValueError("Inconsistent finish reason or calls")
        if not calls:
            return dict(result, status="no_target_write", text_requires_semantic_review=True)
        if len({call["id"] for call in calls}) != len(calls):
            raise ValueError("Duplicate tool call identity")
        profiles = _payment_sources(case["messages"])
        for call in calls:
            function = call["function"]
            if function["name"] != "book_reservation":
                result["other_tool_calls"].append(function["name"])
                continue
            result["target_write_calls"] += 1
            args = _object(function["arguments"])
            quantity = args["total_baggages"]
            if type(quantity) is not int or quantity < 0:
                raise ValueError("Baggage quantity must be a nonnegative integer")
            expected = rubric["baggage"]["expected_quantity"]
            baggage = "unresolved_public_quantity" if expected is None else "pass" if quantity == expected else "violation"
            payments = args["payment_methods"]
            if not isinstance(payments, list) or not payments:
                raise ValueError("Missing payment plan")
            ids = [p["payment_id"] for p in payments]
            if len(set(ids)) != len(ids) or any(type(p["amount"]) not in (int, float)
                    or not isfinite(p["amount"]) or p["amount"] < 0 for p in payments):
                raise ValueError("Invalid or duplicate payment entries")
            methods = profiles.get(args["user_id"], {})
            known = all(methods.get(key) in {"credit_card", "gift_card", "certificate"} for key in ids)
            certificates = sum(methods.get(key) == "certificate" for key in ids) if known else None
            payment_status = ("unknown_public_payment_method" if certificates is None else
                "violation" if certificates > rubric["certificates"]["max_per_reservation"] else "pass")
            result["checks"].append(dict(call_id=call["id"], total_baggages=quantity,
                baggage_status=baggage, certificate_count=certificates, certificate_status=payment_status))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return result
    checks = [value for row in result["checks"] for key, value in row.items() if key.endswith("_status")]
    status = ("no_target_write" if not result["target_write_calls"] else
              "target_violation_detected" if "violation" in checks else
              "target_checks_passed" if all(v == "pass" for v in checks) else "unresolved_target_checks")
    return dict(result, status=status)


def review_original_decisions(packet, rubrics):
    """Historical replies are calibration examples, never a new experiment baseline."""
    validation = validate_rubrics(packet, rubrics)
    rules = {r["case_id"]: r for r in rubrics["cases"]}
    return dict(format="agent-memory-written-source-calibration/v1", validation=validation,
        packet_sha256=digest(canonical(packet)), rubric_sha256=digest(canonical(rubrics)),
        scope="historical_public_reply_calibration_not_new_memory_effect",
        new_model_or_catalog_calls=0, cases=[dict(case_id=c["case_id"],
            result=screen_reply(c, rules[c["case_id"]], c["original_public_reply"], finish_reason="tool_calls"))
            for c in packet["cases"]])


def review_recorded_replies(root, packet, rubrics):
    """Screen audited new replies against their exact public source, without effect aggregation."""
    from agent_memory_eval.analysis.snapshot_replay import audit_snapshot_replays
    from agent_memory_eval.recording.experience import ReplayContextReader
    from agent_memory_eval.recording.journal import BlobStore, read_experiment

    validation = validate_rubrics(packet, rubrics)
    evidence = read_experiment(root)
    audit = audit_snapshot_replays(root, evidence=evidence)
    scan, state, _ = evidence
    blobs = BlobStore(root)
    cases = {c["source_request_sha256"]:c for c in packet["cases"]}
    if len(cases) != len(packet["cases"]):
        raise ContractError("Review packet repeats a source request")
    rules = {r["case_id"]:r for r in rubrics["cases"]}
    finished = {e["payload"]["request_id"]:e["payload"] for e in scan["events"].values()
                if e["type"] == "model.request_finished"}
    reader = ReplayContextReader()
    rows = []
    for run in audit["replays"]:
        case = cases.get(run["source_request_sha256"])
        if case is None:
            raise ContractError("Recorded reply has no matching reviewed source request")
        source = reader.read(blobs, state["snapshot_replays"][run["run_id"]]["decision_ref"])
        if case["messages"] != [public_message(m) for m in source["wire"]["messages"]]:
            raise ContractError("Review packet public context differs from the recorded source")
        checked = None
        if run["status"] == "response":
            raw = blobs.get(finished[run["request_id"]]["response_ref"])
            checked = screen_reply(case, rules[case["case_id"]], blobs.get(run["message_ref"]),
                                   finish_reason=raw["choices"][0]["finish_reason"])
        rows.append(dict(run_id=run["run_id"], case_id=case["case_id"], condition_id=run["condition_id"],
            request_sha256=run["request_sha256"], record_kind=run["record_kind"],
            delivery_status=run["delivery_status"], unknown_cost=run["unknown_cost"], stop_required=run["stop_required"],
            error_code=run["error_code"], result=checked))
    return dict(format="agent-memory-written-replay-review/v1", validation=validation,
        packet_sha256=digest(canonical(packet)), rubric_sha256=digest(canonical(rubrics)), replays=rows,
        scope="public_target_checks_not_official_success_or_policy_effect", business_tools_executed=False,
        new_model_or_catalog_calls=0, independent_adjudication=False, policy_effect_estimated=False)
