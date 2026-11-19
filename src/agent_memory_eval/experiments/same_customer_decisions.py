"""Compare authentic first-pass banks and a one-line historical link after a common public lookup."""

from functools import lru_cache

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import FORMS
from agent_memory_eval.experiments.multi_workflow import POLICY, arm_id, inventory
from agent_memory_eval.experiments.replay_comparison import ReplayStudy
from agent_memory_eval.experiments.return_date import MUTATIONS, captured, public_action
from agent_memory_eval.memory.context import snapshot_message
from agent_memory_eval.memory.imports import install_snapshot
from agent_memory_eval.memory.preparation import snapshot_replay_input
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.experience import ReplayContextReader
from agent_memory_eval.recording.query_variant import FORMAT, PROFILE_READ

PROFILE = "protocols/pilots/tau2-same-customer-decisions-v1.json"


def schedule(cfg):
    def rank(*parts):
        return digest(canonical([cfg["study"], *parts]))

    slots = []
    block = 0
    for repeat in (1, 2):
        for case in sorted(cfg["cases"], key=lambda c: rank(repeat, c)):
            rule = cfg["cases"][case]
            source = rule["source"]
            originals = [(f, w) for f in ("empty", *FORMS) for w in (1, 2)]
            originals.sort(key=lambda pair: rank(case, *pair))
            twins = ["bound", "unbound"]
            twins.sort(key=lambda variant: rank(case, variant))
            for half in (0, 1) if repeat == 1 else (1, 0):
                block += 1
                group = [*originals[half * 5 : half * 5 + 5], (twins[half], 0)]
                for form, writer in sorted(group, key=lambda pair: rank(case, "within", *pair), reverse=repeat == 2):
                    slots.append(
                        dict(
                            slot=len(slots) + 1,
                            batch=block,
                            repeat=repeat,
                            case=case,
                            source=source,
                            target=rule["target"],
                            form=form,
                            writer=writer,
                            arm=arm_id(source, form, writer),
                        )
                    )
    return slots


def public_twins(anchor, bank, report):
    """Build a pair from the actual source writer and the source customer's earlier public tool returns."""
    if not any(anchor["source_excerpt"] in i["body"] for i in bank["items"]):
        raise ContractError("The shared diagnostic excerpt is absent from its authentic source writer")
    results = []
    for message in report["messages"]:
        if message["role"] == "tool" and not message.get("error"):
            try:
                data = read_json(message["content"].encode())
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("user_id") == anchor["customer"]:
                results.append(data)
    ids, old = anchor["candidates"], anchor["past_id"]
    if (
        not any(d.get("reservations") == ids for d in results)
        or not any(
            d.get("reservation_id") == old and [d.get("origin"), d.get("destination")] == anchor["route"]
            for d in results
        )
        or anchor["bound"] != {3: "item three", 9: "item nine"}.get(ids.index(old) + 1)
    ):
        raise ContractError("The frozen past relationship must be supported by first-pass public tool results")
    note_key = digest(canonical(["historical-cue/v1", bank["run_id"]]))
    line = (
        "Earlier profile entries in order: "
        + ", ".join(ids)
        + ". Earlier trip: "
        + " to ".join(anchor["route"])
        + ". Linked list entry: "
    )
    twins = {
        v: dict(
            id="m_" + note_key[:32],
            version="v_" + note_key[32:],
            title="Notes from an earlier interaction",
            body=anchor["source_excerpt"] + "\n" + line + anchor[v] + ".",
        )
        for v in ("bound", "unbound")
    }
    if len(canonical(twins["bound"])) != len(canonical(twins["unbound"])) or twins["bound"] == twins["unbound"]:
        raise ContractError("The controlled relationship line must change at equal byte length")
    return twins


@lru_cache(maxsize=2)
def source_notes(workspace, profile_bytes):
    cfg = read_json(profile_bytes)
    manifest = read_json(asset_text(ROOT, cfg["native_ref"]).encode())
    if (
        cfg["repetitions"] != 2
        or cfg["max_usd"] != manifest["fixed_decision_separate_max_usd"]
        or cfg["instruction"] != asset_text(ROOT, cfg["instruction_ref"])
        or {a["target"]: t for t, a in cfg["anchors"].items()}
        != {p["target"]: p["source"] for p in manifest["pairs"]}
    ):
        raise ContractError("Fixed decisions differ from the native study or common solver instruction")
    banks = inventory(workspace, cfg)
    notes = {}
    for source, anchor in cfg["anchors"].items():
        basis = banks[arm_id(source, "fact", 1)]
        report = read_json(asset_text(workspace, anchor["first_public_report"]).encode())
        variants = public_twins(anchor, basis, report)
        for form in ("empty", *FORMS):
            for writer in (1, 2):
                arm = arm_id(source, form, writer)
                bank = banks.get(arm)
                notes[arm] = dict(
                    kind="authentic" if bank else "empty_control",
                    items=bank["items"] if bank else [],
                    writer_run=bank["run_id"] if bank else None,
                    original_snapshot=bank["snapshot_id"] if bank else None,
                )
        for variant, item in variants.items():
            notes[arm_id(source, variant, 0)] = dict(
                kind="analyst_edit",
                items=[item],
                writer_run=basis["run_id"],
                original_snapshot=basis["snapshot_id"],
                source_public_report=anchor["first_public_report"]["sha256"],
            )
    return notes


def stage(journal, workspace, cfg, rows):
    cases = {r["case"] for r in rows}
    if len(cases) != 1 or len({r["form"] for r in rows} & {"bound", "unbound"}) != 1:
        raise ContractError("Stage each controlled twin in its own immutable journal")
    (case,) = cases
    rule = cfg["cases"][case]
    anchor = cfg["anchors"][rule["source"]]
    reader = ReplayContextReader()
    first, profile = captured(journal, workspace, anchor, [anchor["opening"], anchor["profile"]])
    opening = journal.blobs.put(dict(format=FORMAT, recorded_context_ref=first, customer_text=rule["text"]))
    checkpoint = journal.blobs.put(
        dict(format=PROFILE_READ, opening_ref=opening, profile_context_ref=profile, user_id=anchor["customer"])
    )
    originals = source_notes(workspace, canonical(cfg))
    memory = MemoryService(journal, record_kind="observed")
    snapshots, evidence, cells, wires = {}, {}, {}, {}
    for row in rows:
        arm = row["arm"]
        note = originals[arm]
        items = note["items"]
        heads = {i["id"]: i["version"] for i in items}
        snapshots[arm] = install_snapshot(
            memory,
            POLICY,
            heads,
            list(heads),
            len(items),
            {i["version"]: journal.blobs.put(i) for i in items},
            owner_run=arm,
        )
        plan = snapshot_replay_input(
            journal.blobs,
            journal.state,
            checkpoint,
            snapshots[arm],
            policy=POLICY,
            instruction=cfg["instruction"],
            max_input_bytes=cfg["max_input_bytes"],
            context_reader=reader,
        )
        wire = plan["request"]
        messages = wire["messages"]
        profile_data = read_json(messages[-1]["content"].encode())
        if (
            wire["model"] != cfg["model"]
            or wire["seed"] != 4
            or not plan["fits_input_limit"]
            or [m["role"] for m in messages] != ["system", "user", "assistant", "user", "assistant", "tool"]
            or messages[1] != snapshot_message(items)
            or messages[3]["content"] != rule["text"]
            or profile_data["reservations"] != anchor["candidates"]
        ):
            raise ContractError("Unexpected public checkpoint, treatment, model or size")
        common = dict(wire, messages=[messages[0], *messages[2:]])
        cells[arm] = dict(
            wire_sha256=digest(canonical(wire)),
            common_wire_sha256=digest(canonical(common)),
            expected_memory_spans=2 * len(items),
            input_bytes=plan["input_bytes"],
        )
        wires[case, arm] = wire
        evidence[arm] = dict(**note, items_sha256=digest(canonical(items)))
    return dict(
        reader=reader,
        decisions={case: checkpoint},
        snapshots=snapshots,
        cells={case: cells},
        wires=wires,
        evidence=evidence,
    )


def classify(case, rule, message):
    text, calls = public_action(message)
    ids = [
        c["arguments"]["reservation_id"]
        for c in calls
        if c["name"] == "get_reservation_details"
        and isinstance(c["arguments"], dict)
        and set(c["arguments"]) == {"reservation_id"}
        and isinstance(c["arguments"]["reservation_id"], str)
    ]
    direct = len(calls) == 1 and ids == [rule["current_id"]]
    return dict(
        text=text,
        calls=calls,
        requested_ids=ids,
        direct=direct,
        target_in_batch=rule["current_id"] in ids,
        writes=[c["name"] for c in calls if c["name"] in MUTATIONS],
        old_in_batch=rule["source"] == "3" and "JMO1MG" in ids,
        text_review=digest(canonical([case, text])) if text and not calls and rule["requires"] == "ask" else None,
    )


STUDY = ReplayStudy(
    PROFILE, "customer_cue", POLICY, stage, schedule, classify, block_field="batch", stage_by_block=True
)
