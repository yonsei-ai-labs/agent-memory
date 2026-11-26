"""Run an offline native-memory wiring check on one public BFCL prerequisite and its question."""

import argparse
from pathlib import Path
import re
import subprocess
import sys

from agent_memory_eval.adapters.bfcl_memory import RecSumMemory
from agent_memory_eval.adapters.bfcl_task_start import REVISION
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import MemoryPolicy, RunContext, canonical, digest, read_json
from agent_memory_eval.memory.router import ToolRouter
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.models.fake import tool_call
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.recording.provenance import OfflineGateway, SourceRegistry


class _Scripted:
    offline = True

    def __init__(self, decision):
        self.decision = decision

    def respond(self, request):
        return {"choices": [{"message": {"role": "assistant", **self.decision(request["messages"][-1])}}]}


class _NoBusinessTools:
    stop_tools = frozenset()
    ended = False


def _open(journal, ctx, question, tools, bank=None, snapshot=None, sources=()):
    start = ctx.at("prepare").event("run.started", dict(bank_id=bank, start_snapshot_id=snapshot, fresh_session=True,
        source_run_ids=list(sources), task_ref=journal.blobs.put(question)))
    journal.append([start])
    registry = SourceRegistry(journal, ctx)
    template = registry.template(dict(model="synthetic-offline", tools=tools,
                                      system=dict(role="system", content="Answer the public question.")))
    return registry, template, [(template, "/system"), (registry.add("task_initial", start["event_id"]), "")]


def _close(journal, ctx, grade=None, snapshot=None):
    status = "graded" if grade is not None else "not_available"
    journal.append([ctx.at("grade").event("outcome.scored", dict(grading_status=status,
        official_success=grade["valid"] if grade is not None else None, official_score=None,
        official_artifact_ref=journal.blobs.put(grade) if grade is not None else None))])
    journal.append([ctx.at("seal").event("run.ended", dict(termination_reason="offline_control",
        grading_status=status, end_snapshot_id=snapshot))])


def _fact(message):
    match = re.search(r"\bMy name is [A-Za-z]+\b", message["content"])
    require(match is not None, "The selected public prerequisite does not contain the fixture fact")
    return match.group()


def _submitted(journal, request_id):
    return next(e["payload"] for e in journal.events.values() if e["type"] == "model.request_started"
                and e["payload"]["request_id"] == request_id)


def _trial(folder, backend_class, checker, prerequisite, question, definitions, answers):
    folder.mkdir(parents=True, exist_ok=False)
    gateway_dir = folder / "journal"
    native_root = folder / "native"
    config = dict(model_result_dir=native_root, scenario="customer")
    spans, grades, first_inputs = {}, {}, []
    with Journal(gateway_dir, "bfcl-native-control", contract_version="1.3") as journal:
        memory, gateway = MemoryService(journal), OfflineGateway(journal)
        source = RunContext("bfcl-native-control", "source", "session-source", "no_memory", "collection", "solve")
        registry, template, history = _open(journal, source, prerequisite, [])
        gateway.call(source, registry, "worker", template, history, _Scripted(lambda _: {"content": "Received."}), max_requests=1)
        _close(journal, source)

        writer = RunContext(source.experiment_id, "writer", "session-writer", "native_memory", "collection", "post_task_write")
        writable = MemoryPolicy(True, "post_task", False, 1)
        bank = memory.create_bank(writer.run_id, writable)
        backend = backend_class()
        backend._load_scenario(dict(config, test_id="memory_rec_sum_prereq_0-customer-0"))
        registry, template, history = _open(journal, writer, prerequisite, [definitions["memory_append"]], bank, sources=[source.run_id])
        journal.append([writer.event("business.transition", dict(action="observe_public_source", arguments={},
            public_message_ref=journal.blobs.put(prerequisite), business_step_index=0))])
        router = ToolRouter(journal, memory, _NoBusinessTools(), bank, writable, native_memory=RecSumMemory(backend))
        def append(last):
            return {"tool_calls": [tool_call("memory_append", dict(text=_fact(last)))]}
        msg, _, req = gateway.call(writer, registry, "writer", template, history, _Scripted(append), max_requests=1)
        (ack, _, _), = router.dispatch(writer, req, msg["tool_calls"])
        require(read_json(ack["content"].encode()) == {"status": "Memory appended."}, "BFCL store failed")
        snapshot = memory.freeze(bank)
        memory.seal(bank)
        backend._flush_memory_to_local_file()
        _close(journal, writer, snapshot=snapshot)
        stored, = [e["payload"] for e in journal.events.values() if e["type"] == "memory.stored"]

        for enabled in (True, False):
            label = "read_enabled" if enabled else "access_control"
            ctx = RunContext(source.experiment_id, label, "session-" + label, label, "probe", "solve")
            policy = MemoryPolicy(enabled, "none", False, 1, read_mode="on_demand" if enabled else "none")
            fork = memory.fork(snapshot, ctx.run_id, policy) if enabled else None
            restored = backend_class()
            if enabled:
                restored._load_scenario(dict(config, test_id="memory_rec_sum_0-customer-0"))
                require(digest(restored.memory.encode()) == stored["body_sha256"], "Wrong native checkpoint")
            router = ToolRouter(journal, memory, _NoBusinessTools(), fork, policy, native_memory=RecSumMemory(restored))
            registry, template, history = _open(journal, ctx, question, [definitions["memory_retrieve"]], fork,
                snapshot if enabled else None, [writer.run_id] if enabled else [])
            read = _Scripted(lambda _: {"tool_calls": [tool_call("memory_retrieve", {})]})
            msg, own, req = gateway.call(ctx, registry, "worker", template, history, read, max_requests=2)
            start = _submitted(journal, req)
            first_inputs.append(start["request_ref"]["sha256"])
            history.append((own, ""))
            (wire, kind, event), = router.dispatch(ctx, req, msg["tool_calls"])
            returned = read_json(wire["content"].encode())
            if enabled:
                require(returned == {"memory_content": restored.memory} and kind == "memory_return", "Native retrieval failed")
            else:
                require(kind == "policy_result" and returned["error"]["code"] == "POLICY_DENIED", "Disabled native access")
            history.append((registry.add(kind, event), ""))
            def answer(last):
                value = read_json(last["content"].encode())
                return {"content": value["memory_content"].split()[-1] if "memory_content" in value else "Unknown."}
            response, _, decision = gateway.call(ctx, registry, "worker", template, history, _Scripted(answer), max_requests=2)
            submitted = _submitted(journal, decision)
            actual_wire = journal.blobs.get(submitted["request_ref"])
            include = journal.blobs.get(submitted["input_manifest_ref"])["memory_spans"]
            require(actual_wire["messages"][-1] == wire and all(
                s["pointer"] == f"/messages/{len(actual_wire['messages']) - 1}/content"
                and s["version_id"] == stored["version_id"] and s["item_id"] == stored["item_id"]
                and s["field"] == "body" for s in include), "Wrong retrieval provenance")
            spans[label] = len(include)
            require(spans[label] == int(enabled), "Unexpected native exposure count")
            grade = checker(response["content"], answers)
            grades[label] = grade["valid"]
            _close(journal, ctx, grade)
    require(first_inputs[0] == first_inputs[1] and grades == {"read_enabled": True, "access_control": False},
            "Matched public input or native official control failed")
    return dict(format="bfcl-native-memory-control/v1", benchmark_revision=REVISION,
                task="memory_0-customer-0", prerequisite="memory_prereq_0-customer-0", scripted=True,
                equal_first_reader_wire=True, source_fact_sha256=stored["body_sha256"],
                native_snapshot_restored=True, version_bound_input_spans=spans, official_success=grades,
                request_audit=audit_requests(gateway_dir), new_provider_requests=0)


def check(checkout, folder):
    checkout = checkout.resolve()
    require(subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip() == REVISION,
            "Wrong BFCL revision")
    package = checkout / "berkeley-function-call-leaderboard"
    base = "berkeley-function-call-leaderboard/bfcl_eval/"
    def pinned(name):
        return subprocess.check_output(["git", "-C", checkout, "show", f"{REVISION}:{base}{name}"])
    def rows(name):
        return [read_json(line) for line in pinned("data/" + name).splitlines() if line]
    def entry(name, identity):
        row, = [r for r in rows(name) if r["id"] == identity]
        return row
    prerequisite = entry("memory_prereq_conversation/memory_customer.json", "memory_prereq_0-customer-0")["question"][0][0]
    question = entry("BFCL_v4_memory.json", "memory_0-customer-0")["question"][0][0]
    definitions = {}
    for tool in rows("multi_turn_func_doc/memory_rec_sum.json"):
        parameters = dict(tool["parameters"], type="object")
        definitions[tool["name"]] = dict(type="function", function=dict(name=tool["name"],
            description=tool["description"], parameters=parameters))
    answers = entry("possible_answer/BFCL_v4_memory.json", "memory_0-customer-0")["ground_truth"]
    sys.path.insert(0, str(package))
    try:
        from bfcl_eval.eval_checker.agentic_eval import agentic_checker
        from bfcl_eval.eval_checker.multi_turn_eval.func_source_code import memory_api_metaclass, memory_rec_sum
        for module in (agentic_checker, memory_rec_sum, memory_api_metaclass):
            path = Path(module.__file__).resolve()
            relative = path.relative_to(package / "bfcl_eval")
            require(path.read_bytes() == pinned(relative.as_posix()), "Modified BFCL implementation")
        return _trial(folder, memory_rec_sum.MemoryAPI_rec_sum, agentic_checker.agentic_checker,
                      prerequisite, question, definitions, answers)
    finally:
        sys.path.remove(str(package))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--recording", required=True, type=Path, help="New directory for full journals and the native checkpoint")
    args = parser.parse_args()
    report = check(args.checkout, args.recording)
    encoded = canonical(report)
    (args.recording / "result.json").write_bytes(encoded + b"\n")
    print(encoded.decode())


if __name__ == "__main__":
    main()
