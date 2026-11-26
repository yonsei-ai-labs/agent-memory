"""Export masked evidence or audit the sealed airline payment-allocation pilot offline."""

import argparse
from decimal import Decimal
import json
from pathlib import Path

from agent_memory_eval.adapters.tau2 import native_modules, verify_checkout
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path
from scripts.run_airline_allocation import PROTOCOL, grade, schedule


def case_code(run_id):
    return digest(canonical(["airline-allocation-blind-v1", run_id]))[:12]


def evidence(run, *, final):
    def load(name):
        return read_json((run / name).read_bytes())
    close = load("completion.json") if final else None
    if final and any(digest((run / name).read_bytes()) != close[key]
                     for key, name in (("allocation_sha256", "allocations.json"), ("control_sha256", "control.json"))):
        raise ContractError("Run controller seals differ from the archived files")
    control, rows = load("control.json"), load("allocations.json")
    study = read_json((ROOT / PROTOCOL).read_bytes())
    for row, slot in zip(rows, schedule(study), strict=True):
        if any(row.get(k) != v for k, v in slot.items()):
            raise ContractError("Stored schedule differs from the fixed assignments")
    writers = {}
    for row in rows:
        if row["phase"] != "write":
            continue
        if row["status"] in {"planned", "started"}:
            raise ContractError("Writer blinding packet requires all five writer dispositions")
        result = row.get("result")
        bank = None
        if row["status"] in {"completed", "empty"}:
            bank = read_form_bank(safe_path(run, row["directory"]), expected_log_sha256=result["log_sha256"])
            if (bank["run_id"] != row["run_id"] or bank["arm"] != row["arm"]
                    or bank["snapshot_id"] != result["snapshot_id"] or bank["public_context_sha256"] != study["source_public_sha256"]):
                raise ContractError("Writer does not match the assigned public source and sealed snapshot")
        writers[row["arm"]] = (row, bank)
    return control, rows, writers, close


def writer_packet(run):
    control, _, writers, _ = evidence(run, final=False)
    packet, public = [], None
    for row, bank in writers.values():
        notes = [{"title": i["title"], "body": i["body"]} for i in bank["items"]] if bank is not None else None
        packet.append(dict(case=case_code(row["run_id"]), status=row["status"], notes=notes))
        if public is None and bank is not None:
            folder = safe_path(run, row["directory"])
            scan, _, _ = read_experiment(folder)
            blobs = BlobStore(folder)
            event, = [e for e in scan["events"].values() if e["type"] == "business.transition"
                      and e["payload"].get("origin") == "forms.public_experience_import"]
            source = blobs.get(event["payload"]["source_ref"])
            public = blobs.get(source["public_context_ref"])
            if digest(canonical(public)) != control["preflight"]["public_sha256"]:
                raise ContractError("The blind review packet did not receive the shared public experience")
    return dict(public_experience=public, writers=sorted(packet, key=lambda r: r["case"]))


def booking_audit(task, sim, actual, initial):
    """Compare the airline reference and observed receipts, including generated IDs."""
    from tau2.data_model.message import ToolCall
    from tau2.domains.airline.environment import get_environment

    reference = get_environment()
    actions = task["evaluation_criteria"]["actions"]
    for index, action in enumerate(actions):
        reply = reference.get_response(ToolCall(id=f"reference-{index}", name=action["name"], arguments=action["arguments"]))
        if reply.error:
            raise ContractError("A reference action failed during the booking audit")
    gold = reference.tools.db
    if (actual.get_hash() == gold.get_hash()) != sim.reward_info.db_check.db_match:
        raise ContractError("The receipt audit disagrees with the recorded database score")
    expected = {k: v for k, v in gold.reservations.items() if k not in initial}
    observed = {k: v for k, v in actual.reservations.items() if k not in initial}
    receipts = {}
    for key in sorted(set(expected) | set(observed)):
        pair = {}
        for label, booking in (("reference", expected.get(key)), ("observed", observed.get(key))):
            pair[label] = None if booking is None else dict(
                passengers=[p.first_name for p in booking.passengers], destination=booking.destination,
                total_baggages=booking.total_baggages, payments=[p.model_dump(mode="json") for p in booking.payment_history])
        receipts[key] = pair
    old_id = actions[0]["arguments"]["reservation_id"]
    second, third = sorted(expected)[1:]
    swapped_hash = None
    if second in observed and third in observed:
        copy = actual.model_copy(deep=True)
        copy.reservations[second] = observed[third].model_copy(update={"reservation_id": second})
        copy.reservations[third] = observed[second].model_copy(update={"reservation_id": third})
        swapped_hash = copy.get_hash()
    wallet = None
    profile_calls = {c.id for m in sim.messages if m.role == "assistant" for c in (m.tool_calls or [])
                     if c.name == "get_user_details"}
    for index, message in enumerate(sim.messages):
        if (message.role == "tool" and message.id in profile_calls and not message.error
                and message.content and message.content.lstrip().startswith("{")):
            user = json.loads(message.content)
            if isinstance(user, dict) and "payment_methods" in user and "saved_passengers" in user:
                wallet = dict(turn=index, passengers=[p["first_name"] for p in user["saved_passengers"]],
                              certificates=[k for k, p in user["payment_methods"].items()
                                            if p.get("source") == "certificate" and p.get("amount") == 250])
                break
    return dict(reference_db=gold.get_hash(), observed_db=actual.get_hash(), transposed_ids=[second, third],
                db_if_only_these_ids_transposed=swapped_hash, current_wallet=wallet, new_receipts=receipts,
                original_reservation_status=dict(reference=gold.reservations[old_id].status,
                                                 observed=actual.reservations[old_id].status))


def report(run, mode, workspace):
    if mode == "writers":
        return writer_packet(run)
    native_modules(verify_checkout(workspace / "data/tau2-bench"))
    from loguru import logger
    from tau2.data_model.simulation import SimulationRun
    from tau2.domains.airline.environment import get_environment
    logger.disable("tau2")
    control, rows, writers, close = evidence(run, final=True)
    seen_sessions, seen_banks, episodes = set(), set(), []
    for row in rows:
        result = row.get("result")
        if result:
            if result["session_id"] in seen_sessions or result["bank_id"] in seen_banks:
                raise ContractError("Jobs unexpectedly shared a live session or private bank")
            seen_sessions.add(result["session_id"])
            seen_banks.add(result["bank_id"])
        if row["phase"] == "write":
            continue
        entry = dict(case=case_code(row["run_id"]), writer_case=case_code(writers[row["arm"]][0]["run_id"]),
                     form=row["arm"], request=row["task_variant"], status=row["status"])
        episodes.append(entry)
        if not result:
            entry["reason"] = row.get("reason")
            continue
        folder = safe_path(run, row["directory"])
        if digest((folder / "log.jsonl").read_bytes()) != result["log_sha256"]:
            raise ContractError("Episode journal differs from the sealed allocation")
        scan, state, _ = read_experiment(folder)
        blobs = BlobStore(folder)
        assignment = state["assignments"][row["run_id"]]
        bank = writers[row["arm"]][1]
        if (assignment["source_writer_snapshot_id"] != bank["snapshot_id"] or
                assignment["source_writer_run_id"] != bank["run_id"] or result["bank_sizes"]["items"] != len(bank["items"])):
            raise ContractError("The native episode read the wrong writer")
        task = control["task_variants"][row["task_id"]][row["task_variant"]]
        checks = control["preflight"]["native_checks"][row["task_variant"]]
        if digest(canonical(task)) != checks["task_sha256"]:
            raise ContractError("The customer task no longer matches its registered hash")
        for event in scan["events"].values():
            if event["type"] != "model.request_started":
                continue
            p = event["payload"]
            wire = blobs.get(p["request_ref"])
            if p["actor_role"] == "worker" and json.loads(wire["messages"][1]["content"]) != {"items": bank["items"]}:
                raise ContractError("Worker wire did not contain exactly its assigned sealed bank")
            if p["actor_role"] == "user_simulator" and digest(wire["messages"][0]["content"].encode()) != checks["customer_directions_sha256"]:
                raise ContractError("The simulator received another scenario")
        entry.update(official_success=result["official_success"], grading_status=result["grading_status"],
                     http_calls=result["http_calls"], usage=result["usage_by_role"], error_code=result["error_code"],
                     memory_response_spans=result["normal_response_memory_spans"])
        ref = result["native_report_ref"]
        if not ref:
            if result.get("partial_trajectory_ref"):
                entry["partial_trajectory_ref"] = result["partial_trajectory_ref"]
            continue
        sim = SimulationRun.model_validate(blobs.get(ref))
        if mode == "dialogue":
            entry["turns"] = [dict(i=i, role=m.role, text=m.content) for i, m in enumerate(sim.messages)
                              if m.role in ("assistant", "user") and m.content]
            continue
        if result["grading_status"] != "graded":
            continue
        strict = grade(task, sim, sim.messages)
        if strict.reward != result["official_score"]:
            raise ContractError("New strict replay disagrees with the recorded native grade")
        env = get_environment()
        initial = set(env.tools.db.reservations)
        env.set_state(None, None, sim.messages, strict=True)
        if env.get_db_hash() != result["final_db_hash"]:
            raise ContractError("Strict final database replay differs from the observed native result")
        if mode == "booking-audit":
            entry["booking_audit"] = booking_audit(task, sim, env.tools.db, initial)
            continue
        bookings = [b for k, b in env.tools.db.reservations.items() if k not in initial and b.status != "cancelled"]
        expected = {a["arguments"]["passengers"][0]["first_name"]: a["arguments"]["payment_methods"][0]["payment_id"]
                    for a in task["evaluation_criteria"]["actions"][2:4]}
        observed = {p: [] for p in expected}
        for booking in bookings:
            for passenger in booking.passengers:
                if passenger.first_name in observed:
                    certs = [(p.payment_id, p.amount) for p in booking.payment_history if p.payment_id.startswith("certificate_")]
                    observed[passenger.first_name].append(certs)
        calls = [c.name for m in sim.messages if m.role == "assistant" for c in (m.tool_calls or [])]
        entry.update(official_score=strict.reward, native_db_match=strict.db_check.db_match,
                     communications=[c.met for c in strict.communicate_checks],
                     allocation_correct=all(observed[p] == [[(v, 250)]] for p, v in expected.items()),
                     active_booking_certificates=observed, customer_turns=sum(m.role == "user" for m in sim.messages),
                     business_calls=len([name for name in calls if name != "think"]), think_calls=calls.count("think"))
    if mode == "dialogue":
        return sorted([dict(case=e["case"], turns=e.get("turns")) for e in episodes], key=lambda e: e["case"])
    if mode == "booking-audit":
        return sorted([dict(case=e["case"], status=e["status"], audit=e.get("booking_audit")) for e in episodes],
                      key=lambda e: e["case"])
    writer_rows = [dict(case=case_code(r["run_id"]), form=r["arm"], status=r["status"],
                        items=len(b["items"]) if b is not None else None, billing=r.get("result", {}).get("billing_by_role"))
                   for r, b in writers.values()]
    billing = [cell for row in rows for cell in row.get("result", {}).get("billing_by_role", {}).values()]
    total = sum((Decimal(c["reported_usd"]) for c in billing), Decimal(0))
    if sum(c["http_calls"] for c in billing) != close["http_calls"] or abs(total - Decimal(str(close["reported_usd"]))) > Decimal("1e-8"):
        raise ContractError("Aggregated per-job calls or cost disagree with the completion ledger")
    return dict(implementation=control["implementation"]["implementation_commit"], allocation_sha256=close["allocation_sha256"],
                first_pass_success=control["preflight"]["baseline_success"], writers=writer_rows, consultations=episodes,
                http_calls=close["http_calls"], confirmed_usd=str(total), reserved_usd=close.get("reserved_usd", "0"),
                stop_reason=close["stop_reason"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("mode", choices=("writers", "dialogue", "results", "booking-audit"))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(report(args.run.resolve(), args.mode, args.workspace.resolve()), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
