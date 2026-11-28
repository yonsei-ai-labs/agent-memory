"""Join verified worker choices to the immutable writer items assigned to a reader."""

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import validate_public_return


def item_exposure(bank_items, choices, events, decision_request, *, contents, blobs):
    """Separate tool returns, submitted inputs and answered inputs through the business decision."""
    if [(item["item_id"], item["version_id"]) for item in bank_items] != [
        (item["id"], item["version"]) for item in contents
    ]:
        raise ContractError("Reader bank order or item versions differ from the sealed writers")
    values = {item["id"]: item for item in contents}
    entries = {}
    for item in bank_items:
        item_id = item["item_id"]
        if item_id in entries:
            raise ContractError("A cohort bank contains a duplicate item id")
        entries[item_id] = dict(
            item, returned_by=set(), returned_fields=set(), input_fields=set(),
            before_decision_fields=set() if decision_request is not None else None,
            get_requested=False, first_get_rank=None,
        )

    def entry_for(field):
        entry = entries.get(field["item_id"])
        if entry is None or entry["version_id"] != field["version_id"]:
            raise ContractError("A reader received an item outside its registered bank")
        return entry

    for event in events:
        if event["type"] != "memory.returned":
            continue
        returned = event["payload"]
        wire = blobs.get(returned["result_ref"])
        validate_public_return(wire, returned)
        result = read_json(wire["content"].encode())
        public = result.get("items", [result["item"]] if "item" in result else [])
        for item in public:
            expected = values.get(item["id"])
            if expected is None or any(key not in expected or expected[key] != value for key, value in item.items()):
                raise ContractError("A returned field differs from the sealed writer's item")
        for item in returned["items"]:
            entry = entry_for(item)
            entry["returned_fields"].update(item["fields"])
            entry["returned_by"].add(returned["operation"])

    rank = 0
    before = decision_request is not None
    for step in choices:
        for field in step["visible_memory"]:
            if field["via"] == "self_authored":
                continue
            entry = entry_for(field)
            encoded = canonical(values[entry["item_id"]][field["field"]])[1:-1]
            if digest(encoded) != field["rendered_sha256"]:
                raise ContractError("Presented memory text differs from its immutable writer version")
            if step["delivery"] == "response_received":
                entry["input_fields"].add(field["field"])
            if before and step["status"] == "response":
                entry["before_decision_fields"].add(field["field"])
        for read in step["reads"]:
            if read["name"] != "memory_get":
                continue
            # An invalid id or a repeated get still occupies a real choice in the emitted tool-call order.
            rank += 1
            entry = entries.get(read["requested_item_id"])
            if entry is not None and not entry["get_requested"]:
                entry.update(get_requested=True, first_get_rank=rank)
        if step["request_id"] == decision_request:
            if step["status"] != "response":
                raise ContractError("The measured business decision has no worker response")
            before = False
    if before:
        raise ContractError("The measured business decision is outside the reader session")
    for entry in entries.values():
        for key in ("returned_by", "returned_fields", "input_fields", "before_decision_fields"):
            if entry[key] is not None:
                entry[key] = sorted(entry[key])
    return list(entries.values())
