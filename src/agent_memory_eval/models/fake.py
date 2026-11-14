"""A scripted worker exercising the recording path; it measures no LLM ability."""

from copy import deepcopy

from agent_memory_eval.contracts.core import canonical, new_id, read_json


def tool_call(name, arguments):
    return dict(id=new_id("call"), type="function",
                function=dict(name=name, arguments=canonical(arguments).decode()))


class FakeWorker:
    offline = True

    def __init__(self):
        self.requests = []

    def respond(self, request):
        self.requests.append(deepcopy(request))
        messages = request["messages"]
        task = read_json(next(m["content"] for m in messages if m["role"] == "user").encode())
        names = {tool["function"]["name"] for tool in request["tools"]}
        calls = [(i, c) for i, m in enumerate(messages) for c in m.get("tool_calls", [])]
        seen = {c["function"]["name"] for _, c in calls}
        results = {m["tool_call_id"]: read_json(m["content"].encode())
                   for m in messages if m["role"] == "tool"}
        title = task["kind"] + " 절차"
        writer = messages[0]["content"] == "synthetic_writer"
        finish_index = max((i for i, c in calls if c["function"]["name"] == "finish"), default=-1)
        written_after = any(i > finish_index and c["function"]["name"] == "memory_store" for i, c in calls)

        def store():
            arguments = dict(title=title, body="inspect로 현재 요청을 확인한 뒤 apply한다.\n현재 승인과 조건을 다시 확인한다.")
            if writer:
                for _, call in reversed(calls):
                    if call["function"]["name"] == "memory_store":
                        prior = results.get(call["id"], {})
                        if prior.get("status") == "ok":
                            arguments.update(id=prior["id"], expected_version=prior["version"])
                            break
            return tool_call("memory_store", arguments)

        if writer:
            call = store() if "memory_store" in names and not written_after else None
        elif "memory_list" in names and "memory_list" not in seen:
            call = tool_call("memory_list", {})
        elif "memory_get" in names and "memory_get" not in seen and any(
                item["title"] == title for value in results.values() for item in value.get("items", [])):
            item = next(item for value in results.values() for item in value.get("items", []) if item["title"] == title)
            call = tool_call("memory_get", dict(id=item["id"]))
        elif "inspect" not in seen:
            call = tool_call("inspect", {})
        elif "memory_store" in names and "memory_store" not in seen:
            call = store()
        elif "apply" not in seen:
            observation = next(value for value in results.values() if "target" in value)
            call = tool_call("apply", dict(value=observation["target"]))
        elif "finish" not in seen:
            call = tool_call("finish", {})
        else:
            call = None
        message = dict(role="assistant", tool_calls=[call]) if call else dict(role="assistant", content="합성 작성 종료")
        return dict(choices=[dict(message=message)])
