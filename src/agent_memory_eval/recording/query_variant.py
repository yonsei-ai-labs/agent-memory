"""Resolve an explicitly authored first customer turn against its archived public parent."""

from copy import deepcopy
from functools import lru_cache

from jsonschema import Draft202012Validator

from agent_memory_eval.contracts.core import ContractError, read_json
from agent_memory_eval.contracts.validation import ROOT


FORMAT = "agent-memory-first-customer-query/v1"


@lru_cache(maxsize=1)
def _validator():
    schema = read_json((ROOT / "schemas/first-customer-query.schema.json").read_bytes())
    return Draft202012Validator(schema)


def replay_decision(blobs, ref, read_parent):
    descriptor = blobs.get(ref)
    if descriptor.get("format") != FORMAT:
        return read_parent(blobs, ref)
    if not _validator().is_valid(descriptor) or not descriptor["customer_text"].strip():
        raise ContractError("Invalid authored customer query")
    parent = read_parent(blobs, descriptor["recorded_context_ref"])
    wire = deepcopy(parent["wire"])
    if [m["role"] for m in wire["messages"]] != ["system", "assistant", "user"]:
        raise ContractError("An authored first turn requires an archived greeting before any customer history")
    if (wire["messages"][1].get("tool_calls") or set(wire["messages"][2]) != {"role", "content"}
            or wire["messages"][2]["content"] == descriptor["customer_text"]):
        raise ContractError("Expected a plain, distinct replacement of the opening customer utterance")
    wire["messages"][2]["content"] = descriptor["customer_text"]
    return dict(wire=wire, archive=parent["archive"], decision_origin=FORMAT)
