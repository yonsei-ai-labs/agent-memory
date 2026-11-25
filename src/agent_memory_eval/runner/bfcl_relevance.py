"""Run or regrade a registered, single-decision BFCL experiment with isolated memory banks."""

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from agent_memory_eval.adapters.bfcl_task_start import REVISION, open_relevance
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import MemoryPolicy, RunContext, canonical, digest, new_id, read_json
from agent_memory_eval.memory.context import snapshot_message
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.memory.router import ToolRouter
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.memory.writing import (REQUIRED_STORE_CONTRACT, WriterFeedback, freeze_public_experience,
                                               write_from_experience)
from agent_memory_eval.models.local_qwen import LocalQwen
from agent_memory_eval.recording.journal import BlobStore, Journal, read_experiment
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry, public_assistant_message


def load_inputs(checkout, cfg):
    require(subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
            == cfg["revision"] == REVISION, "Wrong BFCL revision")
    package = checkout / "berkeley-function-call-leaderboard"
    sys.path.insert(0, str(package.resolve()))
    from bfcl_eval import utils
    from bfcl_eval.utils import is_empty_output

    def pinned(path):
        return subprocess.check_output(["git", "-C", checkout, "show",
            f"{REVISION}:berkeley-function-call-leaderboard/bfcl_eval/{path}"])
    require(Path(utils.__file__).read_bytes() == pinned("utils.py"), "Changed BFCL output-format checker")
    cases = []
    for category, ids in cfg["tasks"].items():
        raw = pinned(f"data/BFCL_v4_{category}.json")
        require(digest(raw) == cfg["data_sha256"][category], "Changed public BFCL data")
        rows = {r["id"]: r for line in raw.splitlines() if line for r in [read_json(line)]}
        cases.extend((category, rows[task]) for task in ids)
    return cases, is_empty_output


def business_tools(functions):
    tools = []
    for fn in functions:
        schema = deepcopy(fn)
        require(schema["parameters"]["type"] in ("dict", "object"), "Unexpected BFCL function ABI")
        schema["parameters"]["type"] = "object"
        tools.append(dict(type="function", function=schema))
    return tools


def normalized_first(wire, cfg, mode, items):
    value = deepcopy(wire)
    if mode:
        suffix = "\n" + cfg["memory_notice"]
        require(value["messages"][0]["content"].endswith(suffix), "Unexpected memory instructions")
        value["messages"][0]["content"] = value["messages"][0]["content"].removesuffix(suffix)
        if mode == "preload":
            require(value["messages"].pop(1) == snapshot_message(items), "Different preloaded bank")
        else:
            private = memory_tools(["memory_list", "memory_get"])
            require(mode == "on_demand" and value["tools"][-len(private):] == private, "Different retrieval interface")
            del value["tools"][-len(private):]
    return value


def grade_reply(reply, category, is_empty, tools):
    calls = [c["function"] for c in reply.get("tool_calls") or []]
    decoded = [{c["name"]: read_json(c["arguments"].encode())} for c in calls]
    any_call = not is_empty(decoded)
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in tools}
    unknown, missing = [], []
    for call in decoded:
        (name, args), = call.items()
        if name not in schemas:
            unknown.append(name)
        else:
            missing.extend([name, k] for k in schemas[name].get("required", []) if k not in args)
    return dict(metric="bfcl.relevance.call_presence/v1", category=category, any_call=any_call,
                valid=any_call == (category == "live_relevance"), decoded=decoded,
                undeclared_functions=unknown, missing_required=missing)


def solve(journal, cfg, case, client, is_empty, *, source=None, writer=None, form=None, mode=None):
    category, row = case
    controls = dict(model=cfg["model"], revision=cfg["model_revision"], generation=cfg["generation"])
    start = open_relevance(row, category, controls, reference=source["start"] if source else None)
    public = read_json(start.public_input)
    tools = business_tools(public["functions"])
    question, = public["messages"]
    run = row["id"] + "-" + (f"{form}-{mode}" if mode else "source")
    ctx = RunContext(journal.experiment_id, run, new_id("s"), f"{form}-{mode}" if mode else "no_memory",
                     "diagnostic", "solve", "observed")
    policy = MemoryPolicy(bool(mode), "none", False, 1, read_mode=mode or "none")
    memory = MemoryService(journal, record_kind="observed")
    snapshot = writer["snapshot_id"] if writer else None
    items = snapshot_items(journal.blobs, journal.state, snapshot) if writer else []
    bank = memory.fork(snapshot, run, policy) if writer else None
    event = ctx.at("prepare").event("run.started", dict(bank_id=bank, start_snapshot_id=snapshot, fresh_session=True,
        task_ref=journal.blobs.put(question), source_run_ids=[source["run"], writer["run_id"]] if writer else []))
    journal.append([event])
    registry = SourceRegistry(journal, ctx)
    wire = dict(model=cfg["model"], seed=cfg["generation"]["seed"], max_completion_tokens=cfg["generation"]["worker_tokens"],
                system=dict(role="system", content=cfg["worker_instruction"] + ("\n" + cfg["memory_notice"] if mode else "")),
                tools=tools + (memory_tools(["memory_list", "memory_get"]) if mode == "on_demand" else []))
    template = registry.template(wire, roles=("worker",))
    history = [(template, "/system")]
    if mode == "preload":
        preload = memory.preload(ctx, bank)
        history.append((registry.add("memory_snapshot", preload["event_id"]), ""))
    history.append((registry.add("task_initial", event["event_id"]), ""))
    first = dict(wire)
    first["messages"] = [first.pop("system"), *([snapshot_message(items)] if mode == "preload" else []), question]
    business = normalized_first(first, cfg, mode, items)
    if source:
        require(business == source["business"], "Worker's first business input changed before submission")
    gateway = RecordingGateway(journal)
    router = ToolRouter(journal, memory, SimpleNamespace(stop_tools=set(), ended=False), bank, policy)
    limit = cfg["limits"][(mode or "source") + "_calls"]
    grade, reason = None, "private_read_limit"
    for turn in range(limit):
        def check(actual):
            if turn == 0:
                require(actual == first, "Submitted first request differs from its approved input")
        reply, own, request_id = gateway.call(ctx, registry, "worker", template, history, client,
            max_requests=limit, validate_wire=check, project_self_authored=True)
        response = journal.blobs.get(registry.sources[own]["artifact_ref"])
        if response["choices"][0]["finish_reason"] == "length" or response["local_generation"].get("decode_error"):
            reason = "incomplete_or_malformed_response"
            break
        names = [c["function"]["name"] for c in reply.get("tool_calls") or []]
        private = {"memory_list", "memory_get"}
        if not names or any(n not in private for n in names):
            if private.intersection(names):
                reason = "mixed_private_business_response"
                break
            grade = grade_reply(reply, category, is_empty, tools)
            reason = "business_decision"
            if not source:
                for message in [dict(role="system", content=canonical(dict(available_functions=tools)).decode()),
                                public_assistant_message(reply)]:
                    journal.append([ctx.event("business.transition", dict(origin="bfcl.public_observation",
                        public_message_ref=journal.blobs.put(message)))])
            break
        if mode != "on_demand":
            reason = "unavailable_memory_tool"
            break
        history.append((own, ""))
        for _, kind, event_id in router.dispatch(ctx, request_id, reply["tool_calls"]):
            history.append((registry.add(kind, event_id), ""))
    status = "graded" if grade is not None else "not_available"
    journal.append([ctx.at("grade").event("outcome.scored", dict(grading_status=status,
        official_success=grade["valid"] if grade else None, official_score=int(grade["valid"]) if grade else None,
        official_artifact_ref=journal.blobs.put(grade) if grade else None))])
    journal.append([ctx.at("seal").event("run.ended", dict(termination_reason=reason, grading_status=status,
        end_snapshot_id=snapshot))])
    return dict(start=start, business=business, run=run, grade=grade)


def collect_task(root, cfg, case, client, is_empty):
    task = case[1]["id"]
    with Journal(root / task, cfg["name"], contract_version="1.2") as journal:
        source = solve(journal, cfg, case, client, is_empty)
        if source["grade"] is None:
            return
        experience = freeze_public_experience(journal, source["run"])
        writers = {}
        for form, line in cfg["forms"].items():
            writers[form] = write_from_experience(journal, experience, WriterFeedback("result_only"),
                instruction=cfg["writer_instruction"] + "\n" + line,
                policy=MemoryPolicy(True, "post_task", False, 1, read_mode="none", max_items=1),
                client=client, model_config=dict(model=cfg["model"], seed=cfg["generation"]["seed"],
                    max_completion_tokens=cfg["generation"]["writer_tokens"]),
                max_requests=cfg["limits"]["writer_calls"], condition_id=form, run_id=f"{task}-write-{form}",
                completion_contract=REQUIRED_STORE_CONTRACT)
        if not all(w["status"] == "completed" and w["memory_stores"] == 1 for w in writers.values()):
            return
        slots = [(f, m) for f in writers for m in cfg["delivery"]]
        for form, mode in sorted(slots, key=lambda slot: digest(canonical([task, *slot]))):
            solve(journal, cfg, case, client, is_empty, source=source, writer=writers[form], form=form, mode=mode)


def audit_task(root, cfg, case, is_empty):
    category, row = case
    task = row["id"]
    folder = root / task
    scan, state, experiment = read_experiment(folder)
    audit_requests(folder, evidence=(scan, state, experiment))
    require(not scan["incomplete_runs"] and not scan["pending_requests"], "Unclosed local trial")
    blobs = BlobStore(folder)
    events = list(scan["events"].values())
    source = task + "-source"
    sessions = [e["session_id"] for e in scan["starts"].values()]
    require(len(sessions) == len(set(sessions)), "Reused session")
    firsts, rows, source_wire, experience_ref = {}, [], None, None
    slots = [(source, "source", None, None)]
    slots += [(f"{task}-write-{f}", "write", f, None) for f in cfg["forms"]]
    slots += [(f"{task}-{f}-{m}", "solve", f, m) for f in cfg["forms"] for m in cfg["delivery"]]
    for run, phase, form, mode in slots:
        rec = dict(task=task, run=run, phase=phase, form=form, mode=mode, success=None, status="not_started")
        rows.append(rec)
        if run not in scan["starts"]:
            continue
        batch = [e for e in events if e["run_id"] == run]
        starts = [e["payload"] for e in batch if e["type"] == "model.request_started"]
        finishes = {e["payload"]["request_id"]: e["payload"] for e in batch if e["type"] == "model.request_finished"}
        end, = [e["payload"] for e in batch if e["type"] == "run.ended"]
        rec.update(status=end["termination_reason"], session=scan["starts"][run]["session_id"], requests=len(starts),
                   prompt_tokens=0, completion_tokens=0)
        reads = Counter()
        for q in starts:
            f = finishes[q["request_id"]]
            require(f["status"] == "response" and f["cost_usd"] == 0, "Local request lacks an observed receipt")
            raw = blobs.get(f["response_ref"])
            generation = raw["local_generation"]
            require(digest(generation["rendered_input"].encode()) == generation["input_sha256"], "Changed model prompt")
            for key in ("prompt_tokens", "completion_tokens"):
                require(generation[key] == f["usage"][key], "Token receipt differs from generation")
                rec[key] += f["usage"][key]
            calls = raw["choices"][0]["message"].get("tool_calls") or []
            reads.update(c["function"]["name"] for c in calls if c["function"]["name"] in {"memory_list", "memory_get"})
        head = scan["starts"][run]["payload"]
        if phase == "write":
            writer = state["memory_writes"][run]
            exp = blobs.get(writer["experience_ref"])
            require(exp["source_run_id"] == source and writer["feedback"]["level"] == "result_only", "Wrong writer source")
            if experience_ref is None:
                experience_ref = writer["experience_ref"]
            require(writer["experience_ref"] == experience_ref, "Formats received different public experiences")
            snapshot = end["end_snapshot_id"]
            stored = snapshot_items(blobs, state, snapshot)
            rec.update(snapshot=snapshot, items=len(stored), experience_sha256=experience_ref["sha256"])
            firsts[form] = snapshot
            continue
        first = blobs.get(starts[0]["request_ref"])
        items = snapshot_items(blobs, state, head["start_snapshot_id"]) if mode else []
        original = normalized_first(first, cfg, mode, items)
        if phase == "source":
            source_wire = original
        else:
            require(firsts[form] == head["start_snapshot_id"] and source_wire == original and
                    head["source_run_ids"] == [source, f"{task}-write-{form}"], "Wrong source, bank or business input")
        require(original["messages"] == [dict(role="system", content=cfg["worker_instruction"]), row["question"][0][0]]
                and original["tools"] == business_tools(row["function"]), "Worker did not receive the public BFCL task")
        specs = [e["payload"] for e in batch if e["type"] == "memory.input_included"]
        fields = sorted({e["field"] for e in specs})
        for q in starts:
            scoped = [p for p in specs if p["request_id"] == q["request_id"] and p["field"] == "body"]
            if scoped:
                rendered = blobs.get(finishes[q["request_id"]]["response_ref"])["local_generation"]["rendered_input"]
                require(len(items) == 1 and canonical(items[0]["body"]).decode()[1:-1] in rendered,
                        "Returned memory body missing from actual tokenized prompt")
        require(mode != "preload" or fields == ["body", "title"], "Preload did not reach the worker")
        scored, = [e["payload"] for e in batch if e["type"] == "outcome.scored"]
        rec.update(success=scored["official_success"], fields=fields, reads=dict(reads),
                   business_sha256=digest(canonical(original)), snapshot=head["start_snapshot_id"])
        if rec["success"] is not None:
            raw = blobs.get(finishes[starts[-1]["request_id"]]["response_ref"])
            g = grade_reply(raw["choices"][0]["message"], category, is_empty, original["tools"])
            require(g == blobs.get(scored["official_artifact_ref"]) and g["valid"] is rec["success"], "Grade mismatch")
            rec.update(any_call=g["any_call"], undeclared=g["undeclared_functions"], missing_required=g["missing_required"])
    return dict(journal_sha256=digest((folder / "log.jsonl").read_bytes()), allocations=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["run", "analyze"])
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--model-python", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args()
    cfg = read_json(args.profile.read_bytes())
    cases, is_empty = load_inputs(args.checkout, cfg)
    root = args.recording
    if args.phase == "run":
        if not args.model_python or not args.checkpoint or args.checkpoint.name != cfg["model_revision"]:
            parser.error("A local interpreter and the registered model checkpoint are required")
        for name, expected in cfg["checkpoint_sha256"].items():
            with (args.checkpoint / name).open("rb") as stream:
                require(hashlib.file_digest(stream, "sha256").hexdigest() == expected, f"Changed checkpoint file {name}")
        root.mkdir(parents=True, exist_ok=False)
        identity = subprocess.check_output(["git", "-C", str(Path(__file__).parent), "rev-parse", "HEAD"], text=True).strip()
        (root / "registration.json").write_bytes(canonical(dict(profile_sha256=digest(args.profile.read_bytes()),
            implementation=identity, tasks=[case[1]["id"] for case in cases])) + b"\n")
        client = LocalQwen(args.model_python, args.checkpoint, cfg["model"], cfg["generation"]["seed"])
        try:
            (root / "runtime.json").write_bytes(canonical(client.runtime) + b"\n")
            for case in cases:
                collect_task(root, cfg, case, client, is_empty)
                print("Finished", case[1]["id"], file=sys.stderr, flush=True)
        finally:
            client.close()
    reg = read_json((root / "registration.json").read_bytes())
    require(reg["profile_sha256"] == digest(args.profile.read_bytes()) and reg["tasks"] == [r["id"] for _, r in cases],
            "Protocol or allocations changed")
    tasks = {case[1]["id"]: audit_task(root, cfg, case, is_empty) for case in cases}
    report = dict(format="bfcl-local-two-pass/v1", registration=reg, runtime=read_json((root / "runtime.json").read_bytes()),
                  external_calls=0, tasks=tasks)
    print(canonical(report).decode())


if __name__ == "__main__":
    main()
