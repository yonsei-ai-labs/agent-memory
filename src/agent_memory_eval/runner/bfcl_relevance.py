"""Run or regrade a registered, single-decision BFCL experiment with isolated memory banks."""

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

from agent_memory_eval.adapters.bfcl_task_start import REVISION, open_relevance
from agent_memory_eval.analysis.recorded_decisions import require, sealed_bytes
from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import MemoryPolicy, RunContext, canonical, digest, new_id, read_json
from agent_memory_eval.contracts.validation import ROOT, inspect_journal
from agent_memory_eval.memory.context import snapshot_message
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.memory.router import ToolRouter
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.memory.writing import (REQUIRED_STORE_CONTRACT, WriterFeedback, freeze_public_experience,
                                               write_from_experience)
from agent_memory_eval.models.local_qwen import LocalQwen
from agent_memory_eval.policies.writer_pairs import paired_writer_request, writer_pairs
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, Journal, read_experiment
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry, public_assistant_message

PAGE_SIZE = 1


def read_tools(cfg):
    return memory_tools(["memory_list", "memory_get"],
                        page_size=cfg.get("read_page_size", PAGE_SIZE) if cfg.get("advertise_list_limit") else None,
                        read_descriptions=cfg.get("read_tool_descriptions"))


def memory_notice(cfg, mode):
    if "memory_notice_by_delivery" in cfg:
        return cfg["memory_notice_by_delivery"][mode]
    return cfg["memory_notice"]


def worker_instruction(cfg, mode):
    base = cfg["worker_instruction"]
    notice = memory_notice(cfg, mode) if mode else ""
    return base + "\n" + notice if notice else base


def read_choice(setting, turn):
    """Historical booleans constrain zero or two calls. A prefix can release the reader after listing."""
    sequence = ("memory_list", "memory_get")
    if isinstance(setting, bool):
        names = sequence if setting else ()
    else:
        require(isinstance(setting, (list, tuple)) and tuple(setting) == sequence[:len(setting)],
                "A read control may only constrain a prefix of list, get")
        names = setting
    return dict(type="function", function=dict(name=names[turn])) if turn < len(names) else None


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


def hosted_tools(functions):
    """BFCL's Python type names and dotted names need the OpenAI function-tool representation."""
    import re
    types = {"dict": "object", "list": "array", "tuple": "array", "float": "number", "int": "integer",
             "bool": "boolean", "str": "string"}
    def schema(value):
        result = dict(value)
        if "type" in result:
            result["type"] = types.get(result["type"], result["type"])
        if "properties" in result:
            result["properties"] = {k: schema(v) for k, v in result["properties"].items()}
        if isinstance(result.get("items"), dict):
            result["items"] = schema(result["items"])
        return result
    tools = business_tools(functions)
    names = {"memory_get", "memory_list", "memory_store"}
    for tool in tools:
        fn = tool["function"]
        name = re.sub(r"[^A-Za-z0-9_-]", "_", fn["name"])
        require(0 < len(name) <= 64 and name not in names, "BFCL function names collide on the provider ABI")
        names.add(name)
        fn.update(name=name, parameters=schema(fn["parameters"]))
    return tools


def normalized_first(wire, cfg, mode, items):
    value = deepcopy(wire)
    if mode:
        system = value["messages"][0]
        notice = memory_notice(cfg, mode)
        suffix = "\n" + notice if notice else ""
        require(system["role"] == "system" and system["content"].endswith(suffix), "Unexpected memory instructions")
        system["content"] = system["content"].removesuffix(suffix)
        if "worker_instruction" in cfg:
            require(system["content"] == cfg["worker_instruction"], "Unexpected business instructions")
        if mode == "preload":
            position = next((i for i, message in enumerate(value["messages"]) if message["role"] != "system"), None)
            require(position is not None and value["messages"].pop(position) == snapshot_message(items),
                    "Different preloaded bank")
        else:
            private = read_tools(cfg)
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


def solve(journal, cfg, case, client, is_empty, *, source=None, writer=None, form=None, mode=None, read_control=False,
          transfer=None, run_id=None, target_baseline=None):
    category, row = case
    controls = dict(model=cfg["model"], revision=cfg["model_revision"], generation=cfg["generation"])
    reference = transfer["target_start"] if transfer else (source["start"] if source else None)
    if target_baseline is not None:
        require(not any((source, writer, form, mode, read_control, transfer)),
                "Target no-memory controls require an independent solve")
        reference = target_baseline["target_start"]
    start = open_relevance(row, category, controls, reference=reference)
    public = read_json(start.public_input)
    tools = (hosted_tools if "wire_parameters" in cfg else business_tools)(public["functions"])
    native_messages = public["messages"]
    run = run_id or row["id"] + "-" + (f"{form}-{mode}" if mode else "source")
    condition = f"{form}-{mode}" if mode else "no_memory"
    pool = transfer.get("pool") if transfer else None
    if pool is not None:
        condition = transfer["condition"]
    ctx = RunContext(journal.experiment_id, run, new_id("s"), condition, "diagnostic", "solve", "observed")
    policy = MemoryPolicy(bool(mode), "none", False, cfg.get("read_page_size", PAGE_SIZE), read_mode=mode or "none")
    memory = MemoryService(journal, record_kind="observed")
    snapshot = writer["snapshot_id"] if writer else None
    parents = []
    if pool is not None:
        own = [note for note in pool if note["source_task"] == row["id"]]
        require(len(own) == 1 and own[0]["snapshot"] == snapshot and own[0]["writer_run"] == writer["run_id"]
                and own[0]["source_run"] == transfer["source_run"] and row["id"] == transfer["source_task"]
                and ((transfer["bank"] == "own" and len(pool) == 1)
                     or (transfer["bank"] == "mixed" and len(pool) > 1)),
                "A cohort reader requires its own sealed writer and the registered bank")
        parents = [run_id for note in pool for run_id in (note["source_run"], note["writer_run"])]
        if len(pool) > 1:
            snapshots = [note["snapshot"] for note in pool]
            snapshot = next((key for key, value in journal.state["snapshots"].items()
                             if value["parent_snapshot_ids"] == snapshots), None) or memory.compose(snapshots)
    elif transfer:
        require(writer and transfer["source_task"] != row["id"] and transfer["bank"] in {"real", "empty"},
                "A transfer requires an independent target and sealed writer")
        if transfer["bank"] == "empty":
            from agent_memory_eval.memory.imports import install_snapshot
            snapshot = install_snapshot(memory, policy, {}, [], 0, {})
    if writer and pool is None:
        parents = [transfer["source_run"] if transfer else source["run"], writer["run_id"]]
    items = snapshot_items(journal.blobs, journal.state, snapshot) if writer else []
    bank = memory.fork(snapshot, run, policy) if writer else None
    event = ctx.at("prepare").event("run.started", dict(bank_id=bank, start_snapshot_id=snapshot, fresh_session=True,
        task_ref=journal.blobs.put(native_messages[0]), source_run_ids=parents))
    opening_events = [event]
    question_event = None
    if len(native_messages) > 1:
        # This native BFCL turn begins with a task-specific system instruction and then its public user question.
        question_event = ctx.event("business.transition", dict(origin="bfcl.public_question",
            public_message_ref=journal.blobs.put(native_messages[-1])))
        opening_events.append(question_event)
    if target_baseline is not None:
        opening_events.append(ctx.event("business.transition", dict(origin="bfcl.target_no_memory",
            target_start_ref=journal.blobs.put(start.record()), index=target_baseline["index"],
            draw=target_baseline["draw"])))
    if pool is not None:
        opening_events.append(ctx.event("business.transition", dict(origin="bfcl.cohort_assignment",
            target_start_ref=journal.blobs.put(start.record()), bank=transfer["bank"], sources=pool)))
    journal.append(opening_events)
    if transfer and pool is None:
        journal.append([ctx.event("business.transition", dict(origin="bfcl.transfer_assignment",
            target_start_ref=journal.blobs.put(start.record()), source_task=transfer["source_task"],
            donor_snapshot=writer["snapshot_id"], bank=transfer["bank"]))])
    registry = SourceRegistry(journal, ctx)
    wire = dict(model=cfg["model"], seed=cfg["generation"]["seed"], max_completion_tokens=cfg["generation"]["worker_tokens"],
                system=dict(role="system", content=worker_instruction(cfg, mode)),
                tools=tools + (read_tools(cfg) if mode == "on_demand" else []))
    wire.update(cfg.get("wire_parameters", {}))
    template = registry.template(wire, roles=("worker",))
    history = [(template, "/system")]
    if question_event:
        history.append((registry.add("task_initial", event["event_id"]), ""))
    if mode == "preload":
        preload = memory.preload(ctx, bank)
        history.append((registry.add("memory_snapshot", preload["event_id"]), ""))
    question_source = (registry.add("business_result", question_event["event_id"]) if question_event
                       else registry.add("task_initial", event["event_id"]))
    history.append((question_source, ""))
    first = dict(wire)
    first["messages"] = [first.pop("system"), *native_messages[:-1],
                         *([snapshot_message(items)] if mode == "preload" else []), native_messages[-1]]
    business = normalized_first(first, cfg, mode, items)
    if source:
        require(business == source["business"], "Worker's first business input changed before submission")
    gateway = RecordingGateway(journal)
    router = ToolRouter(journal, memory, SimpleNamespace(stop_tools=set(), ended=False), bank, policy)
    limit = cfg["limits"][(mode or "source") + "_calls"]
    grade, reason = None, "private_read_limit"
    require(not read_control or mode == "on_demand", "A constrained read control needs the retrieval interface")
    for turn in range(limit):
        choice = read_choice(read_control, turn)
        turn_template = registry.template(dict(wire, tool_choice=choice), roles=("worker",)) if choice else template
        def check(actual):
            require(actual.get("tool_choice") == choice, "Unexpected decoder constraint")
            if turn == 0:
                expected = dict(first, tool_choice=choice) if choice else first
                require(actual == expected, "Submitted first request differs from its approved input")
        reply, own, request_id = gateway.call(ctx, registry, "worker", turn_template, history, client,
            max_requests=limit, validate_wire=check, project_self_authored=True)
        response = journal.blobs.get(registry.sources[own]["artifact_ref"])
        if "wire_parameters" in cfg:
            from agent_memory_eval.memory.writing import writer_response_error
            identity = dict(id=client.model["id"], canonical_slug=cfg["model_revision"],
                            provider_name=client.model["selected_endpoint"]["provider_name"])
            problem = writer_response_error(response, model_identity=identity, observed=True)
            if problem:
                reason = problem
                break
        if response["choices"][0]["finish_reason"] == "length" or response.get("local_generation", {}).get("decode_error"):
            reason = "incomplete_or_malformed_response"
            break
        names = [c["function"]["name"] for c in reply.get("tool_calls") or []]
        private = {"memory_list", "memory_get"}
        if not names or any(n not in private for n in names):
            if private.intersection(names):
                reason = "mixed_private_business_response"
                break
            try:
                grade = grade_reply(reply, category, is_empty, tools)
            except (ValueError, TypeError):
                if "wire_parameters" not in cfg:
                    raise
                reason = "malformed_business_call"
                break
            reason = "business_decision"
            if not source and not transfer and (target_baseline is None or target_baseline.get("writer_source")):
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


def registered_slots(cfg):
    """Declared (form, delivery) readers, or every form with every delivery for older profiles."""
    if "conditions" in cfg:
        return [tuple(slot) for slot in cfg["conditions"]]
    return [(f, m) for f in cfg["forms"] for m in cfg["delivery"]]


def collect_task(root, cfg, case, client, is_empty, *, parent=None, pairs=None):
    category, row = case
    task = row["id"]
    folder = root / task
    if parent is not None:
        origin, checksum = parent
        shutil.copytree(origin, folder)
        sealed_bytes(folder / "log.jsonl", checksum)
    with Journal(folder, cfg["name"], contract_version="1.2") as journal:
        expected = {}
        if parent is None:
            source = solve(journal, cfg, case, client, is_empty)
            if source["grade"] is None:
                return
            forms = cfg["forms"]
        else:
            # The parent journal is sealed. Compare each actual old wire before the new writer can send.
            requests = {}
            for e in journal.events.values():
                if e["type"] == "model.request_started" and e["run_id"] not in requests:
                    requests[e["run_id"]] = journal.blobs.get(e["payload"]["request_ref"])
            source_run = task + "-source"
            source = dict(run=source_run, business=requests[source_run], start=open_relevance(row, category,
                dict(model=cfg["model"], revision=cfg["model_revision"], generation=cfg["generation"])))
            forms = {}
            for pair in pairs.values():
                ref, new = pair["reference"], pair["candidate"]
                forms[new] = cfg["forms"][new]
                wire = paired_writer_request(requests[f"{task}-write-{ref}"], cfg["writer_instruction"],
                                             cfg["forms"][ref], forms[new])
                expected[new] = digest(canonical(wire))
        experience = freeze_public_experience(journal, source["run"])
        writers = {}
        for form, line in forms.items():
            writers[form] = write_from_experience(journal, experience, WriterFeedback(cfg.get("writer_feedback", "result_only")),
                instruction=cfg["writer_instruction"] + "\n" + line,
                policy=MemoryPolicy(True, "post_task", False, 1, read_mode="none", max_items=1),
                client=client, model_config=dict(model=cfg["model"], seed=cfg["generation"]["seed"],
                    max_completion_tokens=cfg["generation"]["writer_tokens"]),
                max_requests=cfg["limits"]["writer_calls"], condition_id=form, run_id=f"{task}-write-{form}",
                completion_contract=REQUIRED_STORE_CONTRACT, first_input_sha256=expected.get(form))
        completed = {f: w for f, w in writers.items() if w["status"] == "completed" and w["memory_stores"] == 1}
        if len(completed) != len(writers) and not (cfg.get("policy_registration") or cfg.get("reader_admission") == "sealed_bank"):
            return
        writers = completed
        slots = [(f, m) for f, m in registered_slots(cfg) if f in writers]
        for form, mode in sorted(slots, key=lambda slot: digest(canonical([task, *slot]))):
            solve(journal, cfg, case, client, is_empty, source=source, writer=writers[form], form=form, mode=mode)


def audit_task(root, cfg, case, is_empty, *, sources_only=False, allow_partial=False):
    category, row = case
    task = row["id"]
    folder = root / task
    log = folder / "log.jsonl"
    interrupted = set()
    if allow_partial and not log.exists():
        scan, state, experiment = inspect_journal(b""), {}, None
    else:
        scan, state, experiment = read_experiment(folder)
    if allow_partial and (scan["tail_bytes"] or scan["incomplete_runs"]):
        # Native BFCL sessions execute sequentially within one task journal. Audit its closed prefix normally.
        ended_at = max((i for i, record in enumerate(scan["records"], 1)
                        if any(e["type"] == "run.ended" for e in record["events"])), default=0)
        interrupted = set(scan["incomplete_runs"])
        prefix = b"".join(log.read_bytes().splitlines(keepends=True)[:ended_at])
        scan = inspect_journal(prefix)
        state = BlobStore(folder).get(scan["records"][-1]["state_ref"]) if ended_at else {}
    audit_requests(folder, evidence=(scan, state, experiment))
    require(not scan["incomplete_runs"] and not scan["pending_requests"], "Unclosed local trial")
    blobs = BlobStore(folder)
    events = list(scan["events"].values())
    source = task + "-source"
    sessions = [e["session_id"] for e in scan["starts"].values()]
    require(len(sessions) == len(set(sessions)), "Reused session")
    firsts, rows, source_wire, experience_ref = {}, [], None, None
    slots = [(source, "source", None, None)]
    if not sources_only:
        slots += [(f"{task}-write-{f}", "write", f, None) for f in cfg["forms"]]
        slots += [(f"{task}-{f}-{m}", "solve", f, m) for f, m in registered_slots(cfg)]
    for run, phase, form, mode in slots:
        rec = dict(task=task, run=run, phase=phase, form=form, mode=mode, success=None, status="not_started")
        rows.append(rec)
        if run in interrupted:
            rec["status"] = "interrupted"
            continue
        if run not in scan["starts"]:
            continue
        batch = [e for e in events if e["run_id"] == run]
        starts = [e["payload"] for e in batch if e["type"] == "model.request_started"]
        finishes = {e["payload"]["request_id"]: e["payload"] for e in batch if e["type"] == "model.request_finished"}
        end, = [e["payload"] for e in batch if e["type"] == "run.ended"]
        rec.update(status=end["termination_reason"], session=scan["starts"][run]["session_id"], requests=len(starts),
                   reused_requests=0, prompt_tokens=0, completion_tokens=0)
        reads = Counter()
        for q in starts:
            f = finishes[q["request_id"]]
            require(f["status"] == "response" and f["cost_usd"] == 0, "Local request lacks an observed receipt")
            raw = blobs.get(f["response_ref"])
            rec["reused_requests"] += int("reused_local_generation" in raw)
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
            require(exp["source_run_id"] == source and writer["feedback"]["level"] == cfg.get("writer_feedback", "result_only"),
                    "Wrong writer source")
            if experience_ref is None:
                experience_ref = writer["experience_ref"]
            require(writer["experience_ref"] == experience_ref, "Formats received different public experiences")
            if "representation_pairs" in cfg or "policy_writer_pairs" in cfg or cfg.get("reader_admission") == "sealed_bank":
                wire = blobs.get(starts[0]["request_ref"])
                instruction = cfg["writer_instruction"] + "\n" + cfg["forms"][form]
                require(writer["instruction"] == instruction and wire["messages"][0] == dict(role="system", content=instruction)
                        and writer["completion_contract"] == REQUIRED_STORE_CONTRACT and wire["tool_choice"] == "required",
                        "Writer changed the registered instruction or storage contract")
                rec["writer_instruction_sha256"] = digest(instruction.encode())
                rec["writer_request_sha256"] = starts[0]["request_ref"]["sha256"]
                rec["writer_shared_input_sha256"] = digest(canonical(dict(wire, messages=wire["messages"][1:])))
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
        require(original["messages"] == [dict(role="system", content=cfg["worker_instruction"]), *row["question"][0]]
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
        if "representation_pairs" in cfg or cfg.get("reader_admission") == "sealed_bank":
            last = starts[-1]
            finish = finishes[last["request_id"]]
            raw = blobs.get(finish["response_ref"])
            rec["decision"] = dict(request_sha256=last["request_ref"]["sha256"],
                input_manifest_sha256=last["input_manifest_ref"]["sha256"], response_sha256=finish["response_ref"]["sha256"],
                finish_reason=raw["choices"][0]["finish_reason"], decode_error=raw["local_generation"].get("decode_error"))
        if rec["success"] is not None:
            raw = blobs.get(finishes[starts[-1]["request_id"]]["response_ref"])
            g = grade_reply(raw["choices"][0]["message"], category, is_empty, original["tools"])
            require(g == blobs.get(scored["official_artifact_ref"]) and g["valid"] is rec["success"], "Grade mismatch")
            rec.update(any_call=g["any_call"], undeclared=g["undeclared_functions"], missing_required=g["missing_required"])
    return dict(journal_sha256=digest(log.read_bytes()) if log.exists() else None, allocations=rows)


def comparison_inputs(cfg, args):
    parent, output = args.parent_recording.resolve(), args.recording.resolve()
    require(not output.is_relative_to(parent) and not parent.is_relative_to(output),
            "Use separate directories for the archived and new recordings")
    spec = read_json(args.writer_comparison.read_bytes())
    require(spec["format"] == "bfcl-archived-writer-comparison/v1" and spec["read_mode"] in cfg["delivery"]
            and spec["parent_profile_sha256"] == digest(args.profile.read_bytes()), "Wrong parent or delivery mode")
    archive = read_json(asset_text(ROOT, spec["parent_archive_ref"]).encode())
    require(archive["registration"]["profile_sha256"] == spec["parent_profile_sha256"]
            and read_json((args.parent_recording / "registration.json").read_bytes()) == archive["registration"]
            and read_json((args.parent_recording / "runtime.json").read_bytes()) == archive["runtime"],
            "Source recording differs from the pinned study")
    clause = asset_text(ROOT, spec["writer_clause_ref"]).rstrip("\r\n")
    conditions, pairs = writer_pairs(cfg["forms"], clause)
    return dict(cfg, forms={c["id"]: c["prompt"] for c in conditions}, delivery=[spec["read_mode"]],
                representation_pairs=pairs), archive


def reuse_generations(root, client, registration):
    previous = read_json((root / "registration.json").read_bytes())
    require(previous["profile_sha256"] == registration["profile_sha256"] and previous["tasks"] == registration["tasks"]
            and read_json((root / "runtime.json").read_bytes()) == client.runtime, "Incompatible local recording")
    for task in previous["tasks"]:
        folder = root / task
        evidence = read_experiment(folder)
        audit_requests(folder, evidence=evidence)
        scan = evidence[0]
        require(not scan["incomplete_runs"] and not scan["pending_requests"], "Recovery expects closed prior sessions")
        events = list(scan["events"].values())
        starts = {e["payload"]["request_id"]: e["payload"] for e in events if e["type"] == "model.request_started"}
        blobs = BlobStore(folder)
        for e in events:
            if e["type"] != "model.request_finished" or e["payload"]["status"] != "response":
                continue
            f = e["payload"]
            raw = blobs.get(f["response_ref"])
            gen = raw["local_generation"]
            req = starts[f["request_id"]]["request_ref"]
            wire = blobs.get(req)
            require(raw["model"] == wire["model"] == client.model["id"] and raw["provider"] == "local-transformers"
                    and "reused_local_generation" not in raw and digest(gen["rendered_input"].encode()) == gen["input_sha256"]
                    and gen["completion_tokens"] == len(gen["output_token_ids"]), "Unverified original generation")
            origin = dict(recording=root.name, task=task, run=e["run_id"], request_id=f["request_id"],
                          request_sha256=req["sha256"], response_sha256=f["response_ref"]["sha256"])
            client.saved[client.wire_key(wire)].append((gen, origin))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["run", "analyze"])
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--model-python", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--reuse-recording", type=Path, help="Reuse each identical original local request at most once")
    parser.add_argument("--writer-comparison", type=Path, help="Apply one registered writer change across representations")
    parser.add_argument("--parent-recording", type=Path, help="Sealed source and reference decisions for the writer comparison")
    args = parser.parse_args()
    cfg = read_json(args.profile.read_bytes())
    if "policy_registration" in cfg:
        from agent_memory_eval.runner.policy_round import verify_native_registration
        verify_native_registration(args.profile, cfg, "bfcl-relevance")
        require(not (args.writer_comparison or args.reuse_recording or args.parent_recording),
                "A registered two-pass round starts from its own first pass")
    cases, is_empty = load_inputs(args.checkout, cfg)
    archive = None
    if bool(args.writer_comparison) != bool(args.parent_recording) or (args.writer_comparison and args.reuse_recording):
        parser.error("An archived writer comparison needs its own parent recording and fresh candidate requests")
    if args.writer_comparison:
        cfg, archive = comparison_inputs(cfg, args)
        require([row["id"] for _, row in cases] == archive["registration"]["tasks"], "Changed source cohort")
    root = args.recording
    if args.phase == "run":
        if not args.model_python or not args.checkpoint or args.checkpoint.name != cfg["model_revision"]:
            parser.error("A local interpreter and the registered model checkpoint are required")
        for name, expected in cfg["checkpoint_sha256"].items():
            with (args.checkpoint / name).open("rb") as stream:
                require(hashlib.file_digest(stream, "sha256").hexdigest() == expected, f"Changed checkpoint file {name}")
        root.mkdir(parents=True, exist_ok=False)
        identity = subprocess.check_output(["git", "-C", str(Path(__file__).parent), "rev-parse", "HEAD"], text=True).strip()
        reg = dict(profile_sha256=digest(args.profile.read_bytes()), implementation=identity, tasks=[c[1]["id"] for c in cases])
        if archive:
            reg["writer_comparison_sha256"] = digest(args.writer_comparison.read_bytes())
        if args.reuse_recording:
            reg["original_recording"] = args.reuse_recording.name
            reg["original_registration_sha256"] = digest((args.reuse_recording / "registration.json").read_bytes())
        (root / "registration.json").write_bytes(canonical(reg) + b"\n")
        client = LocalQwen(args.model_python, args.checkpoint, cfg["model"], cfg["generation"]["seed"],
                           stop_at_chat_end=cfg["generation"].get("stop_at_chat_end", False))
        try:
            if archive:
                require(client.runtime == archive["runtime"], "Candidate generation settings differ from the references")
            (root / "runtime.json").write_bytes(canonical(client.runtime) + b"\n")
            if args.reuse_recording:
                reuse_generations(args.reuse_recording, client, reg)
            for case in cases:
                task = case[1]["id"]
                parent = (args.parent_recording / task, archive["tasks"][task]["journal_sha256"]) if archive else None
                collect_task(root, cfg, case, client, is_empty, parent=parent, pairs=cfg.get("representation_pairs"))
                print("Finished", case[1]["id"], file=sys.stderr, flush=True)
        finally:
            client.close()
    reg = read_json((root / "registration.json").read_bytes())
    require(reg["profile_sha256"] == digest(args.profile.read_bytes()) and reg["tasks"] == [r["id"] for _, r in cases]
            and reg.get("writer_comparison_sha256") == (digest(args.writer_comparison.read_bytes()) if archive else None),
            "Protocol or allocations changed")
    tasks = {case[1]["id"]: audit_task(root, cfg, case, is_empty) for case in cases}
    report = dict(format="bfcl-local-two-pass/v1", registration=reg, runtime=read_json((root / "runtime.json").read_bytes()),
                  external_calls=0, tasks=tasks)
    if archive:
        from agent_memory_eval.analysis.bfcl_relevance import trace_questions, trials
        from agent_memory_eval.analysis.paired_trials import compare_trials
        mode, = cfg["delivery"]
        require(report["runtime"] == archive["runtime"], "Different registered model runtime")
        for task, data in tasks.items():
            parent_log = args.parent_recording / task / "log.jsonl"
            checksum = archive["tasks"][task]["journal_sha256"]
            prefix_size = len(sealed_bytes(parent_log, checksum))
            sealed_bytes(root / task / "log.jsonl", checksum, prefix_size)
            writers = [r for r in data["allocations"] if r["phase"] == "write" and r["status"] != "not_started"]
            require(len({w["writer_shared_input_sha256"] for w in writers}) == 1
                    and len({w["snapshot"] for w in writers}) == len(writers), "Changed shared writer input or reused bank")
            candidates = {p["candidate"] for p in cfg["representation_pairs"].values()}
            fresh = {r["run"] for r in data["allocations"] if r["form"] in candidates and r["status"] != "not_started"}
            with (root / task / "log.jsonl").open("rb") as stream:
                stream.seek(prefix_size)
                added = [e for line in stream for e in read_json(line)["events"] if e["type"] == "model.request_started"]
            require({e["run_id"] for e in added} == fresh, "Additional model calls outside the registered candidate slots")
        report["representation_pairs"] = cfg["representation_pairs"]
        report["paired_outcomes"] = {form: compare_trials(trials(report, "solve", pair["reference"], mode),
            trials(report, "solve", pair["candidate"], mode), metrics=("tokens", "requests"), test_signs=False)
            for form, pair in cfg["representation_pairs"].items()}
        report["evidence"] = trace_questions(report, root)
    print(canonical(report).decode())


if __name__ == "__main__":
    main()
