"""Trace the retail transfer writers' public inputs, storage calls and delivered notes offline."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.memory.context import snapshot_message
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, safe_path
from scripts.analyze_full_retail import require
from scripts.analyze_retail_full_factors import verified
from scripts.run_full_retail import recorded_source, sealed_rows

PREFIX = "artifacts/analyses/20261121-retail-related-transfer-"
FROZEN = {
    "preflight": "d3e02d03621ae092d521791a7311b262c8b5c5cc4f7994ade5bc06efc4944aa6",
    "results": "faf315af21ff984d5760fd709bce9519a42c9fd9d23dd377e9dd83d9cd260c84",
}
SOURCE_PROTOCOL_SHA256 = (
    "1be3ddf693e85ccbfb0980b24b7b4acbe8f3354a9accda8d3f72c28e64d5363a"
)
RETAIL_RULE = "locating their user id via email, or via name + zip code"
MEMORY_RULE = (
    "Obtain the user ID from the current customer before looking up their information"
)
IDENTITY_TOOLS = {"find_user_id_by_email", "find_user_id_by_name_zip"}

# Retrospective reading of all 13 empty writers. The anchors point to what each
# reply actually says. A case summary can coexist with an explicit choice not to store.
CASE_TEXT = {
    ("5", "episode_original"): "No exchange was submitted",
    ("18", "episode_original"): "office chair that arrived with broken pieces",
    ("26", "episode_original"): "the kettle and keyboard",
    ("38", "episode_original"): "The calculated total was $1,130.85",
    ("45", "episode_original"): "gift card for the exchange price difference",
    (
        "55",
        "procedure_original",
    ): "return requests were submitted before the customer confirmed",
    ("60", "episode_original"): "blue option with the same listed battery-life",
    ("61", "episode_original"): "available blue 8-hour/IPX4 option",
    ("86", "episode_original"): "red, half-zip Fleece Jacket",
}
NO_CASE_TEXT = {
    ("5", "fact_original"): "No memory stored",
    ("18", "fact_original"): "no observations worth retaining",
    ("26", "fact_original"): "No memory was stored",
    ("45", "fact_original"): "no observations to retain",
}


def role_calls(workspace, record, *roles):
    """Read this run's archived requests, outputs and their hashes in journal order."""
    path = safe_path(workspace, record["journal"])
    blobs = BlobStore(path.parent)
    pending = {}
    for line in verified(path, record["journal_sha256"]).splitlines():
        for event in read_json(line)["events"]:
            kind, p = event["type"], event["payload"]
            if kind not in ("model.request_started", "model.request_finished"):
                continue
            require(
                event["run_id"] == record["run"], "Request belongs to another session"
            )
            if kind == "model.request_started" and p["actor_role"] in roles:
                require(not pending, "Overlapping calls in a single conversation")
                pending[p["request_id"]] = p
            elif kind == "model.request_finished" and p["request_id"] in pending:
                start = pending.pop(p["request_id"])
                require(
                    p["delivery_status"] != "not_submitted",
                    "Archived role request was never sent",
                )
                choice, checksum = None, None
                if p["status"] == "response":
                    ref = p["response_ref"]
                    choice, checksum = blobs.get(ref)["choices"][0], ref["sha256"]
                yield (
                    start["actor_role"],
                    blobs.get(start["request_ref"]),
                    choice,
                    checksum,
                )
    require(not pending, "Missing request completion")


def comparable_turn(message):
    """Normalize the two public encodings of a native tool call."""
    calls = []
    for call in message.get("tool_calls") or []:
        if "function" in call:
            f = call["function"]
            calls.append((call["id"], f["name"], json.loads(f["arguments"])))
        else:
            calls.append((call["id"], call["name"], call["arguments"]))
    return (
        message["role"],
        message.get("content"),
        message.get("tool_call_id", message.get("id")),
        calls,
    )


def source_view(workspace, task, donor):
    folder = safe_path(workspace, donor["source_folder"])
    source = recorded_source(folder, task)
    require(
        source
        and source.source_log_sha256 == donor["journal_sha256"]
        and source.source_run_id == donor["run_id"]
        and source.public_context_sha256 == donor["public_context_sha256"],
        "Source binding changed",
    )
    _, _, (row,) = sealed_rows(folder)
    blobs = BlobStore(safe_path(folder, row["directory"]))
    native = blobs.get(row["result"]["before_grade_ref"])["messages"]
    observed = json.loads(source.public_context)["messages"]
    require(
        [comparable_turn(m) for m in observed]
        == [comparable_turn(m) for m in native[: len(observed)]],
        "Writer projection skips or changes a native turn",
    )
    tail = native[len(observed) :]
    require(
        len(tail) == 1 and tail[0]["role"] == "user",
        "Unexpected end of the source conversation",
    )
    return source, {
        "task": task,
        "source_run": source.source_run_id,
        "official": source.official_success,
        "public_context_sha256": source.public_context_sha256,
        "turns_seen": len(observed),
        "customer_closing_turn_outside_writer_view": tail[0]["content"],
    }


def inspect_writer(workspace, source, arm, descriptor, instruction):
    record = dict(descriptor, run=descriptor["run_id"])
    folder = safe_path(workspace, record["journal"]).parent
    bank = read_form_bank(folder, expected_log_sha256=record["journal_sha256"])
    items = bank["items"]
    require(
        bank["task_id"] == source.task_id
        and bank["arm"] == arm
        and bank["run_id"] == record["run"]
        and bank["snapshot_id"] == record["snapshot_id"]
        and bank["public_context_sha256"] == source.public_context_sha256
        and bank["status"] == record["status"]
        and len(items) == record["item_count"]
        and digest(canonical(items)) == record["items_sha256"],
        "Frozen writer snapshot differs",
    )
    feedback = canonical({"kind": "evaluation_feedback", "level": "none"}).decode()
    inputs = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": source.public_context},
        {"role": "user", "content": feedback},
    ]
    calls = list(role_calls(workspace, record, "writer"))
    stores = []
    for i, (_, wire, choice, _) in enumerate(calls):
        require(
            wire["messages"][:3] == inputs
            and (i > 0 or wire["messages"] == inputs)
            and wire["tool_choice"] == "auto"
            and [t["function"]["name"] for t in wire["tools"]] == ["memory_store"]
            and choice
            and choice["finish_reason"] in ("stop", "tool_calls"),
            "Writer input or completion differs",
        )
        for call in choice["message"].get("tool_calls") or []:
            require(
                call["function"]["name"] == "memory_store", "Unexpected writer tool"
            )
            stores.append(json.loads(call["function"]["arguments"]))
    require(
        stores == [{k: item[k] for k in ("title", "body")} for item in items],
        "Writer requested a different store than the sealed snapshot",
    )
    row = {
        "source": source.task_id,
        "arm": arm,
        "run": record["run"],
        "journal": record["journal"],
        "writer_requests": len(calls),
        "store_calls": len(stores),
        "items_sha256": record["items_sha256"],
        "classification": "stored",
    }
    if not items:
        key = (source.task_id, arm)
        label, anchors = (
            ("case_text_without_store", CASE_TEXT)
            if key in CASE_TEXT
            else ("no_case_text", NO_CASE_TEXT)
        )
        _, _, final, response_hash = calls[-1]
        reply = final["message"]["content"]
        require(
            len(calls) == 1 and key in anchors and anchors[key] in reply,
            "Empty reply review does not match",
        )
        row.update(
            classification=label,
            reviewed_excerpt=anchors[key],
            reply_characters=len(reply),
            response_sha256=response_hash,
        )
    return row, items


def replacements(actions):
    changes = {}
    for action in actions:
        args = action["arguments"]
        if "new_item_ids" in args:
            for original, new in zip(
                args["item_ids"], args["new_item_ids"], strict=True
            ):
                key = action["name"], args["order_id"], original
                require(
                    key not in changes, "Ambiguous replacement history on the same item"
                )
                changes[key] = new
    return changes


def review_selections(source, reference, executed, notes):
    """Compare literal SKU retention when the same original item got a different replacement."""
    expected, actual = replacements(reference), replacements(executed)
    differences = []
    for key in sorted(expected.keys() & actual.keys()):
        chosen, wanted = actual[key], expected[key]
        if chosen == wanted:
            continue
        messages = json.loads(source.public_context)["messages"]
        product_calls = {
            call["id"]
            for m in messages
            for call in (m.get("tool_calls") or [])
            if call["function"]["name"] == "get_product_details"
        }
        options = []
        for m in messages:
            if m["role"] == "tool" and m["tool_call_id"] in product_calls:
                product = json.loads(m["content"])
                if isinstance(product, dict):
                    options.append(
                        {
                            k
                            for k, v in product.get("variants", {}).items()
                            if v["available"]
                        }
                    )
        retention = {}
        for form, items in notes.items():
            text = "\n".join(i["title"] + "\n" + i["body"] for i in items)
            retention[form] = {
                "stored": bool(items),
                "chosen_id_present": chosen in text,
                "reference_id_present": wanted in text,
            }
        differences.append(
            {
                "task": source.task_id,
                "operation": key[0],
                "order": key[1],
                "item": key[2],
                "source_chosen": chosen,
                "source_reference": wanted,
                "both_available_in_same_public_lookup": any(
                    {chosen, wanted} <= group for group in options
                ),
                "retention": retention,
            }
        )
    return differences


def inspect_delivery(workspace, cell, items, native_system):
    first_tool, requests, responses = None, 0, 0
    enabled = cell["arm"] != "no_memory"
    expected_snapshot = snapshot_message(items)
    customer_replies = []
    for actor, wire, choice, _ in role_calls(
        workspace, cell, "user_simulator", "worker"
    ):
        if actor == "user_simulator":
            if choice:
                customer_replies.append(choice["message"]["content"])
            continue
        requests += 1
        messages = wire["messages"]
        system = messages[0]["content"]
        require(
            system.partition("\n<memory_policy>")[0] == native_system
            and RETAIL_RULE in system
            and (MEMORY_RULE in system) == enabled,
            "Authentication instructions differ",
        )
        if enabled:
            require(
                messages[1] == expected_snapshot,
                "Worker did not receive the expected snapshot",
            )
        else:
            require(
                system == native_system,
                "Pure control has additional system instructions",
            )
        dialogue = messages[2:] if enabled else messages[1:]
        require(
            [m["content"] for m in dialogue if m["role"] == "user"] == customer_replies,
            "Customer history differs from this session's preceding simulator replies",
        )
        if choice:
            responses += 1
            calls = choice["message"].get("tool_calls") or []
            if first_tool is None and calls:
                first_tool = calls[0]["function"]["name"]
    require(
        responses == cell["worker_responses"]
        and bool(items and responses) == cell["body_exposed"],
        "Worker exposure differs from the verified study",
    )
    return {
        "run": cell["run"],
        "worker_requests": requests,
        "worker_replies": responses,
        "first_tool": first_tool,
        "memory_id_instruction_sent": bool(enabled and requests),
        "body_exposed": cell["body_exposed"],
    }


def audit(workspace):
    pre, result = [
        read_json(verified(workspace / (PREFIX + k + ".json"), FROZEN[k]))
        for k in ("preflight", "results")
    ]
    full = read_json(
        verified(
            workspace / "protocols/pilots/tau2-retail-full-forms-v1.json",
            SOURCE_PROTOCOL_SHA256,
        )
    )
    writing = json.loads(asset_text(workspace, full["base_design"]))["writing"]
    common = asset_text(workspace, writing["common_instruction_ref"]).rstrip("\r\n")
    tasks_path = "data/tau2/domains/retail/tasks.json"
    tasks = {
        t["id"]: t
        for t in read_json(
            verified(
                workspace / "data/tau2-bench" / tasks_path, full["assets"][tasks_path]
            )
        )
    }
    sources, writers, banks, selection_review = [], {}, {}, []
    source_policies = set()
    for task, donor in pre["donors"].items():
        source, source_summary = source_view(workspace, task, donor)
        sources.append(source_summary)
        source_policies.add(
            json.loads(source.public_context)["task_context"]["content"]
        )
        for arm, descriptor in donor["banks"].items():
            instruction = (
                common + "\n" + writing["lines"][arm.removesuffix("_original")]
            )
            writers[task, arm], banks[task, arm] = inspect_writer(
                workspace, source, arm, descriptor, instruction
            )
        selection_review.extend(
            review_selections(
                source,
                tasks[task]["evaluation_criteria"]["actions"],
                pre["database_replay"]["observed"][task]["executed"],
                {a: banks[task, a] for a in donor["banks"]},
            )
        )
    require(
        {k for k, v in writers.items() if not v["store_calls"]}
        == CASE_TEXT.keys() | NO_CASE_TEXT.keys(),
        "Review must cover exactly the empty writer replies",
    )
    deliveries, outcomes, auth = [], defaultdict(Counter), defaultdict(Counter)
    pairs = {p["run"]: p for p in result["pairs"]}
    require(len(source_policies) == 1, "Sources used different retail policies")
    native_system = source_policies.pop()
    for cell in result["cells"]:
        key = cell["source"], cell["arm"]
        delivery = inspect_delivery(
            workspace, cell, banks[key] if cell["source"] else [], native_system
        )
        label = writers[key]["classification"] if cell["source"] else cell["arm"]
        deliveries.append(
            dict(
                delivery,
                source=cell["source"],
                task=cell["task"],
                arm=cell["arm"],
                stage=cell["stage"],
            )
        )
        counts = outcomes[label]
        counts["assigned"] += 1
        counts["exposed"] += delivery["body_exposed"]
        counts["graded"] += cell["official"] is not None
        counts["passed"] += cell["official"] is True
        if cell["source"]:
            for control in ("no_memory", "empty_preload"):
                counts[f"{control}_{pairs[cell['run']][control]['state']}"] += 1
        group = auth[cell["arm"] if not cell["source"] else "assigned_writer"]
        group["assigned"] += 1
        group["memory_id_instruction_sent"] += delivery["memory_id_instruction_sent"]
        group["first_action_is_identity_lookup"] += (
            delivery["first_tool"] in IDENTITY_TOOLS
        )
        group["any_business_tool"] += delivery["first_tool"] is not None
    return {
        "study": result["study"],
        "input_sha256": dict(FROZEN, source_protocol=SOURCE_PROTOCOL_SHA256),
        "analysis_model_calls": 0,
        "sources": sources,
        "writers": list(writers.values()),
        "empty_reply_method": "Retrospective public-text review",
        "source_replacement_differences": selection_review,
        "outcomes_by_writer_result": outcomes,
        "authentication": auth,
        "deliveries": deliveries,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output file")
    result = audit(args.workspace.resolve())
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    print(
        f"Verified {len(result['writers'])} writers and {len(result['deliveries'])} transfer assignments"
    )


if __name__ == "__main__":
    main()
