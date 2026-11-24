"""Literal evidence for action arguments in observed public and memory inputs."""

import json
import re


def input_evidence(messages, tools, document_tools):
    """Classify the *current worker request*, including ordinary tools and earlier worker speech."""
    sources = [(m["role"], m["content"]) for m in messages[:1] if m["role"] == "system"]
    calls = {}
    for message in messages[1:]:
        role = message["role"]
        for call in message.get("tool_calls") or []:
            function = call["function"]
            calls[call["id"]] = function["name"]
            sources.append(("prior_worker_call", str(function["arguments"])))
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if role == "tool":
            origin = "current_document" if calls.get(message.get("tool_call_id")) in document_tools else "current_tool"
        else:
            origin = {"user": "current_customer", "assistant": "prior_worker"}.get(role, role)
        sources.append((origin, content))
    sources.append(("tool_schema", json.dumps(tools, ensure_ascii=False, sort_keys=True)))
    return sources


def argument_literals(value, path=""):
    """Ordered string leaves with JSON pointers. Numbers and short strings provide weak evidence."""
    if isinstance(value, dict):
        for key, part in value.items():
            token = key.replace("~", "~0").replace("/", "~1")
            yield from argument_literals(part, f"{path}/{token}")
    elif isinstance(value, list):
        for index, part in enumerate(value):
            yield from argument_literals(part, f"{path}/{index}")
    elif isinstance(value, str) and len(value.strip()) >= 4:
        yield path, value


def literal_matches(value, sources):
    """Case-sensitive verbatim matches; an empty list means no literal match in these fields."""
    return sorted({name for name, content in sources if value in content})


def argument_features(arguments):
    """Also retain identifier tokens embedded in search patterns or summaries."""
    for path, value in argument_literals(arguments):
        yield path, "whole_argument", value
        identifiers = re.findall(r"(?<!\w)[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+(?!\w)", value)
        for token in dict.fromkeys(identifiers):
            if token != value:
                yield path, "embedded_identifier", token
