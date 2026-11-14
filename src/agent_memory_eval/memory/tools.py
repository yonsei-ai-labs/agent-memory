"""Public memory tool schemas, independent of a benchmark or model provider."""

from agent_memory_eval.contracts.core import ContractError


def memory_tools(names):
    properties = {
        "memory_store": dict(title={"type": "string"}, body={"type": "string"},
            id={"type": ["string", "null"]}, expected_version={"type": ["string", "null"]}),
        "memory_list": dict(cursor={"type": ["string", "null"]}, limit={"type": ["integer", "null"]}),
        "memory_get": dict(id={"type": "string"}),
    }
    required = {"memory_store": ["title", "body"], "memory_list": [], "memory_get": ["id"]}
    descriptions = {
        "memory_store": "Create a persistent memory, or update one using its id and expected_version. Preserve conditions and uncertainty.",
        "memory_list": "List memory ids, versions and titles. Use the returned opaque cursor for another page. Bodies require memory_get.",
        "memory_get": "Read one memory's title and body by id. Check whether its conditions apply to the current task.",
    }
    if set(names) - properties.keys():
        raise ContractError("Unknown memory tool")
    return [dict(type="function", function=dict(name=name, description=descriptions[name],
        parameters=dict(type="object", properties=properties[name], required=required[name], additionalProperties=False)))
        for name in names]
