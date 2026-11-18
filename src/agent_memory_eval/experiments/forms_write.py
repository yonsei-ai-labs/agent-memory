"""Verified first-pass public experience and isolated, optional form writers."""

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.memory_writing import audit_memory_writes
from agent_memory_eval.contracts.connection import provider_route
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, RunContext, canonical, digest, new_id, read_json
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.memory.writing import WRITER_COMPLETION_CONTRACT, WriterFeedback, write_from_experience
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.experience import _closed_run, _pair, _source, public_text_message
from agent_memory_eval.recording.journal import safe_path


@dataclass(frozen=True)
class FormSource:
    task_id: str
    source_run_id: str
    source_log_sha256: str
    source_end_event_id: str
    source_end_seq: int
    public_context: str
    public_context_sha256: str
    public_context_bytes: int
    official_success: bool  # For analysis only, never installed in a writer journal.


def _project_context(wire, response):
    messages = wire["messages"]
    if (not messages or messages[0]["role"] != "system" or any(m["role"] == "system" for m in messages[1:])
            or any(t["function"]["name"].startswith("memory_") for t in wire.get("tools", []))):
        raise ContractError("A first-pass source must be a no-memory worker context with one public policy")
    reply = response["choices"][0]["message"]
    if reply["role"] != "assistant":
        raise ContractError("The final worker request lacks a public assistant response")
    return dict(kind="past_public_experience", task_context=public_text_message(messages[0]),
                messages=[public_text_message(m) for m in [*messages[1:], reply]])


def load_form_sources(design, root):
    """Verify one locked baseline journal, then project each selected final worker exchange once."""
    root = Path(root).absolute()
    source = design["source"]
    cohort = source["cohort"]
    if source["feedback_level"] != "none" or digest(canonical(cohort)) != source["cohort_sha256"]:
        raise ContractError("The source cohort or its feedback policy changed")
    source_root = safe_path(root, source["root"])
    summary_ref = dict(path="baseline-summary.json", sha256=source["summary_sha256"])
    summary = read_json(asset_text(source_root, summary_ref).encode())
    receipt_ref = dict(path=source["receipt_path"], sha256=source["receipt_sha256"])
    audit = read_json(asset_text(root, receipt_ref).encode())["audit"]
    if (audit["log_sha256"] != source["log_sha256"] or audit["summary_sha256"] != source["summary_sha256"]
            or audit["runs"] != summary["runs"] or summary["native_revision"] != source["benchmark_revision"]
            or summary["domain"] != "airline" or summary["memory_enabled"] is not False):
        raise ContractError("The pinned baseline summary and receipt disagree on the source run")
    selected = [r for r in summary["runs"]
                if r["repetition"] == 1 and r["task_id"] not in source["excluded_tasks"]]
    by_task = {r["task_id"]: r for r in selected}
    expected_count = design["allocation"]["planned"]["eligible_source_episodes"]
    if (len(by_task) != len(selected) or len(cohort) != expected_count or len(by_task) != len(cohort)
            or set(by_task) != {r["task_id"] for r in cohort}
            or len({r["run_id"] for r in cohort}) != len(cohort)):
        raise ContractError("The cohort must cover every eligible first repetition exactly once")
    sources = {}
    with _source(source_root, source["log_sha256"]) as (scan, blobs, _, experiment):
        if experiment != summary["experiment_id"]:
            raise ContractError("The source summary names another experiment")
        for row in cohort:
            task, run = row["task_id"], row["run_id"]
            reported = by_task[task]
            if run != reported["run_id"] or reported["status"] != "completed" or reported["grading_status"] != "graded":
                raise ContractError("The task does not name its completed and graded first repetition")
            start, end, outcome = _closed_run(scan, run)
            scored = outcome["payload"]
            if (start["record_kind"] != "observed" or start["condition_id"] != "no_memory"
                    or start["payload"]["bank_id"] is not None or start["payload"]["start_snapshot_id"] is not None
                    or blobs.get(start["payload"]["task_ref"]).get("task_id") != task
                    or scored["official_success"] is not reported["official_success"]
                    or scored["official_score"] != reported["official_score"]
                    or scored["official_artifact_ref"] != reported["report_ref"]):
                raise ContractError("Source task, no-memory assignment or recorded outcome differs")
            request, finish = _pair(scan, run)
            context = _project_context(blobs.get(request["payload"]["request_ref"]),
                                       blobs.get(finish["payload"]["response_ref"]))
            data = canonical(context)
            if digest(data) != row["public_context_sha256"] or len(data) != row["public_context_bytes"]:
                raise ContractError("Public context hash or byte length differs for task " + task)
            sources[task] = FormSource(task, run, source["log_sha256"], end["event_id"], end["event_seq"],
                                       data.decode(), row["public_context_sha256"], len(data), scored["official_success"])
    return sources


def read_form_experience(blobs, scan, value):
    """Reopen an imported projection without the private source, grade, or simulator artifacts."""
    ref = value["source_ref"]
    event = scan["events"][value["import_event_id"]]
    if (event["type"] != "business.transition" or event["run_id"] is not None
            or event["payload"] != dict(origin="forms.public_experience_import", source_ref=ref)):
        raise ContractError("The public writer input lacks its original local import event")
    source = blobs.get(ref)
    context = blobs.get(source["public_context_ref"])
    data = canonical(context)
    if (source["source_record_kind"] != "observed" or len(data) != source["public_context_bytes"]
            or digest(data) != source["public_context_sha256"] or context["kind"] != "past_public_experience"
            or set(context) != {"kind", "task_context", "messages"}
            or context["task_context"] != blobs.get(source["task_ref"]) or context["task_context"]["role"] != "system"
            or context["task_context"] != public_text_message(context["task_context"])
            or not context["messages"] or context["messages"][-1]["role"] != "assistant"
            or any(m["role"] == "system" or public_text_message(m) != m for m in context["messages"])):
        raise ContractError("The imported first-pass projection or its provenance changed")
    return dict(format=value["format"], source_run_id=source["source_run_id"], task_ref=source["task_ref"],
                record_kind=event["record_kind"], source_record_kind=source["source_record_kind"],
                source_end_event_id=source["source_end_event_id"], source_end_seq=source["source_end_seq"],
                local_ready_seq=event["event_seq"], observations=[dict(message=m) for m in context["messages"]])


def _import(journal, design, source, repetition, arm, run_id, record_kind):
    row = next((r for r in design["source"]["cohort"] if r["task_id"] == source.task_id), None)
    data = source.public_context.encode()
    context = read_json(data)
    if (row is None or source.source_run_id != row["run_id"] or source.source_log_sha256 != design["source"]["log_sha256"]
            or source.public_context_sha256 != row["public_context_sha256"] or len(data) != row["public_context_bytes"]
            or len(data) != source.public_context_bytes or digest(data) != source.public_context_sha256
            or canonical(context) != data):
        raise ContractError("Writer source differs from the verified task projection")
    proof = dict(task_id=source.task_id, source_run_id=source.source_run_id,
                 source_log_sha256=source.source_log_sha256, source_end_event_id=source.source_end_event_id,
                 source_end_seq=source.source_end_seq, source_record_kind="observed",
                 public_context_sha256=source.public_context_sha256, public_context_bytes=len(data),
                 public_context_ref=journal.blobs.put(context), task_ref=journal.blobs.put(context["task_context"]))
    proof_ref = journal.blobs.put(proof)
    event = RunContext(journal.experiment_id, record_kind=record_kind).event(
        "business.transition", dict(origin="forms.public_experience_import", source_ref=proof_ref))
    value = dict(format="agent-memory-forms-public-experience/v1", import_event_id=event["event_id"], source_ref=proof_ref)
    ref = journal.blobs.put(value)
    state = deepcopy(journal.state)
    state["form_writer"] = dict(task_id=source.task_id, repetition=repetition, arm=arm, run_id=run_id,
                                experience_ref=ref, public_context_sha256=source.public_context_sha256)
    journal.append([event], state=state)
    return ref


def _bank_descriptor(blobs, scan, state):
    assignment = state["form_writer"]
    run = assignment["run_id"]
    config = state["memory_writes"][run]
    if (set(scan["starts"]) != {run} or set(state["memory_writes"]) != {run}
            or config["completion_contract"] != WRITER_COMPLETION_CONTRACT
            or config["feedback"] != {"level": "none", "critique": None, "critique_origin": None}
            or config["experience_ref"] != assignment["experience_ref"]):
        raise ContractError("The journal does not contain the assigned isolated writer")
    source = blobs.get(blobs.get(config["experience_ref"])["source_ref"])
    start = scan["starts"][run]
    if (source["task_id"] != assignment["task_id"] or source["public_context_sha256"] != assignment["public_context_sha256"]
            or start["payload"]["source_run_ids"] != [source["source_run_id"]] or start["condition_id"] != assignment["arm"]
            or start["payload"]["bank_id"] != config["bank_id"] or start["payload"]["fresh_session"] is not True):
        raise ContractError("The authored bank belongs to another writer or source")
    events = [e for e in scan["events"].values() if e["run_id"] == run]
    end = [e["payload"] for e in events if e["type"] == "run.ended"]
    if len(end) != 1 or end[0]["termination_reason"] not in {"memory_write_completed", "memory_write_empty"}:
        raise ContractError("Only normally finished writers, including voluntary empty writers, can supply a bank")
    snapshot_id = end[0]["end_snapshot_id"]
    snapshot = state["snapshots"][snapshot_id]
    bank = state["banks"][config["bank_id"]]
    frozen = [e["payload"] for e in scan["events"].values() if e["type"] == "bank.snapshot_created"
              and e["payload"]["snapshot_id"] == snapshot_id]
    if (len(frozen) != 1 or blobs.get(frozen[0]["manifest_ref"]) != snapshot or snapshot["bank_id"] != config["bank_id"]
            or bank["owner_run"] != run or not bank["sealed"] or bank["parent_snapshot"] is not None or bank["start_heads"]):
        raise ContractError("The writer's sealed snapshot or its creating event differs")
    requested = {}
    for event in events:
        payload = event["payload"]
        if event["type"] == "model.request_finished" and payload["status"] == "response":
            message = blobs.get(payload["response_ref"])["choices"][0]["message"]
            for index, call in enumerate(message.get("tool_calls") or []):
                if call["function"]["name"] == "memory_store":
                    key = "op_" + digest(canonical([run, payload["request_id"], call["id"], index]))
                    requested[key] = call["function"]["arguments"]
    stores = [e["payload"] for e in events if e["type"] == "memory.stored"]
    heads = {}
    for revision, stored in enumerate(stores, 1):
        item, version = stored["item_id"], stored["version_id"]
        memory = blobs.get(stored["body_ref"])
        call_args = read_json(requested.get(stored["operation_id"], "{}").encode())
        if (not isinstance(call_args, dict) or call_args.get("title") != memory["title"] or call_args.get("body") != memory["body"]
                or call_args.get("expected_version") != stored["parent_version_id"]
                or call_args.get("id") != (item if item in heads else None)):
            raise ContractError("The immutable memory differs from the writer's public storage call")
        if (stored["bank_id"] != config["bank_id"] or stored["head_revision"] != revision
                or stored["parent_version_id"] != heads.get(item) or state["versions"][version] != stored["body_ref"]
                or (memory["id"], memory["version"], memory["title"]) != (item, version, stored["title"])
                or digest(memory["body"].encode()) != stored["body_sha256"]):
            raise ContractError("An immutable memory differs from its original writer event")
        heads[item] = version
    if (snapshot["heads"] != heads or snapshot["order"] != list(heads) or snapshot["head_revision"] != len(stores)
            or (bank["heads"], bank["order"], bank["revision"]) != (heads, list(heads), len(stores))):
        raise ContractError("The final bank differs from the ordered writer events")
    status = "completed" if heads else "empty"
    if end[0]["termination_reason"] != "memory_write_" + status:
        raise ContractError("The writer's terminal status differs from the stored items")
    return dict(task_id=assignment["task_id"], repetition=assignment["repetition"], arm=assignment["arm"],
                run_id=run, snapshot_id=snapshot_id, status=status, items=snapshot_items(blobs, state, snapshot_id),
                head_revision=len(stores), public_context_sha256=source["public_context_sha256"])


def read_form_bank(writer_root, *, expected_log_sha256=None):
    """Verify the closed writer against its journal and export immutable heads, including an empty bank."""
    writer_root = Path(writer_root).absolute()
    expected = expected_log_sha256 if expected_log_sha256 is not None else digest(safe_path(writer_root, "log.jsonl").read_bytes())
    with _source(writer_root, expected) as (scan, blobs, _, _):
        audit_memory_writes(writer_root)
        return _bank_descriptor(blobs, scan, blobs.get(scan["records"][-1]["state_ref"]))


def run_form_writer(journal, design, source, *, repetition, arm, client, guard, root, run_id=None):
    """Run one scheduled assignment with the caller's client and common per-request spend guard."""
    writing = design["writing"]
    if (journal.scan["records"] or guard is None or getattr(client, "guard", None) is not guard
            or design["source"]["feedback_level"] != "none" or writing["completion_contract"] != WRITER_COMPLETION_CONTRACT
            or writing["tool"] != "memory_store"):
        raise ContractError("Writing requires a fresh journal, the shared guard and the frozen feedback/tool contract")
    blocks = [b for b in design["allocation"]["blocks"] if b["task_id"] == source.task_id and b["repetition"] == repetition]
    seeds = [s["writer"] for s in design["model"]["seed_pairs"] if s["repetition"] == repetition]
    if (type(repetition) is not int or len(blocks) != 1 or arm not in blocks[0]["write"] or len(seeds) != 1
            or arm not in writing["lines"]):
        raise ContractError("Unknown writer assignment or seed")
    line = writing["lines"][arm]
    if not isinstance(line, str) or not line.strip() or (("\n" in line or "\r" in line) and "conditions" not in design):
        raise ContractError("Each writing form requires a single instruction line")
    root = Path(root).absolute()
    instruction = asset_text(root, writing["common_instruction_ref"]).rstrip("\r\n") + "\n" + line
    connection = read_json(asset_text(root, design["model"]["connection_profile_ref"]).encode())
    offline = bool(getattr(client, "offline", False))
    config = dict(model="synthetic-offline" if offline else connection["worker"]["model_id"],
                  provider=provider_route(connection, "worker"), stream=False, **deepcopy(connection["worker"]["parameters"]))
    config["seed"] = seeds[0]
    limits = writing["proposed_limits"]
    policy = MemoryPolicy(True, "post_task", False, 1, read_mode="none", max_items=limits["bank_items"],
                          max_payload_bytes=limits["bank_payload_bytes"], max_return_bytes=limits["bank_return_bytes"])
    execution = ExecutionLimits(0, limits["requests"], 0, limits["input_bytes"])
    if run_id is not None and (not isinstance(run_id, str) or not run_id):
        raise ContractError("An assigned writer run ID must be a nonempty string")
    run_id = new_id("writer") if run_id is None else run_id
    ref = _import(journal, design, source, repetition, arm, run_id, "illustrative" if offline else "observed")
    result = write_from_experience(journal, ref, WriterFeedback("none"), instruction=instruction, policy=policy,
                                   client=client, model_config=config, condition_id=arm, run_id=run_id,
                                   max_requests=None, execution_limits=execution)
    finishes = [e["payload"] for e in journal.events.values() if e["type"] == "model.request_finished"]
    reported_cost = sum((Decimal(str(p["cost_usd"])) for p in finishes if p["cost_status"] == "observed"), Decimal(0))
    token_counts = [(p["usage"] or {}).get("total_tokens") for p in finishes]
    bank = None if result["status"] == "error" else _bank_descriptor(journal.blobs, journal.scan, journal.state)
    return dict(result, task_id=source.task_id, repetition=repetition, arm=arm,
                record_kind=journal.scan["starts"][run_id]["record_kind"],
                session_id=journal.scan["starts"][run_id]["session_id"], requests=len(finishes),
                reported_tokens=sum(n for n in token_counts if type(n) is int and n >= 0),
                unknown_cost_requests=sum(p["cost_status"] == "unknown" for p in finishes),
                reported_cost_usd_decimal=str(reported_cost), bank=bank,
                partial_snapshot_id=result["snapshot_id"] if result["status"] == "error" else None,
                log_sha256=digest(journal.data))
