"""Trace observed regressions in the sealed two-pass airline experiment."""
import argparse
from collections import Counter
import csv
import io
import json
from pathlib import Path
import re

from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.experiments.forms_batch import FORMS
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, safe_path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "artifacts/pilots/20261118-two-pass-forms-final-reproduction-lock.json"
OUTPUT = ROOT / "artifacts/analyses/20261118-two-pass-memory-failure-factors.json"
# Post-hoc descriptions of the observed business outcome, one per discordant pair.
MECHANISMS = {
    "fee_question_then_transfer": (360, 362, 364, 429, 432, 433, 434),
    "future_date_treated_as_past": (245, 375, 376),
    "different_booking_arguments": (322, 323, 328, 373),
    "change_before_amount_or_eligibility_confirmed": (427, 445),
}
FEE_QUESTION = re.compile(r"\b(?:fees?|insurance|waiv\w*)\b", re.IGNORECASE)


def fee_discussion(messages):
    """Read only public speech before the first flight update or handoff."""
    index, tool = next((i, c["name"]) for i, m in enumerate(messages) for c in m.get("tool_calls") or []
                       if c["name"] in {"update_reservation_flights", "transfer_to_human_agents"})
    spoken = [dict(index=i, role=m["role"], text=m["content"]) for i, m in enumerate(messages[:index])
              if m["role"] in {"assistant", "user"} and m.get("content")]
    question = next((n for n, t in enumerate(spoken) if t["role"] == "user" and FEE_QUESTION.search(t["text"])), None)
    # Include the preceding offer and the dialogue through final approval or handoff.
    return dict(first_fee_question=spoken[question]["index"] if question is not None else None,
                decision_tool=tool, decision_index=index,
                public_turns=spoken[max(0, question - 1):] if question is not None else spoken[-2:])


def jfk_flight_evidence(messages):
    """Extract usable JFK one-stop options returned for this customer's stated time."""
    replies = {m["id"]: m for m in messages if m["role"] == "tool"}
    searches, offers, bookings = [], [], []
    for i, m in enumerate(messages):
        if m["role"] == "assistant" and "JFK" in (m.get("content") or "") and "11:00" in m["content"]:
            offers.append(dict(index=i, text=m["content"]))
        for call in m.get("tool_calls") or []:
            args = call["arguments"]
            if call["name"] == "book_reservation":
                bookings.append(dict(index=i, flights=args["flights"], total_baggages=args["total_baggages"],
                                     nonfree_baggages=args["nonfree_baggages"], payment_methods=args["payment_methods"]))
            if call["name"] != "search_onestop_flight" or (args["origin"], args["destination"]) != ("JFK", "SEA"):
                continue
            eligible = []
            for route in json.loads(replies[call["id"]]["content"]):
                departure = route[0]["scheduled_departure_time_est"]
                if departure >= "11:00:00" and all(leg["available_seats"]["economy"] >= 1 for leg in route):
                    eligible.append(dict(flights=[leg["flight_number"] for leg in route], departure_est=departure,
                                         economy_fare=sum(leg["prices"]["economy"] for leg in route)))
            searches.append(dict(index=i, date=args["date"], eligible_routes=eligible))
    return dict(searches=searches, public_time_or_airport_statements=offers, bookings=bookings)


def analyze(view):
    lock = read_json(LOCK.read_bytes())
    audit = json.loads(asset_text(ROOT, lock["audit_ref"]))
    native = json.loads(asset_text(ROOT, lock["native_regrade_ref"]))
    seal = json.loads(asset_text(view, dict(path="completion.json", sha256=audit["combined_view_completion_sha256"])))
    allocations = json.loads(asset_text(view, dict(path="allocations.json", sha256=seal["allocation_sha256"])))
    if native["allocation_sha256"] != seal["allocation_sha256"]:
        raise ContractError("Native grades and sealed allocations differ")
    task_file = ROOT / "data/tau2-bench/data/tau2/domains/airline/tasks.json"
    task_data = task_file.read_bytes()
    if digest(task_data) != native["benchmark_task_file_sha256"]:
        raise ContractError("Simulator instructions differ from the benchmark used for grading")
    task33 = next(t for t in read_json(task_data) if t["id"] == "33")
    rows = {r["index"]: r for r in allocations}
    grades = {g["index"]: g for g in native["grades"]}
    cells = list(csv.DictReader(io.StringIO(asset_text(ROOT, lock["cells_ref"]))))
    controls = {(c["task_id"], c["repetition"], c["condition"]): c for c in cells}
    main = [c for c in cells if c["condition"] in FORMS and c["grading_status"] == "graded"]
    baseline_success = [c for c in main if c["control_success"] == "True"]
    source_success = [c for c in main if c["first_pass_success"] == "True"]
    direct_failures = [int(c["job"]) for c in source_success if c["official_success"] == "False"]
    mechanism = {job: label for label, jobs in MECHANISMS.items() for job in jobs}
    losses = []
    for cell in baseline_success:
        if cell["official_success"] != "False":
            continue
        job = int(cell["job"])
        block = cell["task_id"], cell["repetition"]
        empty = controls[(*block, "empty_preload")]
        if empty["grading_status"] != "graded" or grades[job]["official_score"] != 0:
            raise ContractError("The loss or its empty-preload match lacks a verified outcome")
        losses.append(dict(job=job, task=cell["task_id"], repetition=int(cell["repetition"]), form=cell["condition"],
                           mechanism=mechanism[job], source_success=cell["first_pass_success"] == "True",
                           control=int(controls[(*block, "no_memory")]["job"]), empty_control=int(empty["job"]),
                           empty_control_success=empty["official_success"] == "True", written_items=int(cell["writer_items"]),
                           db_match=grades[job]["db_match"], missing_communication=grades[job]["missing_communication"]))
    if {p["job"] for p in losses} != set(mechanism):
        raise ContractError("Post-hoc descriptions do not cover the observed losses")
    focal = []
    for c in cells:
        job = int(c["job"])
        if c["task_id"] != "33" and job not in (371, 373):
            continue
        row, grade = rows[job], grades[job]
        ref = row["result"]["report_ref"]
        if ref["sha256"] != grade["report_sha256"] or (row["task_id"], row["arm"]) != (c["task_id"], c["condition"]):
            raise ContractError("Public dialogue and audited cell differ")
        messages = BlobStore(safe_path(view, row["directory"])).get(ref)["messages"]
        evidence = fee_discussion(messages) if c["task_id"] == "33" else jfk_flight_evidence(messages)
        focal.append(dict(job=job, task=c["task_id"], repetition=int(c["repetition"]), form=c["condition"],
                          official_success=bool(grade["official_score"]), report_sha256=ref["sha256"], **evidence))
    return dict(format="agent-memory-two-pass-failure-factors/v1", method_sha256=digest(Path(__file__).read_bytes()),
                source_lock_sha256=digest(LOCK.read_bytes()), allocation_sha256=seal["allocation_sha256"],
                task_definitions_sha256=native["benchmark_task_file_sha256"], first_pass_success_graded=len(source_success),
                first_pass_success_then_failure_jobs=direct_failures, matched_no_memory_success_pairs=len(baseline_success),
                same_pass_losses=losses, loss_counts_by_observed_outcome=dict(Counter(p["mechanism"] for p in losses)),
                task33_simulator_only_instructions=task33["user_scenario"]["instructions"]["task_instructions"],
                public_dialogue_and_tool_evidence=focal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("view", type=Path, help="Sealed three-source combined-view directory")
    parser.add_argument("--write-new", action="store_true", help="Create the new descriptive analysis once")
    args = parser.parse_args()
    data = (json.dumps(analyze(args.view.resolve()), ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    if args.write_new:
        with OUTPUT.open("xb") as stream:
            stream.write(data)
    elif data != OUTPUT.read_bytes():
        raise ContractError("Published failure evidence differs from the sealed records")
    print("Verified paired regressions, task 33 dialogues, and task 20 flight search returns")


if __name__ == "__main__":
    main()
