"""Resolve explicitly authored customer turns against immutable public worker requests."""

from copy import deepcopy
from functools import lru_cache
import re

from jsonschema import Draft202012Validator

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT, SCHEMA_ROOT


FORMAT = "agent-memory-first-customer-query/v1"
CHECKPOINT = "agent-memory-checkpoint-customer-query/v1"
PROFILE_READ = "agent-memory-public-profile-read/v1"


@lru_cache(maxsize=3)
def _validator(filename="first-customer-query.schema.json"):
    schema = read_json((SCHEMA_ROOT / filename).read_bytes())
    return Draft202012Validator(schema)


def replay_decision(blobs, ref, read_parent):
    descriptor = blobs.get(ref)
    kind = descriptor.get("format")
    if kind == PROFILE_READ:
        if not _validator("public-profile-read.schema.json").is_valid(descriptor):
            raise ContractError("Invalid public profile continuation")
        opening_ref = descriptor["opening_ref"]
        if blobs.get(opening_ref).get("format") != FORMAT:
            raise ContractError("A public profile continuation requires an authored opening")
        opening = replay_decision(blobs, opening_ref, read_parent)
        profile = read_parent(blobs, descriptor["profile_context_ref"])
        if opening["archive"]["source_run_id"] != profile["archive"]["source_run_id"]:
            raise ContractError("Opening and public profile come from different native sessions")
        assistant, returned = profile["wire"]["messages"][-2:]
        calls = assistant.get("tool_calls") or []
        if (assistant["role"] != "assistant" or assistant.get("content") or len(calls) != 1
                or set(assistant) - {"role","content","tool_calls"} or set(returned) != {"role","content","tool_call_id"}
                or returned["role"] != "tool" or returned["tool_call_id"] != calls[0]["id"]
                or calls[0]["function"]["name"] != "get_user_details"):
            raise ContractError("Public continuation must be one standalone read and its matching result")
        args = read_json(calls[0]["function"]["arguments"].encode())
        result = read_json(returned["content"].encode())
        user_id = descriptor["user_id"]
        customer_text = opening["wire"]["messages"][-1]["content"]
        if (args != {"user_id": user_id} or not isinstance(result, dict) or result.get("user_id") != user_id
                or not re.search(r"(?<!\w)" + re.escape(user_id) + r"(?!\w)", customer_text)
                or not isinstance(result.get("reservations"), list)):
            raise ContractError("The current customer, read arguments and returned profile must agree")
        wire = deepcopy(opening["wire"])
        wire["messages"].extend(deepcopy([assistant, returned]))
        return dict(wire=wire, archive=opening["archive"], decision_origin=PROFILE_READ)
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
