"""Check what the two observed first actions can return on the reset airline task."""

import argparse
from collections import Counter
from pathlib import Path
import re
from typing import Any

from agent_memory_eval.adapters.tau2 import PIN, native_modules, verify_checkout
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, safe_path


RESULT = {
    "path": "artifacts/analyses/20261118-format-content-decisions.json",
    "sha256": "864b8bf8822638a4225e4f833537685533d2db8cf45800473ae0365f97b1d132",
}
PRIOR_WIRE = "cecfad9ba04da7d50001e77e02d52c04336b9ce140f2cec6aacc63bacaddd8d5"
CASE = "previous_date"


def wire_at(folder, checksum):
    return read_json(asset_text(folder, dict(path="blobs/sha256/" + checksum, sha256=checksum)).encode())


def tool_returns(messages, names):
    calls = {call["id"]: call["function"] for message in messages for call in message.get("tool_calls") or []}
    for index, message in enumerate(messages):
        if message["role"] == "tool":
            fn = calls[message["tool_call_id"]]
            if fn["name"] in names:
                yield index, fn["name"], read_json(fn["arguments"].encode()), read_json(message["content"].encode())


def observed_actions(root, result, query):
    notes = []
    call_arguments = {}
    generations = set()
    shared_requests = set()
    for arm, bank in result["first_pass_evidence"]["banks"].items():
        if bank["origin"] != "worker" or not bank["prior_date_and_flight_literals"]:
            continue
        item, = bank["items"]
        rows = [row for row in result["rows"] if row["case"] == CASE and row["arm"] == arm]
        rows.sort(key=lambda row: row["repeat"])
        if [r["repeat"] for r in rows] != [1, 2, 3, 4] or len({r["wire_sha256"] for r in rows}) != 1:
            raise ContractError("The selected bank does not have four identical inputs")
        trials = []
        for row in rows:
            folder = root / f"block-{row['block']:02d}"
            wire = wire_at(folder, row["wire_sha256"])
            raw = BlobStore(folder).get(row["response_ref"])
            message = raw["choices"][0]["message"]
            call, = message["tool_calls"]
            name = call["function"]["name"]
            args = read_json(call["function"]["arguments"].encode())
            snapshot = read_json(wire["messages"][1]["content"].encode())
            if (wire["seed"] != 3 or wire["messages"][-1]["content"] != query
                    or row["memory_input_spans"] != 2 or snapshot != {"items": [item]}):
                raise ContractError("Original request or exposed memory differs from the audited input")
            if (row["status"] != "response" or raw["id"] != row["generation_id"]
                    or (message.get("content") or "").strip()
                    or row["score"]["calls"] != [dict(name=name, arguments=args)]
                    or name not in {"get_user_details", "search_direct_flight"}):
                raise ContractError("Original reply or tool call differs from the audited response")
            if name in call_arguments and call_arguments[name] != args:
                raise ContractError("Calls with the same name used different arguments")
            call_arguments[name] = args
            del wire["messages"][1]
            shared_requests.add(digest(canonical(wire)))
            generations.add(raw["id"])
            trials.append(dict(repeat=row["repeat"], slot=row["slot"], action=name,
                               response_sha256=row["response_ref"]["sha256"]))
        body = item["body"]
        date = re.search(r"\b(?:2024-05-25|May\s+(?:25|20\s*[-–—]\s*25))\b", body, re.I)
        if date is None or "HAT276" not in body:
            raise ContractError("Selected date and flight missing from the original item")
        notes.append(dict(
            arm=arm, item_sha256=digest(canonical(item)), wire_sha256=rows[0]["wire_sha256"],
            body_characters=len(body), date_literal=date.group(), date_character_offset=date.start(),
            flight_character_offset=body.index("HAT276"),
            first_actions=dict(Counter(trial["action"] for trial in trials)), trials=trials,
        ))
    if len(notes) != 4 or len(generations) != 16 or len(shared_requests) != 1:
        raise ContractError("Expected sixteen distinct responses with one shared non-memory context")
    return sorted(notes, key=lambda note: note["arm"]), call_arguments, shared_requests.pop()


def native_reads(workspace, prior, calls, leg):
    from pydantic import TypeAdapter

    checkout = verify_checkout(workspace / "data/tau2-bench")
    native_modules(checkout)
    from tau2.domains.airline.data_model import FlightDB
    from tau2.domains.airline.environment import get_environment

    folder = checkout / "data/tau2/domains/airline"
    task, = [t for t in read_json((folder / "tasks.json").read_bytes()) if t["id"] == "24"]
    if task["initial_state"] is not None:
        raise ContractError("Task requires custom initialization")
    db = FlightDB.load(folder / "db.json")
    env = get_environment(db=db)
    as_json = TypeAdapter(Any)

    def read(name, args):
        return as_json.dump_python(env.make_tool_call(name, requestor="assistant", **args), mode="json")

    before = env.get_db_hash()
    recorded_wire = wire_at(safe_path(workspace, prior["second_pass"]["path"]), PRIOR_WIRE)
    recorded = list(tool_returns(recorded_wire["messages"], calls))
    results = {}
    references = {}
    for name, args in calls.items():
        value = read(name, args)
        (index, _, _, archived), = [entry for entry in recorded if entry[1:3] == (name, args)]
        if value != archived:
            raise ContractError("Native read differs from the earlier public no-memory tool return")
        results[name] = value
        references[name] = dict(arguments=args, archived_message_index=index, return_sha256=digest(canonical(value)))

    source = prior["source"]
    previous_wire = wire_at(safe_path(workspace, source["path"]), source["wire_sha256"])
    (_, _, _, booking), = tool_returns(previous_wire["messages"], {"book_reservation"})
    if (booking["flights"][-1] != leg or booking["user_id"] != calls["get_user_details"]["user_id"]
            or calls["search_direct_flight"] != {k: leg[k] for k in ("origin", "destination", "date")}):
        raise ContractError("Observed calls do not match the original booking or customer")
    leg_fields = ("origin", "destination", "date", "flight_number")
    reservations = {}
    for reservation_id in results["get_user_details"]["reservations"]:
        booking_details = read("get_reservation_details", {"reservation_id": reservation_id})
        reservations[reservation_id] = [{key: flight[key] for key in leg_fields} for flight in booking_details["flights"]]
    options = [{key: flight[key] for key in (*leg_fields, "status")} for flight in results["search_direct_flight"]]
    # The native search filters by the requested date but can leave the date field empty in its options.
    if any(flight["origin"] != leg["origin"] or flight["destination"] != leg["destination"]
           or flight["date"] not in (None, leg["date"]) for flight in options):
        raise ContractError("Returned route or an explicitly returned date conflicts with the verified search")
    if env.get_db_hash() != before:
        raise ContractError("The diagnostic reads changed the database")
    requested_leg = {key: leg[key] for key in leg_fields}
    return dict(
        benchmark_commit=PIN, db_sha256=digest((folder / "db.json").read_bytes()), state_hash_before_and_after=before,
        original_booking=booking["reservation_id"],
        original_booking_present_after_reset=booking["reservation_id"] in db.reservations,
        earlier_public_wire_sha256=PRIOR_WIRE, verified_returns=references, profile_reservations=reservations,
        matching_return_in_profile_reservations=any(requested_leg in itinerary for itinerary in reservations.values()),
        search_options=options, search_date_argument=calls["search_direct_flight"]["date"],
        target_available_for_queried_date=any(flight["flight_number"] == leg["flight_number"] and flight["status"] == "available"
                                            for flight in options),
    )


def review(workspace):
    result = read_json(asset_text(workspace, RESULT).encode())
    root = safe_path(workspace, "runs/" + result["study"])
    control = read_json(asset_text(root, dict(path="control.json", sha256=result["completion"]["control_sha256"])).encode())
    prior = read_json(asset_text(workspace, control["profile"]["prior_protocol_ref"]).encode())
    query = control["profile"]["cases"][CASE]["text"]
    notes, calls, shared = observed_actions(root, result, query)
    return dict(
        scope=("Post hoc single-customer review of date-bearing original notes. Benchmark reads were reconstructed offline. "
               "The observed model replies end at the first proposed tool call."),
        input_result=RESULT, case=CASE, customer_query=query, same_request_without_snapshot_sha256=shared, banks=notes,
        native_read_check=native_reads(workspace, prior, calls, result["first_pass_evidence"]["return_leg"]),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    if args.result.exists():
        parser.error("Provide a new result file")
    write_json(args.result, review(args.workspace.resolve()))
