"""Frozen inputs and reporting rules for the single-customer return-date calibration."""

from copy import deepcopy
import re

from jsonschema import Draft202012Validator

from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.experiments.writing_comparison import _note
from agent_memory_eval.memory.context import snapshot_message
from agent_memory_eval.memory.imports import install_snapshot
from agent_memory_eval.memory.preparation import snapshot_replay_input
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.recording.experience import ReplayContextReader, capture_recorded_contexts
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.recording.query_variant import FORMAT


PROFILE = "protocols/pilots/tau2-return-date-calibration-v1.json"
POLICY = MemoryPolicy(True, "none", False, 1, "preload", 1, 256, 512)
MUTATIONS = {"book_reservation", "cancel_reservation", "send_certificate", "update_reservation_baggages",
             "update_reservation_flights", "update_reservation_passengers"}


def ordered_slots(profile):
    square = ((0, 1, 3, 2), (1, 2, 0, 3), (2, 3, 1, 0), (3, 0, 2, 1))
    def rank(*parts):
        return digest(canonical([profile["schedule_seed"], *parts]))
    result = []
    for repeat in range(1, profile["repetitions"] + 1):
        for case in sorted(profile["cases"], key=lambda name: rank(repeat, name)):
            arms = sorted(profile["arms"], key=lambda arm: rank(case, arm))
            for index in square[(repeat - 1) % 4]:
                result.append(dict(slot=len(result) + 1, repeat=repeat, case=case, arm=arms[index]))
    return result


def captured(journal, workspace, descriptor, selections):
    refs = capture_recorded_contexts(journal, safe_path(workspace, descriptor["path"]),
        [(descriptor["run_id"], s["request_id"]) for s in selections], log_sha256=descriptor["log_sha256"])
    for selection, ref in zip(selections, refs, strict=True):
        if journal.blobs.get(ref)["request_ref"]["sha256"] != selection["wire_sha256"]:
            raise ContractError("Selected public worker request differs from protocol")
    return refs


def stage(journal, workspace, profile):
    reader = ReplayContextReader()
    first, second = profile["source"], profile["second_pass"]
    first_ref, = captured(journal, workspace, first, [first])
    first_wire = reader.read(journal.blobs, first_ref)["wire"]
    messages = first_wire["messages"]
    booking_call = messages[-2]["tool_calls"][0]
    reservation = read_json(messages[-1]["content"].encode())
    leg = reservation["flights"][-1]
    if (booking_call["function"]["name"] != "book_reservation" or messages[-1]["role"] != "tool"
            or messages[-1]["tool_call_id"] != booking_call["id"] or reservation["user_id"] != "mia_kim_4397"
            or leg["origin"] != "SEA" or leg["destination"] != "JFK" or leg["date"] != "2024-05-25"
            or leg["flight_number"] != "HAT276" or messages[2]["role"] != "user"
            or "remove a passenger named Ethan" not in messages[2]["content"] or "H9ZU1C" not in messages[2]["content"]):
        raise ContractError("First-pass public evidence does not support the two control notes")
    opening, booking = captured(journal, workspace, second, [second["opening"], second["booking"]])
    approved = reader.read(journal.blobs, booking)
    original_call = approved["response"]["choices"][0]["message"]["tool_calls"][0]["function"]
    if (approved["wire"]["messages"][-1]["content"] != "Yes, book it that way."
            or original_call["name"] != "book_reservation" or read_json(original_call["arguments"].encode())
                != profile["cases"]["approved_booking"]["expected_booking"]):
        raise ContractError("Booking rubric differs from the reviewed public approval and recorded action")
    decisions = {"approved_booking": booking}
    for name in ("previous_date", "changed_date"):
        decisions[name] = journal.blobs.put(dict(format=FORMAT, recorded_context_ref=opening,
                                                 customer_text=profile["cases"][name]["text"]))
    memory = MemoryService(journal, record_kind="observed")
    snapshots = {}
    items = {k: [_note(note)] for k, note in profile["notes"].items()}
    for arm in profile["arms"]:
        values = items.get(arm, [])
        heads = {i["id"]: i["version"] for i in values}
        snapshots[arm] = install_snapshot(memory, POLICY, heads, list(heads), 0,
            {i["version"]: journal.blobs.put(i) for i in values}, owner_run="control_" + arm)
    wires, evidence = {}, {}
    for case, decision in decisions.items():
        for arm, snapshot in snapshots.items():
            request = snapshot_replay_input(journal.blobs, journal.state, decision, snapshot, policy=POLICY,
                instruction=profile["instruction"], max_input_bytes=profile["max_input_bytes"], context_reader=reader)
            wire = request["request"]
            if (wire["messages"][1] != snapshot_message(items.get(arm, [])) or wire["seed"] != 3
                    or wire["model"] != profile["model"] or not request["fits_input_limit"]):
                raise ContractError("Staged model, seed, snapshot or input size changed")
            stripped = deepcopy(wire)
            del stripped["messages"][1]
            cell = dict(wire_sha256=digest(canonical(wire)), without_snapshot_sha256=digest(canonical(stripped)),
                        input_bytes=request["input_bytes"], decision_origin=profile["cases"][case]["origin"])
            wires[case, arm] = wire
            evidence.setdefault(case, {})[arm] = cell
        cells = evidence[case]
        if (len({c["without_snapshot_sha256"] for c in cells.values()}) != 1
                or cells["empty_a"] != cells["empty_b"] or len({c["wire_sha256"] for c in cells.values()}) != 3
                or cells["other_fact"]["input_bytes"] != cells["return_fact"]["input_bytes"]):
            raise ContractError("Conditions differ outside the intended exposure, or the two notes differ in byte length")
    return dict(decisions=decisions, snapshots=snapshots, wires=wires, cells=evidence, reader=reader,
                evidence=dict(public_return_leg=leg, public_other_fact=messages[2]["content"],
                              source_wire_sha256=first["wire_sha256"]))


def classify(case, rule, message):
    text = (message.get("content") or "").strip()
    if text.startswith("{"):
        try:
            wrapped = read_json(text.encode())
            if isinstance(wrapped, dict) and set(wrapped) == {"message"} and isinstance(wrapped["message"], str):
                text = wrapped["message"].strip()
        except ValueError:
            pass
    calls = []
    for call in message.get("tool_calls") or []:
        fn = call["function"]
        try:
            args = read_json(fn["arguments"].encode())
        except ValueError:
            args = None
        calls.append(dict(name=fn["name"], arguments=args))
    single = calls[0] if len(calls) == 1 and not text else {}
    booking = "expected_booking" in rule
    name = "book_reservation" if booking else "search_direct_flight"
    expected = rule["expected_booking"] if booking else dict(origin="SEA", destination="JFK", date=rule["expected_search_date"])
    args = single.get("arguments")
    differences = None
    if single.get("name") == name and isinstance(args, dict):
        differences = sorted(k for k in expected.keys() | args.keys()
                             if k not in expected or k not in args or not Draft202012Validator({"const": expected[k]}).is_valid(args[k]))
    dates = [c["arguments"].get("date") for c in calls if c["name"] == "search_direct_flight" and isinstance(c["arguments"], dict)]
    return dict(exact_action=differences == [], differing_fields=differences, calls=calls, public_text=text,
                date_question=not calls and bool(re.search(r"\b(date|day|when)\b", text, re.I))
                    and bool(re.search(r"\?|\b(please (provide|confirm|share)|let me know)\b", text, re.I)),
                stale_date_search=case == "changed_date" and "2024-05-25" in dates,
                unauthorized_mutation=not booking and any(c["name"] in MUTATIONS for c in calls))
