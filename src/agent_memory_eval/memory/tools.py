"""Public memory tool schemas, independent of a benchmark or model provider."""

from agent_memory_eval.contracts.core import ContractError


def memory_tools(names, *, page_size=None, read_descriptions=None):
    properties = {
        "memory_store": dict(title={"type": "string"}, body={"type": "string"},
            id={"type": ["string", "null"]}, expected_version={"type": ["string", "null"]}),
        "memory_list": dict(cursor={"type": ["string", "null"]}, limit={"type": ["integer", "null"]}),
        "memory_get": dict(id={"type": "string"}),
    }
    if page_size is not None:
        if type(page_size) is not int or page_size < 1:
            raise ContractError("The catalog page size must be a positive integer")
        properties["memory_list"]["limit"].update(minimum=1, maximum=page_size,
            description=f"Omit to use the default of {page_size}. The maximum is {page_size}.")
    required = {"memory_store": ["title", "body"], "memory_list": [], "memory_get": ["id"]}
    descriptions = {
        "memory_store": "Create a persistent memory, or update one using its id and expected_version. Preserve conditions and uncertainty.",
        "memory_list": "List memory ids, versions and titles. Use the returned opaque cursor for another page. Bodies require memory_get.",
        "memory_get": "Read one memory's title and body by id. Check whether its conditions apply to the current task.",
    }
    if read_descriptions is not None:
        if (not isinstance(read_descriptions, dict) or read_descriptions.keys() - {"memory_list", "memory_get"}
                or any(not isinstance(text, str) or not text.strip() for text in read_descriptions.values())):
            raise ContractError("Read tool descriptions must name a retrieval tool and contain nonempty text")
        descriptions.update(read_descriptions)
    if set(names) - properties.keys():
        raise ContractError("Unknown memory tool")
    return [dict(type="function", function=dict(name=name, description=descriptions[name],
        parameters=dict(type="object", properties=properties[name], required=required[name], additionalProperties=False)))
        for name in names]
