"""Perturb one observed note at a time while keeping the public decision fixed."""

from copy import deepcopy

from jsonschema import Draft202012Validator

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.memory.context import memory_system_suffix, snapshot_message


def clause_variants(item, target, background, identifier):
    body = item["body"]
    if (
        not target.strip()
        or not background.strip()
        or body.count(target) != 1
        or body.count(background) != 1
        or identifier not in target
        or body.count(identifier) != 1
        or identifier in item["title"]
    ):
        raise ContractError("The registered excerpts or unique identifier differ from the sealed note")
    a, b = body.index(target), body.index(background)
    if (
        max(a, b) < min(a + len(target), b + len(background))
        or not 0.90 <= len(background) / len(target) <= 1.10
    ):
        raise ContractError("Deletion controls must be disjoint and comparable in length")
    # Each variant runs in an isolated journal. These common public handles mask the arm;
    # the actual author's version and each edited payload digest remain in the analyst ledger.
    handle = digest(canonical(["masked-clause/v1", item["id"], item["version"]]))
    masked = dict(id="m_" + handle[:32], version="v_" + handle[32:], title=item["title"], body=body)
    variants = {"original": masked}
    edits = {}
    for arm, start, excerpt in (("omit_target", a, target), ("omit_background", b, background)):
        end = start + len(excerpt)
        # Also consume a following space, preserving paragraph breaks and every other byte.
        end += body[end : end + 1] == " "
        variants[arm] = dict(masked, body=body[:start] + body[end:])
        edits[arm] = dict(start=start, end=end, removed=body[start:end])
    return variants, edits


def preload_at_decision(base, policy, instruction, item):
    if (
        not policy.preloads
        or not base.get("messages")
        or base["messages"][0]["role"] != "system"
        or any(t["function"]["name"].startswith("memory_") for t in base.get("tools", ()))
    ):
        raise ContractError("Expected an original memory-free public decision and read-only preload")
    wire = deepcopy(base)
    wire["messages"][0]["content"] += memory_system_suffix(policy, instruction)
    wire["messages"].insert(1, snapshot_message([item]))
    return wire


def next_action(choice, native_tools, name, arguments):
    """Classify the proposed public action; a printed JSON object is still a text reply."""
    calls = choice["message"].get("tool_calls") or []
    if not calls:
        return "text"
    if len(calls) != 1:
        return "multiple_calls"
    function = calls[0]["function"]
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in native_tools}
    try:
        args = read_json(function["arguments"].encode())
    except (TypeError, ValueError, AttributeError):
        return "invalid_arguments"
    if function["name"] not in schemas or not Draft202012Validator(schemas[function["name"]]).is_valid(args):
        return "invalid_arguments"
    if function["name"] != name:
        return "search" if function["name"] == "grep" else "other_tool"
    if not isinstance(args, dict) or any(args.get(k) != value for k, value in arguments.items()):
        return "other_arguments"
    return "target_action"
