"""Read-only diagnosis of the fixed baseline: wire exposure, strict replay, grading probes.

Default mode verifies the preserved diagnosis. --write-new creates it once.
The probes change isolated local tool state or copied text only. They do not
continue a model, establish a memory effect, or replace any official score.
"""

import argparse
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.adapters.tau2 import native_modules
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.recording.journal import BlobStore
from agent_memory_eval.runner.tau2_baseline import (
    build_baseline_orchestrator, preflight_baseline, resolved_connection,
)
from review_tau2_baseline_screening import public_message, write_tool_names


ROOT = Path(__file__).resolve().parents[1]
RESULT = Path("artifacts/pilots/20261115-tau2-baseline-screening-results.json")
REVIEW = Path("artifacts/analyses/20261115-tau2-baseline-behavior-review.json")
LOCK = Path("artifacts/pilots/20261115-tau2-baseline-screening-protocol.json")
OUTPUT = Path("artifacts/analyses/20261115-tau2-baseline-failure-diagnosis.json")
DECISIONS = {
    ("17", 1): 13, ("42", 1): 24, ("20", 1): 4, ("21", 1): 21,
    ("7", 1): 42, ("39", 1): 14, ("11", 2): 18, ("23", 2): 27,
    ("14", 2): 44, ("42", 2): 22, ("39", 2): 14,
}


def file_ref(path):
    return dict(path=str(path.relative_to(ROOT)), sha256=digest(path.read_bytes()))


def check_frozen_files():
    counts = []
    for name in (LOCK, Path("artifacts/pilots/20261114-tau2-delivery-development-protocol.json")):
        lock = read_json((ROOT / name).read_bytes())
        refs = lock["implementation_files"] + [v for v in lock.values()
            if isinstance(v, dict) and "path" in v and "sha256" in v]
        for ref in refs:
            assert digest((ROOT / ref["path"]).read_bytes()) == ref["sha256"], ref["path"]
        counts.append(len(refs))
    return counts


def replay_projection(message):
    projected = public_message(message)
    for key in ("requestor", "execution_status"):
        if key in message:
            projected[key] = message[key]
    return projected


def decide_request(run, report, index, events, blobs):
    message = report["messages"][index]
    response_id = message["raw_data"]["id"]
    found = []
    for event in events:
        payload = event["payload"]
        if event["run_id"] != run["run_id"] or event["type"] != "model.request_finished" or payload["status"] != "response":
            continue
        response = blobs.get(payload["response_ref"])
        if response["id"] == response_id:
            found.append((event, response))
    assert len(found) == 1, "Decision must match exactly one original provider response"
    finish, response = found[0]
    start = next(e for e in events if e["type"] == "model.request_started"
        and e["payload"]["request_id"] == finish["payload"]["request_id"])
    assert start["run_id"] == run["run_id"] and start["payload"]["actor_role"] == "worker"
    wire = blobs.get(start["payload"]["request_ref"])
    assert response["choices"][0]["message"].get("content") == message.get("content")
    original_tools = [m for m in report["messages"][:index] if m["role"] == "tool"]
    inclusion = []
    for tool in original_tools:
        matches = [m for m in wire["messages"] if m["role"] == "tool" and m.get("tool_call_id") == tool["id"]]
        assert len(matches) == 1 and matches[0]["content"] == tool["content"], "A prior tool output is absent or changed on the wire"
        inclusion.append(dict(tool_call_id=tool["id"], content_sha256=digest(tool["content"].encode())))
    return dict(message_index=index, public_message=public_message(message),
        request_id=start["payload"]["request_id"], request_event_id=start["event_id"],
        request_ref=start["payload"]["request_ref"], response_event_id=finish["event_id"],
        response_ref=finish["payload"]["response_ref"], finish_reason=response["choices"][0]["finish_reason"],
        prior_tool_outputs_on_wire=inclusion), wire


def build_diagnosis():
    protected = check_frozen_files()
    receipt = read_json((ROOT / RESULT).read_bytes())
    review = read_json((ROOT / REVIEW).read_bytes())
    assert digest((ROOT / REVIEW).read_bytes()) == receipt["behavior_review_ref"]["sha256"]
    assert digest((ROOT / review["method_ref"]["path"]).read_bytes()) == review["method_ref"]["sha256"]
    lock = read_json((ROOT / LOCK).read_bytes())
    root = ROOT / lock["planned_output"]
    blobs = BlobStore(root)
    log_bytes = (root / "log.jsonl").read_bytes()
    assert digest(log_bytes) == receipt["audit"]["log_sha256"]
    assert digest((root / "baseline-summary.json").read_bytes()) == receipt["audit"]["summary_sha256"]
    records = [read_json(line) for line in log_bytes.splitlines()]
    events = [event for record in records for event in record["events"]]
    control = blobs.get(blobs.get(records[-1]["state_ref"])["baseline_screening"]["control_ref"])
    policy_path = "data/tau2/domains/airline/policy.md"
    policy = blobs.get(control["assets"][policy_path]["text_ref"]).strip()
    writes = write_tool_names(blobs.get(control["assets"]["src/tau2/domains/airline/tools.py"]["text_ref"]))
    tasks = {t["id"]: t for t in read_json(blobs.get(control["assets"]["data/tau2/domains/airline/tasks.json"]["text_ref"]).encode())}
    starts = {e["payload"]["request_id"]: e for e in events if e["type"] == "model.request_started"}
    finishes = [e for e in events if e["type"] == "model.request_finished"]
    reasons, modes, request_sizes, errors = Counter(), {}, [], []
    worker_policies = 0
    for event in finishes:
        payload = event["payload"]
        start = starts[payload["request_id"]]
        wire = blobs.get(start["payload"]["request_ref"])
        actor = start["payload"]["actor_role"]
        request_sizes.append(len(canonical(wire)))
        assert not any(t["function"]["name"].startswith("memory_") for t in wire.get("tools", []))
        role_mode = {k: wire[k] for k in ("model", "provider", "reasoning", "temperature", "max_tokens", "max_completion_tokens") if k in wire}
        assert actor not in modes or modes[actor] == role_mode
        modes[actor] = role_mode
        if actor == "worker":
            assert policy in wire["messages"][0]["content"], "Full pinned policy is absent from a worker request"
            worker_policies += 1
        response = blobs.get(payload["response_ref"])
        if payload["status"] == "response":
            reasons.update([response["choices"][0]["finish_reason"]])
        else:
            errors.append(dict(run_id=event["run_id"], request_event_id=start["event_id"],
                response_event_id=event["event_id"], request_ref=start["payload"]["request_ref"],
                response_ref=payload["response_ref"], actor_role=actor, response=response,
                delivery_status=payload["delivery_status"], cost_status=payload["cost_status"]))

    attempts = Counter()
    def blocked(kind):
        def stop(*args, **kwargs):
            attempts[kind] += 1
            raise AssertionError(f"{kind} is forbidden in this offline diagnosis")
        return stop

    cases, probes, gold_cache = [], {}, {}
    with ExitStack() as stack:
        for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex"):
            stack.enter_context(patch(target, side_effect=blocked("network_attempts")))
        plan, checkout = preflight_baseline(control["profile"], ROOT / "data/tau2-bench", require_ready=True)
        assert plan["asset_hashes"] == lock["benchmark_asset_hashes"]
        agent_module, user_module, llm_utils = native_modules(checkout)
        from loguru import logger
        logger.disable("tau2")
        from pydantic import TypeAdapter
        from tau2.data_model.message import Message
        from tau2.data_model.semantic_communication import CommunicateScorer, CommunicateScoring
        from tau2.evaluator import evaluator_communicate
        from tau2.registry import registry
        from tau2.runner.build import _build_env_kwargs
        for module, name in ((agent_module, "generate"), (user_module, "generate"),
                (llm_utils, "generate"), (llm_utils, "completion"), (evaluator_communicate, "generate")):
            stack.enter_context(patch.object(module, name, side_effect=blocked("model_attempts")))
        adapter = TypeAdapter(list[Message])
        scoring = CommunicateScoring(scorer=CommunicateScorer.SUBSTRING)

        for run in review["runs"]:
            if run.get("official_success") is not False:
                continue
            task_id, repetition = run["task_id"], run["repetition"]
            config = resolved_connection(plan, run)
            orch, native = build_baseline_orchestrator(config, tasks[task_id])
            task = orch.task
            env_kwargs = _build_env_kwargs(native, task, language_context=orch.call_context.language)
            constructor = registry.get_env_constructor(config["benchmark"]["domain"])
            initial = task.initial_state
            def fresh():
                env = constructor(**deepcopy(env_kwargs))
                env.set_state(initialization_data=initial.initialization_data if initial else None,
                    initialization_actions=initial.initialization_actions if initial else None,
                    message_history=initial.message_history or [] if initial else [])
                return env
            if task_id not in gold_cache:
                gold = fresh()
                for action in task.evaluation_criteria.actions or []:
                    gold.make_tool_call(tool_name=action.name, requestor=action.requestor, **deepcopy(action.arguments))
                gold_cache[task_id] = (gold.get_db_hash(), gold.get_user_db_hash())
            expected = gold_cache[task_id]
            report = blobs.get(run["report_ref"])
            messages = adapter.validate_python([replay_projection(m) for m in report["messages"]])
            actual = fresh()
            actual.set_state(initialization_data=None, initialization_actions=None, message_history=messages, strict=True)
            assert actual.get_db_hash() == run["final_db_hash"], "Strict replay differs from the saved live final DB"
            db_match = (actual.get_db_hash(), actual.get_user_db_hash()) == expected
            comm = evaluator_communicate.CommunicateEvaluator.calculate_reward(task, messages, scoring=scoring)
            assert float(db_match) == report["reward_info"]["db_check"]["db_reward"]
            assert comm.reward == report["reward_info"]["reward_breakdown"]["COMMUNICATE"]
            assert set(report["reward_info"]["reward_basis"]) == {"DB", "COMMUNICATE"}
            assert float(db_match) * comm.reward == run["official_score"]
            decision, wire = decide_request(run, report, DECISIONS[(task_id, repetition)], events, blobs)
            case = dict(task_id=task_id, repetition=repetition, run_id=run["run_id"], report_ref=run["report_ref"],
                strict_replay_final_db_hash=actual.get_db_hash(), live_final_db_hash_verified=True,
                db_reward=float(db_match), communicate_reward=comm.reward, official_score=run["official_score"],
                decision=decision, full_policy_on_decision_wire=policy in wire["messages"][0]["content"])
            cases.append(case)

            if task_id == "14":
                changed = fresh()
                edits = []
                for index, message in enumerate(report["messages"]):
                    for call in message.get("tool_calls") or []:
                        if call["name"] not in writes:
                            continue
                        args = deepcopy(call["arguments"])
                        if call["name"] == "book_reservation":
                            assert args["total_baggages"] == 6
                            args["total_baggages"] = 0
                            edits.append(dict(message_index=index, tool_call_id=call["id"], field="total_baggages", before=6, after=0))
                        changed.make_tool_call(tool_name=call["name"], requestor="assistant", **args)
                assert len(edits) == 1
                assert (changed.get_db_hash(), changed.get_user_db_hash()) == expected
                probes["task14_baggage_field"] = dict(run_id=run["run_id"], edits=edits,
                    original_db_reward=float(db_match), changed_db_match=True, original_communicate_reward=comm.reward,
                    interpretation="Only the DB field causing the official mismatch is identified. Hidden zero-baggage intent is not proven to have been explicit to the worker.")

            if task_id == "7":
                copied = deepcopy(messages)
                assert "708" in copied[42].content and "1628" not in copied[42].content
                copied[42].content = copied[42].content.replace("708", "1,628")
                changed_comm = evaluator_communicate.CommunicateEvaluator.calculate_reward(task, copied, scoring=scoring)
                assert comm.reward == 0 and changed_comm.reward == 1
                before = fresh()
                user = before.tools.db.users["daiki_muller_1116"]
                date = "2024-05-15"
                def upcoming(env):
                    rows = []
                    for rid in user.reservations:
                        res = env.tools.db.reservations[rid]
                        if res.status != "cancelled" and any(f.date > date for f in res.flights):
                            rows.append(dict(reservation_id=rid, paid=sum(p.amount for p in res.payment_history)))
                    return rows
                before_rows, after_rows = upcoming(before), upcoming(actual)
                others = [r for r in before_rows if r["reservation_id"] not in {"XEHM4B", "59XX6W"}]
                assert sum(r["paid"] for r in before_rows) == 1628
                assert sum(r["paid"] for r in others) == sum(r["paid"] for r in after_rows) == 708
                probes["task7_fixed_target"] = dict(run_id=run["run_id"], expected_information=task.evaluation_criteria.communicate_info,
                    original_db_reward=float(db_match), original_communicate_reward=comm.reward,
                    copied_answer_708_replaced_with_1628_communicate_reward=changed_comm.reward,
                    before_upcoming=before_rows, after_upcoming=after_rows, other_than_requested_cancellations=others,
                    source_scenario=tasks[task_id]["user_scenario"], actual_question=public_message(report["messages"][32]),
                    interpretation="The fixed target counts all four original upcoming reservations, while the realized question asks for other remaining flights. Moving the question earlier alone does not settle its scope.")

            if task_id == "39" and repetition == 1:
                before = fresh()
                rows = []
                now = datetime.fromisoformat("2024-05-15T15:00:00")
                for rid in ("8C8K4E", "LU15PA", "MSJ4OA", "UDMOP1", "XAZ3C0"):
                    res = before.tools.db.reservations[rid]
                    rows.append(dict(reservation_id=rid, cabin=res.cabin, insurance=res.insurance,
                        created_at=res.created_at, hours_since_booking=(now-datetime.fromisoformat(res.created_at)).total_seconds()/3600,
                        flights=[dict(flight_number=f.flight_number, date=f.date,
                            status=before.tools.db.flights[f.flight_number].dates[f.date].status) for f in res.flights]))
                changed = fresh()
                changed.set_state(initialization_data=None, initialization_actions=None, message_history=messages, strict=True)
                result = changed.make_tool_call(tool_name="cancel_reservation", requestor="assistant", reservation_id="MSJ4OA")
                assert (changed.get_db_hash(), changed.get_user_db_hash()) == expected
                probes["task39_policy_reference"] = dict(run_id=run["run_id"], reservations=rows,
                    public_cancellation_reason=public_message(report["messages"][3]),
                    extra_analysis_only_action=dict(name="cancel_reservation", arguments=dict(reservation_id="MSJ4OA")),
                    tool_accepted_extra_cancel=result.status == "cancelled", original_db_reward=float(db_match),
                    changed_db_match=True, source_scenario=tasks[task_id]["user_scenario"],
                    interpretation="This artificial extra cancellation matches the reference DB; the actual given-up-seat reason is not health or weather. Backend acceptance does not establish policy permission.")

            if task_id == "23":
                before = fresh()
                calls = [c for m in report["messages"] for c in m.get("tool_calls") or [] if c["name"] == "book_reservation"]
                assert len(calls) == 1
                args = calls[0]["arguments"]
                profile = before.tools.db.users[args["user_id"]]
                certificate_count = sum(profile.payment_methods[p["payment_id"]].source == "certificate" for p in args["payment_methods"])
                reference_books = [a for a in task.evaluation_criteria.actions if a.name == "book_reservation"]
                assert certificate_count == 3 and len(reference_books) == 3
                assert all(sum(profile.payment_methods[p["payment_id"]].source == "certificate" for p in a.arguments["payment_methods"]) == 1 for a in reference_books)
                probes["task23_backend_acceptance"] = dict(run_id=run["run_id"], actual_booking_count=1,
                    actual_certificates_in_one_booking=certificate_count, reference_booking_count=len(reference_books),
                    reference_certificates_per_booking=[1, 1, 1], strict_replay_accepted_original_booking=True,
                    interpretation="The tool accepts three certificates in one reservation even though the worker's policy limits each reservation to one. The reference separates passengers into three bookings.")

            if task_id == "20":
                meta_event = next(e for e in events if e["run_id"] == run["run_id"] and e["payload"].get("origin") == "baseline.run_control")
                meta = blobs.get(meta_event["payload"]["control_ref"])
                probes["task20_clock_and_drift"] = dict(run_id=run["run_id"],
                    benchmark_clock="2024-05-15 15:00:00 EST", worker_clock_on_wire="2024-05-15 15:00:00 EST" in wire["messages"][0]["content"],
                    year_2026_in_worker_system="2026" in wire["messages"][0]["content"],
                    explicit_benchmark_clock_in_user_system="2024-05-15" in meta["systems"]["user_simulator"],
                    false_past_claim=public_message(report["messages"][4]), user_year_change=public_message(report["messages"][5]),
                    source_scenario=tasks[task_id]["user_scenario"],
                    interpretation="The worker received the benchmark clock. Its false past-date claim preceded the user's invented 2025 year; a provider-internal clock cause is not observable.")

        assert len(cases) == 11 and not attempts
        source_paths = sorted(set(plan["asset_hashes"]) | {"src/tau2/evaluator/evaluator_communicate.py"})
        native_sources = {p: digest((checkout / p).read_bytes()) for p in source_paths}
        for path, sha in plan["asset_hashes"].items():
            assert native_sources[path] == sha

    assert digest((root / "log.jsonl").read_bytes()) == receipt["audit"]["log_sha256"]
    assert digest((root / "baseline-summary.json").read_bytes()) == receipt["audit"]["summary_sha256"]
    assert check_frozen_files() == protected
    return dict(format="agent-memory-tau2-baseline-failure-diagnosis/v1", visibility="analysis_only_never_worker_or_writer_input",
        method_ref=file_ref(Path(__file__).resolve()), sources=[file_ref(ROOT / p) for p in (RESULT, REVIEW, LOCK)],
        raw_log_sha256=receipt["audit"]["log_sha256"], raw_summary_sha256=receipt["audit"]["summary_sha256"],
        benchmark_revision=receipt["audit"]["native_revision"], native_source_hashes=native_sources,
        wire_checks=dict(requests=len(finishes), worker_requests_with_full_policy=worker_policies,
            response_finish_reasons=dict(reasons), max_serialized_request_bytes=max(request_sizes),
            memory_tools_present=False, role_modes=modes, errors=errors),
        strict_replays=cases, diagnostic_probes=probes, unchanged_frozen_reference_counts=protected,
        original_logs_and_summary_unchanged=True, network_attempts=0, model_attempts=0,
        limits=["Single unblinded analyst; not independent adjudication.",
            "Artificial tool/text probes are not model continuations or memory interventions.",
            "All official scores and missing allocations are preserved; no adjusted success rate.",
            "The HTTP error body was reduced by the original transport; exact upstream origin is unavailable."])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-new", action="store_true")
    args = parser.parse_args()
    output = ROOT / OUTPUT
    if args.write_new:
        assert not output.exists(), "Preserve previous diagnostics"
    result = build_diagnosis()
    if args.write_new:
        with output.open("xb") as stream:
            stream.write(canonical(result) + b"\n")
    else:
        assert canonical(result) == canonical(read_json(output.read_bytes())), "Diagnosis differs from preserved source evidence"
    print(json.dumps(dict(status="failure_diagnosis_verified", strict_replays=len(result["strict_replays"]),
        probes=len(result["diagnostic_probes"]), wire_checks=result["wire_checks"]["response_finish_reasons"],
        model_attempts=result["model_attempts"], network_attempts=result["network_attempts"]), indent=2))


if __name__ == "__main__":
    main()
