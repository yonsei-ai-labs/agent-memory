"""Exercise the BFCL on-demand path against a copy of the original source journals."""

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import shutil
import subprocess

from agent_memory_eval.adapters.bfcl_task_start import open_relevance
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require, sealed_bytes
from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.models.local_qwen import LocalQwen, required_prefix
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.runner.bfcl_relevance import load_inputs, read_choice, read_tools, solve


def expected_runtime(parent, spec):
    runtime = dict(parent)
    if "stop_token_ids" in spec:
        runtime["stop_token_ids"] = spec["stop_token_ids"]
    return runtime


def run(args, cfg, spec, archive):
    cases, is_empty = load_inputs(args.checkout, cfg)
    require([r["id"] for _, r in cases] == archive["registration"]["tasks"]
            and len(cases) == spec["limits"]["allocated"]
            and cfg["limits"]["on_demand_calls"] == spec["limits"]["worker_calls_per_question"],
            "Wrong scope for read-path check")
    require(args.checkpoint.name == cfg["model_revision"], "Wrong checkpoint revision")
    for name, checksum in cfg["checkpoint_sha256"].items():
        with (args.checkpoint / name).open("rb") as stream:
            require(hashlib.file_digest(stream, "sha256").hexdigest() == checksum, "Changed local checkpoint")
    args.control.mkdir(parents=True, exist_ok=False)
    reg = dict(spec_sha256=digest(args.spec.read_bytes()), original_archive_sha256=digest(args.archive.read_bytes()),
               implementation=subprocess.check_output(["git", "-C", str(Path(__file__).parent), "rev-parse", "HEAD"],
                                                      text=True).strip(), tasks=archive["registration"]["tasks"])
    if args.arm:
        reg["arm"] = args.arm
    (args.control / "registration.json").write_bytes(canonical(reg) + b"\n")
    client = LocalQwen(args.model_python, args.checkpoint, cfg["model"], cfg["generation"]["seed"],
                       stop_at_chat_end="stop_token_ids" in spec)
    try:
        require(client.runtime == expected_runtime(archive["runtime"], spec), "Different inference runtime")
        (args.control / "runtime.json").write_bytes(canonical(client.runtime) + b"\n")
        notice = "\n".join(filter(None, (cfg["memory_notice"], spec["append_to_neutral_notice"])))
        guided = dict(cfg, memory_notice=notice, advertise_list_limit=spec.get("advertise_list_limit", False))
        controls = dict(model=cfg["model"], revision=cfg["model_revision"], generation=cfg["generation"])
        for case in cases:
            category, row = case
            task = row["id"]
            parent = archive["tasks"][task]
            source_id = task + "-source"
            source = RecordedDecisions(args.parent / task, source_id, parent["journal_sha256"])
            source_req, = source.requests
            baseline = dict(start=open_relevance(row, category, controls), run=source_id,
                            business=source.blobs.get(source_req["request_ref"]))
            writer, = [r for r in parent["allocations"] if r["phase"] == "write" and r["form"] == spec["parent_form"]]
            require(writer["items"] == 1, "The parent must have one real stored note")
            folder = args.control / task
            shutil.copytree(args.parent / task, folder)
            sealed_bytes(folder / "log.jsonl", parent["journal_sha256"])
            with Journal(folder, cfg["name"], contract_version="1.2") as journal:
                solve(journal, guided, case, client, is_empty, source=baseline, form=spec["control_label"],
                      writer=dict(run_id=writer["run"], snapshot_id=writer["snapshot"]), mode="on_demand",
                      read_control=spec.get("constrain_read_names", False))
            print(task, flush=True)
    finally:
        client.close()


def inputs(args):
    spec = read_json(args.spec.read_bytes())
    if "arms" in spec or args.arm:
        require(args.arm and args.arm in spec.get("arms", {}), "Select a registered arm")
        spec = dict(spec, **spec["arms"][args.arm])
    cfg = read_json(sealed_bytes(args.parent_profile, spec["parent_profile_sha256"]))
    archive = read_json(sealed_bytes(args.archive, spec["parent_archive_sha256"]))
    require(read_json((args.parent / "registration.json").read_bytes()) == archive["registration"]
            and archive["registration"]["profile_sha256"] == spec["parent_profile_sha256"],
            "Original archive does not belong to registration")
    return cfg, spec, archive


def inspect_reader(record, expected_snapshot, *, constrain_names=False, advertise_list_limit=False, stops=()):
    note, = record.items
    bank = record.state["banks"][record.start["bank_id"]]
    require(bank["parent_snapshot"] == record.start["start_snapshot_id"] == expected_snapshot
            and bank["start_heads"] == {note["id"]: note["version"]} and bool(note["body"].strip()),
            "Reader was not connected to the registered nonempty version")
    steps = []
    first = None
    for turn, req in enumerate(record.requests):
        wire = record.blobs.get(req["request_ref"])
        finish = record.finishes[req["request_id"]]
        raw = record.blobs.get(finish["response_ref"])
        gen = raw["local_generation"]
        rendered = gen["rendered_input"]
        definitions = rendered.split("<tools>\n", 1)[1].split("\n</tools>", 1)[0].splitlines()
        choice = read_choice(constrain_names, turn)
        require(finish["status"] == "response" and finish["cost_usd"] == 0 and raw["provider"] == "local-transformers"
                and [read_json(line.encode()) for line in definitions] == wire["tools"]
                and wire["tools"][-2:] == read_tools(dict(advertise_list_limit=advertise_list_limit))
                and wire.get("tool_choice") == choice and gen["forced_prefix"] == required_prefix(wire)
                and gen["text"] == gen["forced_prefix"] + gen["emitted_text"]
                and digest(rendered.encode()) == gen["input_sha256"]
                and len(gen["output_token_ids"]) == gen["completion_tokens"], "Wrong tool interface or generation record")
        if stops:
            ids = gen["output_token_ids"]
            require(not set(stops).intersection(ids[:-1]) and gen["ended"] == (bool(ids) and ids[-1] in stops),
                    "Generation continued past a registered end token")
        if first is None:
            first = wire
            require([m["role"] for m in wire["messages"]] == ["system", "user"] and note["id"] not in rendered,
                    "A private value entered the opening")
        spans = record.visible(req, wire)
        fields = sorted({s["field"] for s in spans})
        tool_messages = [m["content"] for m in wire["messages"] if m["role"] == "tool"]
        for message in tool_messages:
            require(message in rendered, "An actual tool return is missing from the rendered model input")
        for field in fields:
            value = canonical(note[field]).decode()[1:-1]
            require(any(value in message for message in tool_messages), "Memory field missing from a tool return")
        names = [c["function"]["name"] for c in raw["choices"][0]["message"].get("tool_calls") or []]
        read_args = [read_json(c["function"]["arguments"].encode()) for c in raw["choices"][0]["message"].get("tool_calls") or []
                     if c["function"]["name"] in {"memory_list", "memory_get"}]
        literal_calls = re.findall(r"<function=([^>\s]+)>", gen["text"])
        malformed = bool(gen.get("decode_error")) or not gen["ended"]
        if not malformed:
            require(literal_calls == names, "A generated tool call was lost during decoding")
        steps.append(dict(request=req["request_ref"]["sha256"], response=finish["response_ref"]["sha256"],
            prompt=gen["input_sha256"], read_arguments=read_args, decode_error=gen.get("decode_error"),
            extra_text_after_chat_end=bool(gen["text"].partition("<|im_end|>")[2].strip()),
            output_tokens_sha256=digest(canonical(gen["output_token_ids"])), calls=names, fields=fields,
            raw_mentions_read=bool(re.search(r"\bmemory_(?:list|get)\b", gen["emitted_text"], re.I)),
            constrained_name=choice["function"]["name"] if choice else None, prompt_tokens=gen["prompt_tokens"],
            completion_tokens=gen["completion_tokens"],
            incomplete=malformed, reused="reused_local_generation" in raw))
    returns = []
    for event in record.of_type("memory.returned"):
        result = event["payload"]
        if result["status"] == "ok":
            require(all((x["item_id"], x["version_id"]) == (note["id"], note["version"]) for x in result["items"]),
                    "A tool returned a different memory version")
        returns.append([result["operation"], result["status"]])
    end, = record.of_type("run.ended")
    outcome, = record.of_type("outcome.scored")
    return first, dict(run=record.run, snapshot=expected_snapshot, session=end["session_id"], steps=steps,
                       returns=returns, denials=[e["payload"]["code"] for e in record.of_type("policy.denied")],
                       termination=end["payload"]["termination_reason"], success=outcome["payload"]["official_success"])


def summarize(rows):
    steps = [s for r in rows for s in r["steps"]]
    counts = Counter(name for step in steps for name in step["calls"])
    return dict(readers=len(rows), requests=len(steps), sessions=len({r["session"] for r in rows}),
        nonempty_snapshots=len({r["snapshot"] for r in rows}),
        unique_first_prompts=len({r["steps"][0]["prompt"] for r in rows}),
        unique_first_token_outputs=len({r["steps"][0]["output_tokens_sha256"] for r in rows}),
        first_business_call=sum(any(n not in {"memory_list", "memory_get"} for n in r["steps"][0]["calls"]) for r in rows),
        first_text_only=sum(not r["steps"][0]["calls"] and not r["steps"][0]["incomplete"] for r in rows),
        list_calls=counts["memory_list"], get_calls=counts["memory_get"],
        first_list_ok=sum(r["steps"][0]["calls"] == ["memory_list"] and r["returns"][:1] == [["list", "ok"]] for r in rows),
        successful_returns=dict(Counter(op for r in rows for op, status in r["returns"] if status == "ok")),
        readers_with_title=sum(any("title" in s["fields"] for s in r["steps"]) for r in rows),
        readers_with_body=sum(any("body" in s["fields"] for s in r["steps"]) for r in rows),
        body_before_business_decision=sum(r["termination"] == "business_decision" and "body" in r["steps"][-1]["fields"]
                                          for r in rows),
        raw_mentions_read=sum(s["raw_mentions_read"] for s in steps), incomplete=sum(s["incomplete"] for s in steps),
        prompt_tokens=sum(s["prompt_tokens"] for s in steps), completion_tokens=sum(s["completion_tokens"] for s in steps),
        reused=sum(s["reused"] for s in steps), denials=sum(len(r["denials"]) for r in rows),
        text_after_chat_end=sum(s["extra_text_after_chat_end"] for s in steps),
        graded=sum(r["success"] is not None for r in rows), successes=sum(r["success"] is True for r in rows),
        terminations=dict(Counter(r["termination"] for r in rows)))


def audit(args, cfg, spec, archive):
    reg = read_json((args.control / "registration.json").read_bytes())
    require(reg["spec_sha256"] == digest(args.spec.read_bytes()) and reg["tasks"] == archive["registration"]["tasks"]
            and reg["original_archive_sha256"] == spec["parent_archive_sha256"]
            and reg.get("arm") == args.arm
            and read_json((args.control / "runtime.json").read_bytes()) == expected_runtime(archive["runtime"], spec),
            "Control registration changed")
    original, guided, unique_by_question = [], [], {}
    seals = {}
    for task in reg["tasks"]:
        data = archive["tasks"][task]
        parent = args.parent / task
        folder = args.control / task
        source_log = sealed_bytes(parent / "log.jsonl", data["journal_sha256"])
        control_log = (folder / "log.jsonl").read_bytes()
        require(control_log.startswith(source_log), "Control did not branch from the sealed parent")
        audit_requests(folder)
        seals[task] = digest(control_log)
        first_prompts, first_outputs = set(), set()
        for row in data["allocations"]:
            if row["mode"] != "on_demand":
                continue
            evidence = RecordedDecisions(parent, row["run"], data["journal_sha256"])
            first, observation = inspect_reader(evidence, row["snapshot"])
            require(first["messages"][0]["content"] == cfg["worker_instruction"] + "\n" + cfg["memory_notice"],
                    "Unexpected original memory notice")
            original.append(observation)
            first_prompts.add(observation["steps"][0]["prompt"])
            first_outputs.add(observation["steps"][0]["output_tokens_sha256"])
            if row["form"] == spec["parent_form"]:
                control_id = task + "-" + spec["control_label"] + "-on_demand"
                control = RecordedDecisions(folder, control_id, seals[task])
                constrain = spec.get("constrain_read_names", False)
                limited = spec.get("advertise_list_limit", False)
                wire, observed = inspect_reader(control, row["snapshot"], constrain_names=constrain,
                    advertise_list_limit=limited, stops=spec.get("stop_token_ids", ()))
                expected = deepcopy(first)
                if limited:
                    expected["tools"][-2:] = read_tools(dict(advertise_list_limit=True))
                if spec["append_to_neutral_notice"]:
                    expected["messages"][0]["content"] += "\n" + spec["append_to_neutral_notice"]
                if constrain:
                    expected["tool_choice"] = dict(type="function", function=dict(name="memory_list"))
                require(wire == expected and control.start["source_run_ids"] == evidence.start["source_run_ids"],
                        "Control differs beyond the registered guidance")
                guided.append(observed)
        unique_by_question[task] = dict(prompts=len(first_prompts), token_outputs=len(first_outputs))
    require(len(original) == len(reg["tasks"]) * len(cfg["forms"]) and len(guided) == spec["limits"]["allocated"],
            "Missing assigned readers")
    condition = args.arm or ("constrained" if spec.get("constrain_read_names") else "directed")
    return {"format": "bfcl-on-demand-read-check/v1", "registration": reg, "control_journals_sha256": seals,
            "neutral": dict(summary=summarize(original), unique_first_by_question=unique_by_question, readers=original),
            condition: dict(summary=summarize(guided), readers=guided)}


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parent", type=Path, required=True)
    p.add_argument("--control", type=Path, required=True)
    p.add_argument("--parent-profile", type=Path, required=True)
    p.add_argument("--spec", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--arm", help="Arm name for a multi-arm registered check")
    return p


if __name__ == "__main__":
    parser = arguments()
    parser.add_argument("--audit", action="store_true", help="Read both recordings without running the model")
    for arg in ("checkout", "checkpoint", "model-python"):
        parser.add_argument("--" + arg, type=Path)
    args = parser.parse_args()
    if args.audit:
        print(canonical(audit(args, *inputs(args))).decode())
    else:
        if not all([args.checkout, args.checkpoint, args.model_python]):
            parser.error("Collection requires --checkout, --checkpoint and --model-python")
        run(args, *inputs(args))
