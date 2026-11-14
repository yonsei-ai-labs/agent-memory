"""Build tiny illustrative contract data, never an executable benchmark plan."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from contract_v11 import canonical, digest, record_hash  # noqa: E402


def write_journal(path, records):
    previous = None
    seq = 0
    for commit, row in enumerate(records, 1):
        row.update(commit_seq=commit, prev_sha256=previous)
        for event in row["events"]:
            seq += 1
            event["event_seq"] = seq
        row["record_sha256"] = record_hash(row)
        previous = row["record_sha256"]
    path.write_bytes(b"".join(canonical(row) + b"\n" for row in records))


def write_fixture(base):
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)

    def put(name, value):
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"path": name, "sha256": digest(path.read_bytes()), "media_type": "application/json"}

    title = "배송지 확인"
    body = '새 배송지를 다시 확인한다.\n승인받은 "주소"만 사용한다.'
    memory = {"id": "m_7fd1", "version": "v_4c2e", "title": title, "body": body}
    task_c = put("examples/task-collection.json", {"role": "user", "content": "합성 배송 업무를 처리해 주세요."})
    task_p = put("examples/task-probe.json", {"role": "user", "content": "현재 요청의 배송지를 처리해 주세요."})
    public_c = {"role": "user", "content": "새 배송지는 사용자의 승인을 받은 뒤 적용합니다."}
    public_p = {"role": "user", "content": "현재 요청에 새 배송지가 있습니다."}
    public_c_ref = put("examples/public-collection.json", public_c)
    public_p_ref = put("examples/public-probe.json", public_p)
    template = {
        "model": "synthetic-offline",
        "writer_system": {"role": "system", "content": "합성 계약 예시. 공개된 경험에서 기억을 작성한다."},
        "worker_system": {"role": "system", "content": "합성 계약 예시. 현재 업무와 허용된 도구를 사용한다."},
        "tools": [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
                  for name in ("memory_store", "memory_list", "memory_get")],
    }
    template_ref = put("examples/templates.json", template)
    store_message = {"role": "assistant", "tool_calls": [{"id": "call_store", "type": "function",
                     "function": {"name": "memory_store", "arguments": canonical({"title": title, "body": body}).decode()}}]}
    get_message = {"role": "assistant", "tool_calls": [{"id": "call_get", "type": "function",
                   "function": {"name": "memory_get", "arguments": canonical({"id": memory["id"]}).decode()}}]}
    list_message = {"role": "assistant", "tool_calls": [{"id": "call_list", "type": "function",
                    "function": {"name": "memory_list", "arguments": "{}"}}]}
    store_response = put("examples/response-store.json", {"choices": [{"message": store_message}]})
    get_response = put("examples/response-get.json", {"choices": [{"message": get_message}]})
    list_response = put("examples/response-list.json", {"choices": [{"message": list_message}]})
    final_response = put("examples/response-final.json", {"choices": [{"message": {"role": "assistant", "content": "합성 실행 종료"}}]})
    ack = {"role": "tool", "tool_call_id": "call_store", "content": canonical({"status": "ok", "id": memory["id"], "version": memory["version"]}).decode()}
    get = {"role": "tool", "tool_call_id": "call_get", "content": canonical({"status": "ok", "item": memory}).decode()}
    listing = {"role": "tool", "tool_call_id": "call_list", "content": canonical({"status": "ok", "items": [
        {key: memory[key] for key in ("id", "version", "title")}], "next_cursor": None}).decode()}
    ack_ref = put("examples/store-result.json", ack)
    get_ref = put("examples/get-result.json", get)
    list_ref = put("examples/list-result.json", listing)
    memory_ref = put("examples/memory.json", memory)
    prefix_line = canonical({"seq": 1, "run_id": "r_collect", "event_id": "e_public_c", "message": public_c}) + b"\n"
    (base / "experience.jsonl").write_bytes(prefix_line)
    prefix = {"path": "experience.jsonl", "run_id": "r_collect", "entry_count": 1,
              "through_event_seq": 2, "prefix_sha256": digest(prefix_line)}

    def spans(text, location, ids, fields=("title", "body")):
        result = []
        for field in fields:
            encoded = json.dumps(memory[field], ensure_ascii=False)[1:-1]
            start = text.index(encoded)
            result.append({"pointer": location, "char_start": start, "char_end": start + len(encoded),
                           "item_id": ids[0], "version_id": ids[1], "field": field})
        return result

    def source(kind, run, roles, ref=None, producer=None, location="", memory_spans=None):
        result = {"kind": kind, "run_id": run, "roles": roles, "pointer": location,
                  "memory_spans": memory_spans or []}
        if ref is not None:
            result["artifact_ref"] = ref
        if producer is not None:
            result["producer_event_id"] = producer
        return result

    catalog = {
        "template": source("template", None, ["worker", "writer"], template_ref),
        "task_c": source("task_initial", "r_collect", ["writer"], task_c, "e_start_c"),
        "task_p": source("task_initial", "r_probe", ["worker"], task_p, "e_start_p"),
        "public_c": {**source("public_experience", "r_collect", ["writer"]), "prefix": prefix},
        "public_p": source("business_result", "r_probe", ["worker"], public_p_ref, "e_public_p"),
        "authored_store": source("self_authored", "r_collect", ["writer"], store_response, "e_finish_q1", "/choices/0/message",
                                  spans(store_message["tool_calls"][0]["function"]["arguments"], "/tool_calls/0/function/arguments", (None, None))),
        "authored_list": source("self_authored", "r_probe", ["worker"], list_response, "e_finish_q3", "/choices/0/message"),
        "authored_get": source("self_authored", "r_probe", ["worker"], get_response, "e_finish_q4", "/choices/0/message"),
        "ack": source("memory_return", "r_collect", ["writer"], ack_ref, "e_ack"),
        "get": source("memory_return", "r_probe", ["worker"], get_ref, "e_get", memory_spans=spans(get["content"], "/content", (memory["id"], memory["version"]))),
        "list": source("memory_return", "r_probe", ["worker"], list_ref, "e_list", memory_spans=spans(listing["content"], "/content", (memory["id"], memory["version"]), ("title",))),
    }
    source_values = {"template": template, "task_c": json.loads((base / task_c["path"]).read_text()),
                     "task_p": json.loads((base / task_p["path"]).read_text()), "public_c": {"messages": [public_c]},
                     "public_p": public_p, "authored_store": store_message, "authored_get": get_message,
                     "authored_list": list_message, "ack": ack, "get": get, "list": listing}

    records = []
    def event(name, kind, payload, run="r_collect", role="post_task_write", stage=None, causes=None):
        row = {"schema_version": "1.1", "event_id": name, "event_seq": 0,
               "experiment_id": "illustrative-v1.1", "run_id": run,
               "session_id": {"r_collect": "s_collect", "r_probe": "s_probe", None: None}[run],
               "condition_id": None if run is None else "illustrative_condition",
               "stage": stage or ("controller" if run is None else "collection" if run == "r_collect" else "probe"),
               "phase": role, "type": kind, "timestamp_utc": "2026-11-14T00:00:00Z",
               "record_kind": "illustrative", "caused_by": causes or [], "payload": payload}
        return row

    def commit(*events, operation=None):
        records.append({"journal_version": "1.0", "commit_seq": 0, "prev_sha256": None,
                        "record_sha256": "0" * 64, "operation": operation, "events": list(events)})

    def outcome(name, run):
        return event(name, "outcome.scored", {"grading_status": "not_available", "official_success": None,
                     "official_score": None, "official_artifact_ref": None}, run, "grade")

    def request(name, run, actor, messages):
        bindings = [{"pointer": "/model", "source_id": "template", "source_pointer": "/model"},
                    {"pointer": "/tools", "source_id": "template", "source_pointer": "/tools"}]
        wire_messages = []
        memory_spans = []
        from contract_v11 import pointer
        for index, (source_id, location) in enumerate(messages):
            wire_messages.append(deepcopy(pointer(source_values[source_id], location)))
            bindings.append({"pointer": f"/messages/{index}", "source_id": source_id, "source_pointer": location})
            for span in catalog[source_id]["memory_spans"]:
                value = deepcopy(span)
                value["pointer"] = f"/messages/{index}" + span["pointer"][len(location):]
                text = pointer(source_values[source_id], span["pointer"])
                value.update(span_id=f"{name}:m{len(memory_spans)}", source_id=source_id,
                             rendered_sha256=digest(text[span["char_start"]:span["char_end"]].encode()))
                memory_spans.append(value)
        wire = {"model": template["model"], "tools": template["tools"], "messages": wire_messages}
        wire_ref = put(f"examples/{name}-request.json", wire)
        manifest = {"schema_version": "1.1", "request_id": name, "run_id": run, "actor_role": actor,
                    "request_ref": wire_ref, "bindings": bindings, "memory_spans": memory_spans}
        manifest_ref = put(f"examples/{name}-manifest.json", manifest)
        commit(event(f"e_start_{name}", "model.request_started", {"request_id": name, "actor_role": actor,
                     "request_ref": wire_ref, "input_manifest_ref": manifest_ref, "delivery_status": "prepared"}, run, "post_task_write" if actor == "writer" else "solve"))
        for span in memory_spans:
            origin = catalog[span["source_id"]]
            origin_key = "returned_event_id" if origin["kind"] == "memory_return" else "response_event_id"
            payload = {"request_id": name, "item_id": span["item_id"], "version_id": span["version_id"],
                       "manifest_span_id": span["span_id"], "source": {"kind": origin["kind"], origin_key: origin["producer_event_id"]},
                       "field": span["field"], "request_ref": wire_ref,
                       **{key: span[key] for key in ("pointer", "char_start", "char_end", "rendered_sha256")}}
            commit(event(f"e_input_{span['span_id']}", "memory.input_included", payload, run,
                         "post_task_write" if actor == "writer" else "solve", causes=[f"e_start_{name}", origin["producer_event_id"]]))

    def finish(name, run, ref, phase):
        commit(event(f"e_finish_{name}", "model.request_finished", {"request_id": name,
                     "status": "response", "delivery_status": "response_received", "response_ref": ref,
                     "usage": None, "cost_usd": None, "cost_status": "unknown"}, run, phase, causes=[f"e_start_{name}"]))

    commit(event("e_start_c", "run.started", {"bank_id": "b_collect", "start_snapshot_id": None,
                 "fresh_session": True, "source_run_ids": [], "task_ref": task_c}, role="prepare"))
    commit(event("e_public_c", "business.transition", {"public_message_ref": public_c_ref}, role="solve"))
    commit(outcome("e_outcome_c", "r_collect"))
    first = [("template", "/writer_system"), ("task_c", ""), ("public_c", "/messages/0")]
    request("q1", "r_collect", "writer", first)
    finish("q1", "r_collect", store_response, "post_task_write")
    stored = event("e_store", "memory.stored", {"bank_id": "b_collect", "head_revision": 1,
                   "operation_id": "op_store", "item_id": memory["id"], "version_id": memory["version"],
                   "parent_version_id": None, "title": title, "body_ref": memory_ref,
                   "body_sha256": digest(body.encode()), "public_context_prefix": prefix}, causes=["e_finish_q1"])
    returned = event("e_ack", "memory.returned", {"bank_id": "b_collect", "operation_id": "op_store",
                     "operation": "create", "status": "ok", "result_ref": ack_ref, "items": []}, causes=["e_store"])
    commit(stored, returned, operation={"operation_id": "op_store", "request_sha256": digest(canonical({"title": title, "body": body})), "result_ref": ack_ref})
    request("q2", "r_collect", "writer", first + [("authored_store", ""), ("ack", "")])
    finish("q2", "r_collect", final_response, "post_task_write")
    snapshot_ref = put("examples/snapshot.json", {"snapshot_id": "snap_1", "heads": {memory["id"]: memory["version"]}})
    commit(event("e_snapshot", "bank.snapshot_created", {"bank_id": "b_collect", "snapshot_id": "snap_1",
                 "head_revision": 1, "parent_snapshot_ids": [], "manifest_ref": snapshot_ref}, None, "seal"))
    commit(event("e_end_c", "run.ended", {"termination_reason": "illustrative_end", "grading_status": "not_available", "end_snapshot_id": "snap_1"}, role="seal"))
    commit(event("e_start_p", "run.started", {"bank_id": "b_probe", "start_snapshot_id": "snap_1", "fresh_session": True,
                 "source_run_ids": ["r_collect"], "task_ref": task_p}, "r_probe", "prepare"))
    commit(event("e_public_p", "business.transition", {"public_message_ref": public_p_ref}, "r_probe", "solve"))
    first_probe = [("template", "/worker_system"), ("task_p", ""), ("public_p", "")]
    request("q3", "r_probe", "worker", first_probe)
    finish("q3", "r_probe", list_response, "solve")
    commit(event("e_list", "memory.returned", {"bank_id": "b_probe", "operation_id": "op_list", "operation": "list",
                 "status": "ok", "result_ref": list_ref, "items": [{"item_id": memory["id"], "version_id": memory["version"], "fields": ["title"]}]},
                 "r_probe", "solve", causes=["e_finish_q3"]), operation={"operation_id": "op_list", "request_sha256": digest(canonical({})), "result_ref": list_ref})
    list_history = [("authored_list", ""), ("list", "")]
    request("q4", "r_probe", "worker", first_probe + list_history)
    finish("q4", "r_probe", get_response, "solve")
    commit(event("e_get", "memory.returned", {"bank_id": "b_probe", "operation_id": "op_get", "operation": "get",
                 "status": "ok", "result_ref": get_ref, "items": [{"item_id": memory["id"], "version_id": memory["version"], "fields": ["title", "body"]}]},
                 "r_probe", "solve", causes=["e_finish_q4"]), operation={"operation_id": "op_get", "request_sha256": digest(canonical({"id": memory["id"]})), "result_ref": get_ref})
    request("q5", "r_probe", "worker", first_probe + list_history + [("authored_get", ""), ("get", "")])
    finish("q5", "r_probe", final_response, "solve")
    commit(outcome("e_outcome_p", "r_probe"))
    commit(event("e_end_p", "run.ended", {"termination_reason": "illustrative_end", "grading_status": "not_available", "end_snapshot_id": None}, "r_probe", "seal"))
    write_journal(base / "log.jsonl", records)
    batch_calls = [{"id": "business_1", "name": "inspect"}, {"id": "memory_1", "name": "memory_store"}, {"id": "business_2", "name": "update"}]
    batches = [{"calls": batch_calls, "configured_stop_tools": [], "disposition": "execute",
                "execution_order": [call["id"] for call in batch_calls], "result_order": [call["id"] for call in batch_calls]},
               {"calls": [{"id": "memory_1", "name": "memory_store"}, {"id": "stop_1", "name": "finish"}],
                "configured_stop_tools": ["finish"], "disposition": "reject_all", "execution_order": [], "result_order": ["memory_1", "stop_1"]}]
    put("fixture.json", {"journal_path": "log.jsonl", "sources_ref": put("sources.json", catalog),
                        "approved_templates": [template_ref], "tool_batches_ref": put("tool-batches.json", batches)})
    return base


if __name__ == "__main__":
    write_fixture(ROOT / "tests" / "fixtures" / "contracts-v1.1")
