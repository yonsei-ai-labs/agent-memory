"""Bounded, explicitly guided native memory roundtrip; no policy-effect estimates."""

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import json
import sys

from jsonschema import Draft202012Validator

from agent_memory_eval.adapters.tau2 import PIN, prepare_native
from agent_memory_eval.adapters.tau2_memory import Tau2MemoryBridge
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, RunContext, digest, new_id
from agent_memory_eval.contracts.validation import ROOT, SCHEMA_ROOT
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.models.openrouter import SpendGuard
from agent_memory_eval.recording.provenance import InputSizeLimit, RoleRequestLimit


def checked_asset(ref):
    path = (ROOT / ref["path"]).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file() or digest(path.read_bytes()) != ref["sha256"]:
        raise ContractError("Connection asset is missing, outside the repository or changed")
    return path.read_bytes().decode("utf-8")


def prepare_memory_check(profile, checkout):
    schema = json.loads((SCHEMA_ROOT / "memory-connection-check.schema.json").read_text())
    Draft202012Validator(schema).validate(profile)
    connection = json.loads(checked_asset(profile["connection_profile_ref"]))
    if connection["profile_version"] != "1.1":
        raise ContractError("Memory checks require the current Luna/DeepSeek connection profile")
    instructions = {name: checked_asset(ref) for name, ref in profile["instructions"].items()}
    prepared = prepare_native(connection, checkout)
    if any(tool.name.startswith("memory_") for tool in prepared[0].agent.tools):
        raise ContractError("Native tool namespace overlaps external memory")
    return connection, instructions, prepared


def fresh_orchestrator(prepared):
    from tau2.runner.build import build_text_orchestrator
    template, native, _, _ = prepared
    return build_text_orchestrator(native.model_copy(deep=True), template.task.model_copy(deep=True),
                                   seed=native.seed)


class Tau2MemoryRunner:
    def __init__(self, journal, connection, prepared, guard, *, opener=None):
        self.journal, self.connection, self.prepared, self.guard = journal, connection, prepared, guard
        self.memory, self.opener = MemoryService(journal, record_kind="observed"), opener
        self.environments = {}

    def episode(self, *, run_id, stage, policy, solve_instruction, post_instruction, parent=None, sources=(),
                condition_id=None, mode="guided_check", block_id=None, execution_limits=None,
                assess_on_policy_limit=False):
        from tau2.runner.simulation import run_simulation
        if type(assess_on_policy_limit) is not bool or (assess_on_policy_limit and execution_limits is None):
            raise ContractError("Reached-state assessment requires explicit execution limits")
        if run_id in self.journal.scan["starts"]:
            raise ContractError("Native run ID must be new")
        orch = fresh_orchestrator(self.prepared)
        self.environments[run_id] = orch
        if not policy.enabled and (parent is not None or sources or solve_instruction or post_instruction):
            raise ContractError("No-memory runs cannot inherit memory or instructions")
        ctx = RunContext(self.journal.experiment_id, run_id, new_id("s"), condition_id or "guided_" + stage,
                         stage, "solve", "observed")
        bank = self.memory.create_bank(run_id, policy, parent) if policy.enabled else None
        state = deepcopy(self.journal.state)
        state["assignments"][run_id] = dict(stage=stage, mode=mode, policy=asdict(policy),
            bank_id=bank, parent_snapshot=parent, task_key=orch.task.id, block_id=block_id,
            condition_id=ctx.condition_id)
        if execution_limits is not None:
            state["assignments"][run_id].update(execution_limits=asdict(execution_limits),
                assess_on_policy_limit=assess_on_policy_limit, execution_contract="per-role-limits/v1")
        self.journal.append([ctx.at("prepare").event("run.started", dict(bank_id=bank,
            start_snapshot_id=parent, fresh_session=True, source_run_ids=list(sources),
            task_ref=self.journal.blobs.put(dict(role="system", content=orch.agent.system_prompt))))], state=state)
        bridge = Tau2MemoryBridge(self.journal, ctx, orch, self.connection, self.prepared[2], self.guard,
            self.prepared[3], memory=self.memory, bank_id=bank, policy=policy,
            solve_instruction=solve_instruction, post_instruction=post_instruction, opener=self.opener,
            execution_limits=execution_limits)
        grading, success, score, report_ref = "not_available", None, None, None
        termination, error_code, writing = "connection_error", None, "not_requested"
        assessment_ref = None
        with bridge:
            try:
                bridge.ensure_preload()
                result = run_simulation(orch)
                report_ref = self.journal.blobs.put(result.model_dump(mode="json"))
                score, grading, termination = result.reward_info.reward, "graded", result.termination_reason
                success = score == 1.0
            except Exception as error:
                error_code = getattr(error, "code", type(error).__name__)
                grading = "error" if orch.done else "incomplete"
                if getattr(error, "recorded_event_id", None) not in self.journal.events:
                    self.journal.append([ctx.event("runtime.error", dict(code=error_code))])
                if (assess_on_policy_limit and isinstance(error, (RoleRequestLimit, InputSizeLimit))
                        and error.actor in {"worker", "writer"}):
                    from agent_memory_eval.adapters.tau2_assessment import assess_reached_state
                    assessment_ref = self.journal.blobs.put(assess_reached_state(orch, self.connection))
                    self.journal.append([ctx.at("grade").event("business.transition", dict(
                        origin="tau2.reached_state_assessment", assessment_ref=assessment_ref,
                        limit_code=error_code, actor_role=error.actor))])
            native_counts = dict(native_steps=orch.step_count, native_tool_errors=orch.num_errors)
            final_db_hash = orch.environment.get_db_hash()
            self.journal.append([ctx.at("grade").event("business.transition", dict(origin="tau2_native_final",
                final_db_hash=final_db_hash, **native_counts))])
            self.journal.append([ctx.at("grade").event("outcome.scored", dict(grading_status=grading,
                official_success=success, official_score=score, official_artifact_ref=report_ref))])
            if grading == "graded" and policy.write_timing in {"post_task", "during_and_post"}:
                try:
                    writing = bridge.post_write()
                    if native_counts != dict(native_steps=orch.step_count, native_tool_errors=orch.num_errors):
                        raise ContractError("Post-task writing changed frozen native counters")
                except Exception as error:
                    writing, termination = "error", "post_task_error"
                    error_code = getattr(error, "code", type(error).__name__)
                    self.journal.append([ctx.at("post_task_write").event("runtime.error", dict(code=error_code))])
        snapshot = self.memory.freeze(bank) if bank is not None else None
        if bank is not None:
            self.memory.seal(bank)
        self.journal.append([ctx.at("seal").event("run.ended", dict(termination_reason=termination,
            grading_status=grading, end_snapshot_id=snapshot))])
        events = [event for event in self.journal.events.values() if event["run_id"] == run_id]
        return dict(run_id=run_id, bank_id=bank, snapshot_id=snapshot, stage=stage,
            status="completed" if grading == "graded" and writing != "error" else "error",
            grading_status=grading, official_success=success, official_score=score,
            native_steps=orch.step_count, native_tool_errors=orch.num_errors, termination_reason=termination,
            final_db_hash=final_db_hash,
            post_task_status=writing, error_code=error_code, report_ref=report_ref,
            requests_by_role=dict(Counter(e["payload"]["actor_role"] for e in events if e["type"] == "model.request_started")),
            memory_stores=sum(e["type"] == "memory.stored" for e in events),
            memory_returns=dict(Counter(e["payload"]["operation"] for e in events if e["type"] == "memory.returned")),
            memory_input_spans=sum(e["type"] == "memory.input_included" for e in events),
            **(dict(reached_state_assessment_ref=assessment_ref) if execution_limits is not None else {}))


def run_memory_check(journal, profile, prepared, *, live, opener=None):
    connection, instructions, native_prepared = prepared
    profile_text = checked_asset(profile["connection_profile_ref"])
    if json.loads(profile_text) != connection or any(digest(text.encode()) != profile["instructions"][name]["sha256"]
                                                    for name,text in instructions.items()):
        raise ContractError("Prepared model settings or instructions changed")
    collection, probe = new_id("r"), new_id("r")
    state = deepcopy(journal.state)
    state["memory_connection_check"] = dict(requested=profile, connection_profile=connection,
        instructions=instructions, model_catalog_entries=native_prepared[2],
        connection_profile_source_ref=journal.blobs.put(profile_text),
        instruction_source_refs={name:journal.blobs.put(text) for name,text in instructions.items()},
        implementation_ref=journal.blobs.put(dict(python=sys.version.split()[0],
            sources={str(path.relative_to(ROOT)):digest(path.read_bytes())
                     for path in sorted((ROOT/"src/agent_memory_eval").rglob("*.py"))})),
        native_resolved=native_prepared[1].model_dump(mode="json"),
        capabilities=dict(memory_tool_routing=True, mixed_side_effect_order=True,
            native_business_projection=True, post_task_context="same_worker_public_history",
            worker_message_rendering="provider_original", research_readiness=False))
    state["schedule"] = [dict(run_id=collection, stage="collection", task_key=connection["benchmark"]["task_id"]),
                         dict(run_id=probe, stage="probe", task_key=connection["benchmark"]["task_id"])]
    journal.append([], state=state)
    if not live:
        return dict(intent="memory_connection_check", mode="guided_check", status="prepared_only",
                    benchmark_or_model_calls=0, planned_runs=[collection, probe])
    limits = profile["guards"]
    guard = SpendGuard(max_calls=limits["max_model_requests"], max_usd=limits["max_reported_cost_usd"],
                       max_seconds=limits["max_seconds"])
    runner = Tau2MemoryRunner(journal, connection, native_prepared, guard, opener=opener)
    first = runner.episode(run_id=collection, stage="collection", policy=MemoryPolicy(**profile["policies"]["collection"]),
        solve_instruction=instructions["collection_solve"], post_instruction=instructions["post_task_write"])
    second = None
    skipped_reason = None
    if first["status"] == "completed" and not guard.unknown_cost:
        second = runner.episode(run_id=probe, stage="probe", policy=MemoryPolicy(**profile["policies"]["probe"]),
            solve_instruction=instructions["probe_solve"], post_instruction="", parent=first["snapshot_id"], sources=[collection])
    else:
        skipped_reason = "unknown_cost" if guard.unknown_cost else "collection_error"
        state = deepcopy(journal.state)
        state["skipped_runs"] = {probe: skipped_reason}
        journal.append([], state=state)
    journal.export_views()
    events = list(journal.events.values())
    successful_reads = {e["payload"]["operation"] for e in events if e["run_id"] == probe
        and e["type"] == "memory.returned" and e["payload"]["status"] == "ok" and e["payload"]["items"]}
    roundtrip = bool(first["memory_stores"] and second and second["status"] == "completed"
                     and {"list", "get"} <= successful_reads and second["memory_input_spans"])
    return dict(intent="memory_connection_check", mode="guided_check", status="completed" if roundtrip else "error",
        memory_roundtrip=roundtrip, collection=first, probe=second, native_revision=PIN,
        probe_skipped_reason=skipped_reason,
        http_calls=guard.calls, reported_cost_usd=guard.reported_usd, unknown_cost=guard.unknown_cost,
        memory_enabled=True, policy_effect_measured=False)
