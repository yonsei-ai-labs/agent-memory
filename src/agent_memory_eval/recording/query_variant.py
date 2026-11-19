"""Resolve explicitly authored customer turns against immutable public worker requests."""

from copy import deepcopy
from functools import lru_cache

from jsonschema import Draft202012Validator

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT


FORMAT = "agent-memory-first-customer-query/v1"
CHECKPOINT = "agent-memory-checkpoint-customer-query/v1"


@lru_cache(maxsize=2)
def _validator(filename="first-customer-query.schema.json"):
    schema = read_json((ROOT / "schemas" / filename).read_bytes())
    return Draft202012Validator(schema)


def replay_decision(blobs, ref, read_parent):
    descriptor = blobs.get(ref)
    kind = descriptor.get("format")
    if kind not in {FORMAT, CHECKPOINT}:
        return read_parent(blobs, ref)
    filename = "first-customer-query.schema.json" if kind == FORMAT else "checkpoint-customer-query.schema.json"
    if not _validator(filename).is_valid(descriptor) or not descriptor["customer_text"].strip():
        raise ContractError("Invalid authored customer query")
    parent = read_parent(blobs, descriptor["recorded_context_ref"])
    wire = deepcopy(parent["wire"])
    messages = wire["messages"]
    if kind == FORMAT and [m["role"] for m in messages] != ["system", "assistant", "user"]:
        raise ContractError("An authored first turn requires an archived greeting before any customer history")
    if kind == CHECKPOINT and (digest(canonical(messages[:-1])) != descriptor["prefix_sha256"]
            or not any(m["role"] == "tool" for m in messages[1:-2])):
        raise ContractError("The archived checkpoint must include the pinned history and a public tool result")
    if (messages[-2]["role"] != "assistant" or messages[-2].get("tool_calls") or messages[-1]["role"] != "user"
            or set(messages[-1]) != {"role", "content"} or messages[-1]["content"] == descriptor["customer_text"]):
        raise ContractError("Expected a plain, distinct replacement of the last customer utterance")
    messages[-1]["content"] = descriptor["customer_text"]
    return dict(wire=wire, archive=parent["archive"], decision_origin=kind)
