"""Re-render the same public experience and import all eight original writer banks."""

from copy import deepcopy
from dataclasses import asdict
import re
from string import Formatter

from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import FORMS
from agent_memory_eval.experiments.forms_write import _project_context, read_form_bank
from agent_memory_eval.experiments.replay_comparison import ReplayStudy
from agent_memory_eval.experiments.return_date import MUTATIONS, captured, classify as classify_date, public_action
from agent_memory_eval.experiments.writing_comparison import _note
from agent_memory_eval.memory.context import snapshot_message
from agent_memory_eval.memory.imports import install_snapshot
from agent_memory_eval.memory.preparation import snapshot_replay_input
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.experience import ReplayContextReader
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.recording.query_variant import FORMAT


PROFILE = "protocols/pilots/tau2-format-content-decisions-v1.json"
POLICY = MemoryPolicy(True, "none", False, 1, "preload", 1, 4096, 4608)


def schedule(profile):
    if len(set(profile["arms"])) != 14 or len(profile["cases"]) != 4 or profile["repetitions"] != 4:
        raise ContractError("This counterbalance needs fourteen arms, four cases and four passes")
    def rank(*parts):
        return digest(canonical([profile["schedule_seed"], *parts]))
    base = sorted(profile["cases"], key=lambda c: rank("case", c))
    rows = []
    for i in range(4):
        for pos, case in enumerate(base[i:] + base[:i], 1):
            arms = sorted(profile["arms"], key=lambda arm: rank(case, arm))
            if i >= 2:
                arms = arms[4:] + arms[:4]
            if i % 2:
                arms.reverse()
            for arm in arms:
                rows.append(dict(slot=len(rows) + 1, block=i * 4 + pos, repeat=i + 1, case=case, arm=arm))
    return rows


def matched_notes(profile):
    facts = profile["propositions"]
    if len(facts) != 6 or len(set(facts)) != len(facts) or set(profile["renderings"]) != set(FORMS):
        raise ContractError("A matched template needs all six distinct public propositions")
    notes = {}
    for form, template in profile["renderings"].items():
        slots = [(field, spec, conv) for _, field, spec, conv in Formatter().parse(template) if field is not None]
        if slots != [(str(i), "", None) for i in range(len(facts))]:
            raise ContractError("The matched formats must include each complete proposition once in the same order")
        body = template.format(*facts)
        if any(body.count(fact) != 1 for fact in facts):
            raise ContractError("Matched rendering duplicated or changed a proposition")
        notes["matched_" + form] = _note(dict(title=profile["controlled_title"], body=body))
    return notes


def quote_rule(policy, case):
    block = policy.split("If the booking user is a " + case["membership"] + " member:", 1)[1].split("- If the booking user", 1)[0]
    rule = re.search(r"- (\d+) free checked bags? for each " + re.escape(case["cabin"]) + r" passenger\b", block)
    price = re.search(r"Each extra baggage is (\d+) dollars", policy)
    if not rule or not price or (int(rule[1]), int(price[1])) != (case["allowance_per_passenger"], case["price_each"]):
        raise ContractError("Expected tariff differs from the public policy")
    free = min(int(rule[1]), case["requested_per_passenger"]) * case["passengers"]
    paid = case["passengers"] * case["requested_per_passenger"] - free
    quote = dict(free=free, paid=paid, fee_usd=paid * int(price[1]))
    if quote != case["expected"]:
        raise ContractError("Whole-party quote differs from the frozen rubric")
    return quote


def public_evidence(source):
    projected = _project_context(source["wire"], source["response"])
    messages = projected["messages"]
    tools = {call["id"]: call["function"]["name"] for m in messages for call in m.get("tool_calls") or []}
    returns = {tools[m["tool_call_id"]]: read_json(m["content"].encode()) for m in messages if m["role"] == "tool"
               and tools[m["tool_call_id"]] in {"get_user_details", "book_reservation"}}
    reservation, member = returns["book_reservation"], returns["get_user_details"]
    leg = reservation["flights"][-1]
    if ((leg["origin"], leg["destination"], leg["date"], leg["flight_number"]) != ("SEA", "JFK", "2024-05-25", "HAT276")
            or member["membership"] != "silver" or member["user_id"] != reservation["user_id"]
            or (reservation["cabin"], len(reservation["passengers"]), reservation["total_baggages"], reservation["nonfree_baggages"])
                != ("basic_economy", 1, 1, 0)
            or not any("your two free checked bags" in (m.get("content") or "") for m in messages if m["role"] == "assistant")
            or "1 free checked bag" not in messages[-1]["content"] or "Ethan" not in messages[1]["content"]
            or "H9ZU1C" not in messages[1]["content"]):
        raise ContractError("First-pass public trajectory does not support the notes")
    return dict(source_public_context_sha256=digest(canonical(projected)), return_leg=leg, past_tier=member["membership"],
                past_cabin=reservation["cabin"], past_bags=reservation["total_baggages"],
                public_correction=messages[-1]["content"])


def banks(workspace, profile, source_hash):
    inventory = read_json(asset_text(workspace, profile["writer_inventory_ref"]).encode())
    parent = safe_path(workspace, profile["writer_root"])
    seal = read_json(asset_text(parent, dict(path="completion.json", sha256=inventory["comparison_completion_sha256"])).encode())
    allocations = read_json(asset_text(parent, dict(path="allocations.json", sha256=seal["allocation_sha256"])).encode())
    writers = {r["index"]: r for r in allocations if r["task_id"] == "24" and r["phase"] == "write"}
    if len(writers) != 8 or sorted(writers) != sorted(row["job"] for row in inventory["rows"]):
        raise ContractError("Historical writer selection is incomplete")
    controls = matched_notes(profile)
    controls["other_fact"] = _note(profile["neutral_note"])
    result = {k: dict(items=[item], revision=0, origin="experimenter") for k, item in controls.items()}
    result["empty"] = dict(items=[], revision=0, origin="empty")
    for selected in inventory["rows"]:
        row = writers[selected["job"]]
        bank = read_form_bank(safe_path(parent, row["directory"]), expected_log_sha256=selected["log_sha256"])
        form, repeat = bank["arm"], bank["repetition"]
        if ((form, repeat) != (selected["arm"], selected["repeat"]) or (form, repeat) != (row["arm"], row["repetition"])
                or bank["task_id"] != "24" or bank["public_context_sha256"] != source_hash
                or selected["source_public_context_sha256"] != source_hash or bank["snapshot_id"] != selected["snapshot"]
                or row["result"]["log_sha256"] != selected["log_sha256"] or bank["run_id"] != row["result"]["run_id"]
                or [digest(canonical(item)) for item in bank["items"]] != [i["item_sha256"] for i in selected["items"]]):
            raise ContractError("Historical snapshot differs from its writer, original experience or sealed inventory")
        result[f"written_{form}_{repeat}"] = dict(items=bank["items"], revision=bank["head_revision"], origin="worker",
            writer_run=bank["run_id"], original_snapshot=bank["snapshot_id"], writer_log_sha256=selected["log_sha256"],
            prior_date_and_flight_literals=any(i["date_literal"] and i["flight_literal"] for i in selected["items"]),
            mentions_silver=any(re.search(r"\bsilver\b", i["title"] + "\n" + i["body"], re.I) for i in bank["items"]))
    if set(result) != set(profile["arms"]) or len(result) != 14:
        raise ContractError("Experimental arms do not match the frozen banks")
    return result


def stage(journal, workspace, profile):
    if asdict(POLICY) != profile["policy"]:
        raise ContractError("Preload policy differs from the protocol")
    prior = read_json(asset_text(ROOT, profile["prior_protocol_ref"]).encode())
    prior_result = read_json(asset_text(workspace, profile["prior_calibration_result_ref"]).encode())
    if prior_result["study"] != prior["study"] or prior_result["advance_to_controlled_formats"] is not True:
        raise ContractError("The pinned calibration did not meet its prespecified criterion")
    if prior["instruction"] != profile["instruction"] or prior["notes"]["other_fact"] != profile["neutral_note"]:
        raise ContractError("Common preload instruction or same-customer comparison changed")
    reader = ReplayContextReader()
    source_ref, = captured(journal, workspace, prior["source"], [profile["first_pass_final"]])
    evidence = public_evidence(reader.read(journal.blobs, source_ref))
    bank_data = banks(workspace, profile, evidence["source_public_context_sha256"])
    evidence["banks"] = bank_data
    second = prior["second_pass"]
    opening, = captured(journal, workspace, second, [second["opening"]])
    public_policy = reader.read(journal.blobs, opening)["wire"]["messages"][0]["content"]
    decisions = {}
    for case, rule in profile["cases"].items():
        if rule["kind"] == "baggage":
            quote_rule(public_policy, rule)
        elif (rule["text"], rule["expected_search_date"]) != (prior["cases"][case]["text"], prior["cases"][case]["expected_search_date"]):
            raise ContractError("Date probe differs from the original calibration")
        decisions[case] = journal.blobs.put(dict(format=FORMAT, recorded_context_ref=opening, customer_text=rule["text"]))
    memory = MemoryService(journal, record_kind="observed")
    snapshots, wires, cells = {}, {}, {}
    for arm, bank in bank_data.items():
        items = bank["items"]
        heads = {i["id"]: i["version"] for i in items}
        snapshots[arm] = install_snapshot(memory, POLICY, heads, list(heads), bank["revision"],
            {i["version"]: journal.blobs.put(i) for i in items}, owner_run=bank.get("writer_run", "control_" + arm))
    for case, decision in decisions.items():
        cells[case] = {}
        for arm, snapshot in snapshots.items():
            planned = snapshot_replay_input(journal.blobs, journal.state, decision, snapshot, policy=POLICY,
                instruction=profile["instruction"], max_input_bytes=profile["max_input_bytes"], context_reader=reader)
            wire, items = planned["request"], bank_data[arm]["items"]
            if (wire["messages"][1] != snapshot_message(items) or wire["seed"] != 3 or wire["model"] != profile["model"]
                    or wire["messages"][-1]["content"] != profile["cases"][case]["text"] or not planned["fits_input_limit"]):
                raise ContractError("Actual wire differs from the frozen opening, item or limits")
            stripped = deepcopy(wire)
            del stripped["messages"][1]
            cells[case][arm] = dict(wire_sha256=digest(canonical(wire)), without_snapshot_sha256=digest(canonical(stripped)),
                                     input_bytes=planned["input_bytes"], expected_memory_spans=2 * len(items))
            wires[case, arm] = wire
        if (len({c["without_snapshot_sha256"] for c in cells[case].values()}) != 1
                or len({c["wire_sha256"] for c in cells[case].values()}) != len(profile["arms"])):
            raise ContractError("Within-case wires must differ only in the memory snapshot")
    return dict(decisions=decisions, snapshots=snapshots, wires=wires, cells=cells, evidence=evidence, reader=reader)


def classify(case, rule, message):
    if rule["kind"] == "date":
        return classify_date(case, rule, message)
    text, calls = public_action(message)
    review_id = digest(canonical([case, text])) if text and not calls else None
    return dict(public_text=text, calls=calls, review_id=review_id,
                unauthorized_mutation=any(c["name"] in MUTATIONS for c in calls))


STUDY = ReplayStudy(PROFILE, "format_content", POLICY, stage, schedule, classify, block_field="block")
