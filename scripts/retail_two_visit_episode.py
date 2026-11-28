"""Native retail source, writer, first-decision and continuation operations for derived two-visit tasks."""

import time
from copy import deepcopy
from dataclasses import asdict

from agent_memory_eval.adapters.tau2 import Tau2Bridge
from agent_memory_eval.adapters.tau2_memory import Tau2MemoryBridge
from agent_memory_eval.contracts.connection import provider_route
from agent_memory_eval.contracts.core import (
    ContractError,
    MemoryPolicy,
    RunContext,
    canonical,
    digest,
    new_id,
    read_json,
)
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.contracts.snapshot_replay import replay_response_error
from agent_memory_eval.memory.context import (
    memory_system_suffix,
    past_memory_instruction,
    snapshot_message,
)
from agent_memory_eval.memory.imports import install_snapshot
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.memory.writing import WriterFeedback, write_from_experience
from agent_memory_eval.models.openrouter import ProviderError
from agent_memory_eval.recording.experience import import_recorded_experience
from agent_memory_eval.recording.provenance import RecordingGateway
from agent_memory_eval.runner.tau2_baseline import build_baseline_orchestrator
from scripts.analyze_full_retail import require


def native_prefix(messages):
    from tau2.data_model.message import (
        AssistantMessage,
        ToolCall,
        ToolMessage,
        UserMessage,
    )

    native = []
    for m in messages:
        if m["role"] == "assistant":
            calls = [
                ToolCall(
                    id=c["id"],
                    name=c["function"]["name"],
                    arguments=read_json(c["function"]["arguments"].encode()),
                )
                for c in m.get("tool_calls", [])
            ]
            msg = AssistantMessage(role="assistant", content=m.get("content"), tool_calls=calls or None)
        elif m["role"] == "tool":
            msg = ToolMessage(role="tool", id=m["tool_call_id"], content=m["content"])
        else:
            require(m["role"] == "user", "Unregistered prefix role")
            msg = UserMessage.text(m["content"])
        native.append(msg)
    return native


def build_episode(protocol, connection, task, *, prefix=None):
    cfg = deepcopy(connection)
    seed = protocol["source_seed"] if prefix is None else protocol["target_seed"]
    cfg["benchmark"].update(
        domain="retail",
        task_id=task["id"],
        seed=seed,
        max_steps=protocol["native_steps"],
        max_errors=protocol["native_errors"],
    )
    cfg["guards"]["max_seconds"] = protocol["episode_seconds"]
    cfg["worker"]["parameters"]["seed"] = seed
    bound = deepcopy(task)
    if prefix is not None:
        bound["initial_state"] = {"message_history": [m.model_dump(mode="json") for m in native_prefix(prefix)]}
    orch, native = build_baseline_orchestrator(cfg, bound)
    return cfg, orch, native


def policy_for(arm):
    mode = {
        "query": "on_demand",
        "empty_query": "on_demand",
        "preload": "preload",
        "empty_preload": "preload",
        "no_memory": "none",
    }[arm]
    return MemoryPolicy(
        mode != "none",
        "none",
        False,
        4,
        read_mode=mode,
        max_items=16,
        max_payload_bytes=65536,
        max_return_bytes=131072,
    )


def start(journal, job, runtime, orch, policy, items, sources):
    require(not journal.scan["records"], "Refusing to start a previously attempted session")
    kind = "illustrative" if runtime["clients"]["worker"].offline else "observed"
    ctx = RunContext(
        journal.experiment_id,
        job["run"],
        new_id("s"),
        job["arm"],
        "collection" if job["stage"] == "source" else "probe",
        "solve",
        kind,
    )
    memory = MemoryService(journal, record_kind=kind)
    parent = bank = None
    if policy.enabled:
        heads = {item["id"]: item["version"] for item in items}
        require(
            len(heads) == len(items) <= policy.max_items
            and len(canonical({"items": items})) <= policy.max_return_bytes,
            "Starting bank exceeds the registered budget or contains a duplicate",
        )
        parent = install_snapshot(
            memory, policy, heads, list(heads), len(heads), {i["version"]: journal.blobs.put(i) for i in items}
        )
        bank = memory.fork(parent, ctx.run_id, policy)
    state = deepcopy(journal.state)
    state["assignments"][ctx.run_id] = {
        "job": job,
        "execution_contract": "per-role-limits/v1",
        "execution_limits": runtime["protocol"]["limits"],
        "policy": asdict(policy),
    }
    journal.append(
        [
            ctx.at("prepare").event(
                "run.started",
                {
                    "bank_id": bank,
                    "start_snapshot_id": parent,
                    "fresh_session": True,
                    "source_run_ids": sources,
                    "task_ref": journal.blobs.put({"role": "system", "content": orch.agent.system_prompt}),
                },
            )
        ],
        state=state,
    )
    return ctx, memory, bank


def finish(journal, ctx, memory, bank, reason, result=None):
    grade = "graded" if result is not None else "not_available"
    score = result["reward"] if result is not None else None
    ref = journal.blobs.put(result) if result is not None else None
    snapshot = None
    if bank:
        snapshot = memory.freeze(bank)
        memory.seal(bank)
    journal.append(
        [
            ctx.at("grade").event(
                "outcome.scored",
                {
                    "grading_status": grade,
                    "official_success": None if score is None else score == 1,
                    "official_score": score,
                    "official_artifact_ref": ref,
                },
            ),
            ctx.at("seal").event(
                "run.ended", {"termination_reason": reason, "grading_status": grade, "end_snapshot_id": snapshot}
            ),
        ]
    )


def run_source(journal, job, task, runtime):
    from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation

    cfg, orch, native = build_episode(runtime["protocol"], runtime["connection"], task)
    ctx, memory, bank = start(journal, job, runtime, orch, policy_for("no_memory"), [], [])
    bridge = Tau2Bridge(
        journal,
        ctx,
        orch,
        cfg,
        runtime["models"],
        runtime["guard"],
        runtime["modules"],
        execution_limits=ExecutionLimits(**runtime["protocol"]["limits"]),
    )
    bridge.clients = dict(runtime["clients"])
    bridge.gateway = CheckedGateway(journal)
    with bridge:
        sim = orch.run()
    reason = sim.termination_reason.value
    if reason not in {"user_stop", "agent_stop"}:
        finish(journal, ctx, memory, bank, reason)
        return {"status": "incomplete", "reason": reason}
    require(
        set(task["evaluation_criteria"]["reward_basis"]) == {"DB", "COMMUNICATE"}
        and native.communicate_scoring.scorer == "substring",
        "Unexpected source grading basis",
    )
    reward = evaluate_simulation(
        simulation=sim,
        task=orch.task,
        evaluation_type=EvaluationType.ALL,
        solo_mode=False,
        domain="retail",
        communicate_scoring=native.communicate_scoring,
    )
    journal.append(
        [
            ctx.event(
                "business.transition",
                {
                    "origin": "revisit.source_simulation",
                    "simulation_ref": journal.blobs.put(sim.model_dump(mode="json")),
                },
            )
        ]
    )
    finish(
        journal,
        ctx,
        memory,
        bank,
        reason,
        {"reward": reward.reward, "native_reward": reward.model_dump(mode="json")},
    )
    return {"status": "completed", "run": ctx.run_id}


def run_writer(journal, job, runtime, source_folder, source_run, source_hash, instruction):
    cfg = runtime["connection"]
    client = runtime["clients"]["worker"]
    experience = import_recorded_experience(
        journal, source_folder, source_run_id=source_run, log_sha256=source_hash
    )
    model = dict(
        model=cfg["worker"]["model_id"],
        provider=provider_route(cfg, "worker"),
        stream=False,
        **cfg["worker"]["parameters"],
    )
    model["seed"] = runtime["protocol"]["writer_seed"]
    limits = runtime["protocol"]["limits"]
    result = write_from_experience(
        journal,
        experience,
        WriterFeedback("none"),
        instruction=instruction,
        policy=MemoryPolicy(
            True,
            "post_task",
            False,
            1,
            read_mode="none",
            max_items=4,
            max_payload_bytes=16384,
            max_return_bytes=32768,
        ),
        client=client,
        model_config=model,
        max_requests=None,
        condition_id=job["arm"],
        run_id=job["run"],
        execution_limits=ExecutionLimits(0, limits["writer_requests"], 0, limits["max_input_bytes"]),
    )
    return {
        "status": result["status"],
        "run": job["run"],
        "snapshot": result["snapshot_id"],
        "provider_error": result["error_code"],
    }


class CheckedGateway(RecordingGateway):
    """Reject partial completions before the native parser can dispatch or grade them."""

    def call(self, ctx, registry, actor, template, messages, worker, **kwargs):
        answer = super().call(ctx, registry, actor, template, messages, worker, **kwargs)
        source = registry.sources[answer[1]]
        raw = self.journal.blobs.get(source["artifact_ref"])
        code = replay_response_error(raw, None)
        if not code:
            message = raw["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls and not (message.get("content") or "").strip():
                code = "REPLAY_INCOMPLETE_REPLY"
            try:
                if any(not isinstance(read_json(c["function"]["arguments"].encode()), dict) for c in calls):
                    code = "REPLAY_INVALID_RESPONSE"
            except (ValueError, UnicodeError):
                code = "REPLAY_INVALID_RESPONSE"
        if code:
            raise ProviderError(code, raw, submitted=True)
        return answer


class FirstDecisionGateway(CheckedGateway):
    def __init__(self, journal, expected, limit):
        super().__init__(journal)
        self.expected, self.limit, self.count = expected, limit, 0

    def call(self, ctx, registry, actor, template, messages, worker, **kwargs):
        require(actor == "worker" and self.count < self.limit, "FIRST_DECISION_LIMIT")

        def check(wire):
            if self.count == 0:
                require(
                    wire == self.expected, "First request differs from the registered public prefix and treatment"
                )
            return wire

        result = super().call(
            ctx, registry, actor, template, messages, worker, transform=check, project_self_authored=True, **kwargs
        )
        allowed = {tool["function"]["name"] for tool in self.expected["tools"]}
        for call in result[0].get("tool_calls") or []:
            name = call["function"]["name"]
            if name.startswith("memory_") and name not in allowed:
                raise ProviderError("UNAVAILABLE_MEMORY_TOOL", submitted=True)
        self.count += 1
        return result


class PublicGateway(CheckedGateway):
    def call(self, ctx, registry, actor, template, messages, worker, **kwargs):
        return super().call(
            ctx, registry, actor, template, messages, worker, project_self_authored=actor == "worker", **kwargs
        )


def target_bridge(journal, ctx, orch, cfg, runtime, memory, bank, policy, instruction):
    bridge = Tau2MemoryBridge(
        journal,
        ctx,
        orch,
        cfg,
        runtime["models"],
        runtime["guard"],
        runtime["modules"],
        memory=memory,
        bank_id=bank,
        policy=policy,
        solve_instruction=instruction,
        post_instruction="",
        execution_limits=ExecutionLimits(**runtime["protocol"]["limits"]),
    )
    bridge.clients = dict(runtime["clients"])
    return bridge


def first_wire(cfg, orch, prefix, policy, instruction, items):
    system = orch.agent.system_prompt
    if policy.enabled:
        system += memory_system_suffix(policy, instruction)
    messages = [{"role": "system", "content": system}]
    if policy.preloads:
        messages.append(snapshot_message(items))
    messages.extend(prefix)
    return dict(
        model=cfg["worker"]["model_id"],
        provider=provider_route(cfg, "worker"),
        stream=False,
        tools=[t.openai_schema for t in orch.agent.tools] + memory_tools(policy.tools("solve")),
        tool_choice="auto",
        messages=messages,
        **cfg["worker"]["parameters"],
    )


def bridge_instruction(arm, instructions):
    if arm in {"preload", "empty_preload"}:
        return past_memory_instruction(instructions["preload"], notice_version="domain-auth-v2")
    return instructions["query"] if arm in {"query", "empty_query"} else ""


def decision_interface(job, instructions):
    arm = job.get("native_arm", job["arm"])
    instruction = instructions[job["instruction"]] if "instruction" in job else bridge_instruction(arm, instructions)
    return arm, policy_for(arm), instruction


def first_decision(journal, job, task, prefix, runtime, items, sources, *, keep):
    protocol = runtime["protocol"]
    arm, policy, inst = decision_interface(job, runtime["instructions"])
    require(
        digest(canonical(prefix)) == job["prefix"]
        and not (arm in {"no_memory", "empty_query", "empty_preload"} and items),
        "Wrong public prefix or control bank",
    )
    cfg, orch, _ = build_episode(protocol, runtime["connection"], task, prefix=prefix)
    expected = first_wire(cfg, orch, prefix, policy, inst, items)
    if "initial_sha256" in job:
        require(
            digest(canonical(expected)) == job["initial_sha256"]
            and digest(canonical(items)) == job["items_sha256"]
            and sources == job["writer_runs"],
            "Decision input, source writers or bank differ from registration",
        )
    ctx, memory, bank = start(journal, job, runtime, orch, policy, items, sources)
    bridge = target_bridge(journal, ctx, orch, cfg, runtime, memory, bank, policy, inst)
    limit = protocol["first_worker_requests"] if arm in {"query", "empty_query"} else 1
    bridge.gateway = FirstDecisionGateway(journal, expected, limit)
    orch.initialize()  # Auth and profile are read-only and checked by the native strict replay.
    for i, (public, rendered) in enumerate(
        zip(prefix, bridge.utils.to_litellm_messages(orch.trajectory), strict=True), 1
    ):
        event = ctx.event(
            "business.transition",
            {"origin": "revisit.public_prefix", "public_message_ref": journal.blobs.put(public)},
        )
        journal.append([event])
        bridge.remember(i, rendered, (bridge.registry.add("business_result", event["event_id"]), ""))
    t0 = time.perf_counter()
    try:
        with bridge:
            orch.step()  # Private memory-only rounds happen inside this one native agent step.
    except ContractError as error:
        if str(error) != "FIRST_DECISION_LIMIT" or not job.get("score_private_limit"):
            raise
        private = [
            e for e in journal.events.values()
            if e["payload"].get("origin") == "tau2_memory_dispatch" and e["payload"]["memory_only"]
        ]
        require(len(private) == bridge.gateway.count == limit, "Unverified private-only response limit")
        finish(journal, ctx, memory, bank, "private_read_limit")
        return {"status": "private_read_limit", "run": job["run"]}
    raw, _, rid = bridge.last_response
    require(
        orch.step_count == 1
        and (
            not raw.get("tool_calls")
            or any(not c["function"]["name"].startswith("memory_") for c in raw["tool_calls"])
        ),
        "No first business decision",
    )
    checkpoint = {
        "task_sha256": digest(canonical(task)),
        "initial_sha256": digest(canonical(expected)),
        "items_sha256": digest(canonical(items)),
        "public_prefix_sha256": job["prefix"],
        "request_id": rid,
        "native_db": orch.environment.get_db_hash(),
        "elapsed_seconds": time.perf_counter() - t0,
    }
    if keep:
        checkpoint.update(
            trajectory=[m.model_dump(mode="json") for m in orch.trajectory],
            message=orch.message.model_dump(mode="json"),
            agent_state=orch.agent_state.model_dump(mode="json"),
            user_state=orch.user_state.model_dump(mode="json"),
            from_role=orch.from_role.value,
            to_role=orch.to_role.value,
            done=orch.done,
            termination=orch.termination_reason.value if orch.termination_reason else None,
            native_errors=orch.num_errors,
            sources=bridge.registry.sources,
            templates=bridge.registry.approved_templates,
            history_sources=bridge.history_sources,
            history_hashes=bridge.history_hashes,
            tool_sources=bridge.tool_sources,
            input_prefix=bridge.input_prefix,
            preload_source=bridge.preload_source,
            last_response=bridge.last_response,
            pending_prefix=bridge.pending["prefix"] if bridge.pending else None,
            pending_answer=bridge.pending["answer"].model_dump(mode="json") if bridge.pending else None,
        )
    ref = journal.blobs.put(checkpoint)
    journal.append([ctx.event("business.transition", {"origin": "revisit.first_decision", "checkpoint_ref": ref})])
    if not keep:
        finish(journal, ctx, memory, bank, "first_decision")
    return {"status": "checkpoint" if keep else "decision", "run": ctx.run_id, "checkpoint": ref, "request": rid}


def restore_decision(journal, job, task, prefix, runtime, record):
    """Restore the exact native and memory state, without advancing the journal or calling a model."""
    from pydantic import TypeAdapter
    from tau2.data_model.message import AssistantMessage, Message
    from tau2.data_model.simulation import TerminationReason

    require(
        not journal.scan["pending_requests"] and set(journal.scan["incomplete_runs"]) == {job["run"]},
        "Continuation requires the exclusively paused original session",
    )
    last = list(journal.events.values())[-1]
    require(
        last["payload"] == {"origin": "revisit.first_decision", "checkpoint_ref": record["checkpoint"]}
        and last["run_id"] == job["run"],
        "No pristine first-decision checkpoint",
    )
    cp = journal.blobs.get(record["checkpoint"])
    require(
        cp["task_sha256"] == digest(canonical(task))
        and cp["public_prefix_sha256"] == job["prefix"] == digest(canonical(prefix))
        and cp["request_id"] == record["request"],
        "Continuation case differs from the sealed decision",
    )
    cfg, orch, _ = build_episode(runtime["protocol"], runtime["connection"], task, prefix=prefix)
    started = journal.scan["starts"][job["run"]]
    ctx = RunContext(
        journal.experiment_id,
        job["run"],
        started["session_id"],
        job["arm"],
        "probe",
        "solve",
        started["record_kind"],
    )
    _, policy, inst = decision_interface(job, runtime["instructions"])
    bank = started["payload"]["bank_id"]
    memory = MemoryService(journal, record_kind=ctx.record_kind)
    bridge = target_bridge(journal, ctx, orch, cfg, runtime, memory, bank, policy, inst)
    items = snapshot_items(journal.blobs, journal.state, started["payload"]["start_snapshot_id"]) if bank else []
    require(
        digest(canonical(items)) == cp["items_sha256"]
        and digest(canonical(first_wire(cfg, orch, prefix, policy, inst, items))) == cp["initial_sha256"],
        "Continuation route, prefix or memory changed",
    )
    orch.initialize()
    require(orch.environment.get_db_hash() == cp["native_db"], "The first decision changed the business DB")
    adapter = TypeAdapter(Message)
    orch.trajectory = [adapter.validate_python(m) for m in cp["trajectory"]]
    orch.message = adapter.validate_python(cp["message"])
    orch.agent_state = type(orch.agent_state).model_validate(cp["agent_state"])
    orch.user_state = type(orch.user_state).model_validate(cp["user_state"])
    orch.from_role, orch.to_role = type(orch.from_role)(cp["from_role"]), type(orch.to_role)(cp["to_role"])
    orch.step_count, orch.num_errors, orch.done = 1, cp["native_errors"], cp["done"]
    orch.termination_reason = TerminationReason(cp["termination"]) if cp["termination"] else None
    orch._run_start_perf = time.perf_counter() - cp["elapsed_seconds"]
    bridge.registry.sources, bridge.registry.approved_templates = cp["sources"], cp["templates"]
    bridge.history_sources = {int(k): tuple(v) for k, v in cp["history_sources"].items()}
    bridge.history_hashes = {int(k): v for k, v in cp["history_hashes"].items()}
    bridge.tool_sources = {k: tuple(v) for k, v in cp["tool_sources"].items()}
    bridge.last_response, bridge.input_prefix, bridge.preload_source = (
        cp["last_response"],
        cp["input_prefix"],
        cp["preload_source"],
    )
    if cp["pending_answer"] is not None:
        answer = AssistantMessage.model_validate(cp["pending_answer"])
        bridge.pending = {
            "answer": answer,
            "request_id": cp["request_id"],
            "business_calls": orch.message.tool_calls,
            "results": None,
            "prefix": cp["pending_prefix"],
        }
    bridge.gateway = PublicGateway(journal)
    return orch, bridge


def continue_decision(journal, job, task, prefix, runtime, record):
    from tau2.domains.retail.environment import get_environment
    from tau2.evaluator.evaluator_env import EnvironmentEvaluator

    orch, bridge = restore_decision(journal, job, task, prefix, runtime, record)
    journal.append(
        [bridge.ctx.event("business.transition", {"origin": "revisit.continued", "checkpoint_ref": record["checkpoint"]})]
    )
    with bridge:
        while not orch.done:
            orch.step()
            orch._check_termination()
    reason = orch.termination_reason.value
    trajectory = [m.model_dump(mode="json") for m in orch.trajectory]
    ref = journal.blobs.put(trajectory)
    result = None
    if reason in {"agent_stop", "user_stop"}:
        score = EnvironmentEvaluator.calculate_reward(get_environment, orch.task, orch.trajectory)
        result = {"reward": score.reward, "native_reward": score.model_dump(mode="json"), "trajectory_ref": ref}
    finish(journal, bridge.ctx, bridge.memory, bridge.bank_id, reason, result)
    return {
        "status": "completed" if result else "incomplete",
        "run": bridge.ctx.run_id,
        "termination": reason,
        "trajectory": ref,
        "db_reward": result["reward"] if result else None,
    }
